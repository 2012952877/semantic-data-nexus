using System.Diagnostics;
using System.Net;
using System.Text;
using ControlApi.Domain;
using ControlApi.Semantic;
using Microsoft.Extensions.Logging.Abstractions;

namespace ControlApi.Tests;

public sealed class SemanticBackendClientTests
{
    [Fact]
    public async Task StartUsesTypedBoundaryAndDoesNotRetry()
    {
        var runId = RunId.New();
        var calls = 0;
        var handler = new DelegateHandler(async (request, cancellationToken) =>
        {
            calls++;
            Assert.Equal(HttpMethod.Post, request.Method);
            Assert.Equal("/v1/runs", request.RequestUri!.AbsolutePath);
            var body = await request.Content!.ReadAsStringAsync(cancellationToken);
            Assert.Contains(runId.Value, body, StringComparison.Ordinal);
            return new HttpResponseMessage(HttpStatusCode.ServiceUnavailable);
        });
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.StartAsync(
                new SemanticRunStart(runId, "synthetic-workload", "synthetic-user", "trace"),
                default));

        Assert.Equal("semantic_backend_start_failed", exception.DiagnosticCode);
        Assert.Equal(1, calls);
    }

    [Fact]
    public async Task TimeoutIsMappedAndCancellationPropagates()
    {
        var handler = new DelegateHandler(async (_, cancellationToken) =>
        {
            await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
            return new HttpResponseMessage(HttpStatusCode.OK);
        });
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/"),
            Timeout = TimeSpan.FromMilliseconds(50)
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var timeout = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetStatusAsync(RunId.New(), default));
        Assert.Equal("semantic_backend_timeout", timeout.DiagnosticCode);

        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            client.GetStatusAsync(RunId.New(), cancellation.Token));
    }

    [Fact]
    public async Task MalformedJsonIsMappedToInvalidResponse()
    {
        var handler = new DelegateHandler((_, _) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("{not-json", Encoding.UTF8, "application/json")
            }));
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetStatusAsync(RunId.New(), default));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public void BackendValidatorRejectsIdentityUsageCollectionAndTimelineViolations()
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Status(runId, RunState.Running);
        SemanticRunStatus[] invalidStatuses =
        [
            valid with { RunId = RunId.New() },
            valid with { TokenUsage = new TokenUsage(-1, 0) },
            valid with { Stages = null! },
            valid with { Diagnostics = null! },
            valid with { StartedAt = null },
            valid with
            {
                State = RunState.Succeeded,
                FinalizedAt = null
            },
            valid with
            {
                Stages =
                [
                    new StageSummary(
                        "stage",
                        "Stage",
                        RunState.Succeeded,
                        DateTimeOffset.UtcNow,
                        DateTimeOffset.UtcNow.AddSeconds(-1),
                        [])
                ]
            }
        ];

        foreach (var status in invalidStatuses)
        {
            var exception = Assert.Throws<SemanticBackendException>(() =>
                SemanticRunStatusValidator.Validate(status, runId));
            Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
            Assert.Equal(SemanticFailureKind.InvalidResponse, exception.FailureKind);
        }
    }
}
