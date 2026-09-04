using System.Diagnostics;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Semantic;
using Microsoft.Extensions.Logging.Abstractions;

namespace ControlApi.Tests;

public sealed class SemanticBackendClientTests
{
    [Fact]
    public async Task StartUsesTypedBoundaryAndDoesNotRetry()
    {
        var runId = RunId.Parse(
            "run_0123456789abcdef0123456789abcdef",
            provider: null);
        var start = new SemanticRunStart(
            runId,
            "request-typed",
            "synthetic-workload",
            "Compare synthetic regional revenue",
            DateTimeOffset.Parse("2026-08-15T09:00:00+08:00"),
            "Asia/Shanghai",
            CompilationMode.MonthlyRegionalComparison,
            ExecutionMode.Thread,
            OutputMode.Stream,
            "synthetic-user",
            "trace");
        var calls = 0;
        var handler = new DelegateHandler(async (request, cancellationToken) =>
        {
            calls++;
            Assert.Equal(HttpMethod.Post, request.Method);
            Assert.Equal("/v1/runs", request.RequestUri!.AbsolutePath);
            var body = await request.Content!.ReadAsStringAsync(cancellationToken);
            using var document = JsonDocument.Parse(body);
            using var fixture = JsonDocument.Parse(
                File.ReadAllText(Path.Combine(
                    AppContext.BaseDirectory,
                    "Fixtures",
                    "bff-start-request.json")));
            Assert.True(JsonElement.DeepEquals(fixture.RootElement, document.RootElement));
            var root = document.RootElement;
            Assert.Equal(runId.Value, root.GetProperty("runId").GetString());
            Assert.Equal("request-typed", root.GetProperty("clientRequestId").GetString());
            Assert.Equal("synthetic-workload", root.GetProperty("workload").GetString());
            Assert.Equal(
                "Compare synthetic regional revenue",
                root.GetProperty("question").GetString());
            Assert.Equal(
                "2026-08-15T09:00:00+08:00",
                root.GetProperty("evaluationClock").GetString());
            Assert.Equal("Asia/Shanghai", root.GetProperty("evaluationTimezone").GetString());
            Assert.Equal(
                "monthly_regional_comparison",
                root.GetProperty("compilationMode").GetString());
            Assert.Equal("thread", root.GetProperty("executionMode").GetString());
            Assert.Equal("stream", root.GetProperty("outputMode").GetString());
            Assert.Equal("synthetic-user", root.GetProperty("requestedBy").GetString());
            Assert.Equal("trace", root.GetProperty("traceId").GetString());
            Assert.Equal(11, root.EnumerateObject().Count());
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
                start,
                default));

        Assert.Equal("semantic_backend_start_failed", exception.DiagnosticCode);
        Assert.Equal(1, calls);
    }

    [Fact]
    public async Task DetailUsesTypedBoundaryAndRejectsMismatchedRunId()
    {
        var requested = RunId.New();
        var options = JsonOptions();
        var handler = new DelegateHandler((request, _) =>
        {
            Assert.Equal(
                $"/v1/runs/{requested.Value}/detail",
                request.RequestUri!.AbsolutePath);
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = JsonContent.Create(
                    StubSemanticBackendClient.Detail(RunId.New()),
                    options: options)
            });
        });
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetDetailAsync(requested, default));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public void DetailValidatorRejectsUnboundedRowsAndNonScalarCells()
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Detail(runId);
        var unbounded = valid with
        {
            Result = valid.Result! with
            {
                Rows = Enumerable.Repeat(valid.Result.Rows[0], 1_001).ToArray(),
                RowCount = 1_001,
                Truncated = false
            }
        };

        var exception = Assert.Throws<SemanticBackendException>(() =>
            SemanticRunDetailValidator.Validate(unbounded, runId));
        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
        Assert.Throws<JsonException>(() =>
            JsonSerializer.Deserialize<SemanticScalarValue>("{\"unsafe\":true}"));
    }

    [Fact]
    public void DetailValidatorRejectsManifestForUnknownPhysicalNode()
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Detail(runId);
        var invalid = valid with
        {
            Manifest = valid.Manifest! with { NodeId = "unknown-node" }
        };

        var exception = Assert.Throws<SemanticBackendException>(() =>
            SemanticRunDetailValidator.Validate(invalid, runId));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public async Task DetailRejectsUnknownJsonProperties()
    {
        var runId = RunId.New();
        var payload = JsonSerializer.Serialize(
            StubSemanticBackendClient.Detail(runId),
            JsonOptions());
        payload = payload[..^1] + ",\"unbounded\":{}}";
        var handler = new DelegateHandler((_, _) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(payload, Encoding.UTF8, "application/json")
            }));
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetDetailAsync(runId, default));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
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

    private static JsonSerializerOptions JsonOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web);
        JsonContractOptions.Configure(options);
        SemanticJsonContractOptions.Configure(options);
        return options;
    }
}
