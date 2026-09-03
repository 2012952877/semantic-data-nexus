using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using ControlApi.Domain;
using Microsoft.Extensions.Options;

namespace ControlApi.Semantic;

public sealed class SemanticBackendOptions
{
    public const string SectionName = "SemanticBackend";
    public bool UseFake { get; init; }
    public string? BaseUri { get; init; }
    public int TimeoutSeconds { get; init; } = 5;
}

public sealed record SemanticRunStart(
    RunId RunId,
    string Workload,
    string RequestedBy,
    string TraceId);

public sealed record SemanticRunStatus(
    RunId RunId,
    RunState State,
    IReadOnlyList<StageSummary> Stages,
    TokenUsage TokenUsage,
    IReadOnlyList<DiagnosticSummary> Diagnostics,
    DateTimeOffset? FinalizedAt);

public interface ISemanticBackendClient
{
    Task<SemanticRunStatus> StartAsync(
        SemanticRunStart request,
        CancellationToken cancellationToken);

    Task<SemanticRunStatus> GetStatusAsync(RunId runId, CancellationToken cancellationToken);
    Task RequestCancellationAsync(RunId runId, CancellationToken cancellationToken);
    Task<bool> IsReadyAsync(CancellationToken cancellationToken);
}

public sealed class SemanticBackendException(
    string diagnosticCode,
    string message,
    HttpStatusCode? backendStatus = null,
    Exception? innerException = null)
    : Exception(message, innerException)
{
    public string DiagnosticCode { get; } = diagnosticCode;
    public HttpStatusCode? BackendStatus { get; } = backendStatus;
}

public sealed class HttpSemanticBackendClient(
    HttpClient httpClient,
    ILogger<HttpSemanticBackendClient> logger) : ISemanticBackendClient
{
    private static readonly JsonSerializerOptions SerializerOptions = CreateSerializerOptions();
    private static readonly Action<ILogger, string, Exception?> LogReadinessFailure =
        LoggerMessage.Define<string>(
            LogLevel.Warning,
            new EventId(1, "SemanticBackendReadinessFailure"),
            "Semantic backend readiness probe failed with diagnostic code {DiagnosticCode}.");

    public Task<SemanticRunStatus> StartAsync(
        SemanticRunStart request,
        CancellationToken cancellationToken) =>
        SendAsync(
            new HttpRequestMessage(HttpMethod.Post, "v1/runs")
            {
                Content = JsonContent.Create(request, options: SerializerOptions)
            },
            "semantic_backend_start_failed",
            cancellationToken);

    public Task<SemanticRunStatus> GetStatusAsync(RunId runId, CancellationToken cancellationToken) =>
        SendAsync(
            new HttpRequestMessage(HttpMethod.Get, $"v1/runs/{Uri.EscapeDataString(runId.Value)}"),
            "semantic_backend_status_failed",
            cancellationToken);

    public async Task RequestCancellationAsync(RunId runId, CancellationToken cancellationToken)
    {
        using var request = new HttpRequestMessage(
            HttpMethod.Post,
            $"v1/runs/{Uri.EscapeDataString(runId.Value)}/cancel");
        using var response = await SendCoreAsync(request, cancellationToken);
        if (!response.IsSuccessStatusCode)
        {
            throw CreateFailure("semantic_backend_cancel_failed", response.StatusCode);
        }
    }

    public async Task<bool> IsReadyAsync(CancellationToken cancellationToken)
    {
        try
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, "health/ready");
            using var response = await SendCoreAsync(request, cancellationToken);
            return response.IsSuccessStatusCode;
        }
        catch (SemanticBackendException exception)
        {
            LogReadinessFailure(logger, exception.DiagnosticCode, exception);
            return false;
        }
    }

    private async Task<SemanticRunStatus> SendAsync(
        HttpRequestMessage request,
        string failureCode,
        CancellationToken cancellationToken)
    {
        try
        {
            using (request)
            using (var response = await SendCoreAsync(request, cancellationToken))
            {
                if (!response.IsSuccessStatusCode)
                {
                    throw CreateFailure(failureCode, response.StatusCode);
                }

                var result = await response.Content.ReadFromJsonAsync<SemanticRunStatus>(
                        SerializerOptions,
                        cancellationToken)
                    .ConfigureAwait(false);
                return result ?? throw new SemanticBackendException(
                    "semantic_backend_invalid_response",
                    "The semantic backend returned an empty response.");
            }
        }
        catch (JsonException exception)
        {
            throw new SemanticBackendException(
                "semantic_backend_invalid_response",
                "The semantic backend returned an invalid response.",
                innerException: exception);
        }
        catch (NotSupportedException exception)
        {
            throw new SemanticBackendException(
                "semantic_backend_invalid_response",
                "The semantic backend returned an unsupported response.",
                innerException: exception);
        }
    }

    private async Task<HttpResponseMessage> SendCoreAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken)
    {
        try
        {
            return await httpClient.SendAsync(
                request,
                HttpCompletionOption.ResponseContentRead,
                cancellationToken);
        }
        catch (OperationCanceledException exception) when (!cancellationToken.IsCancellationRequested)
        {
            throw new SemanticBackendException(
                "semantic_backend_timeout",
                "The semantic backend did not respond within the configured timeout.",
                innerException: exception);
        }
        catch (HttpRequestException exception)
        {
            throw new SemanticBackendException(
                "semantic_backend_unavailable",
                "The semantic backend could not be reached.",
                innerException: exception);
        }
    }

    private static SemanticBackendException CreateFailure(string code, HttpStatusCode status) =>
        new(code, "The semantic backend rejected the control-plane request.", status);

    private static JsonSerializerOptions CreateSerializerOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web);
        options.Converters.Add(new JsonStringEnumConverter(allowIntegerValues: false));
        return options;
    }
}

public sealed class FakeSemanticBackendClient(TimeProvider timeProvider) : ISemanticBackendClient
{
    private readonly object gate = new();
    private readonly Dictionary<RunId, SemanticRunStatus> runs = [];

    public Task<SemanticRunStatus> StartAsync(
        SemanticRunStart request,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var status = new SemanticRunStatus(
                request.RunId,
                RunState.Starting,
                [],
                new TokenUsage(0, 0),
                [],
                null);
            runs[request.RunId] = status;
            return Task.FromResult(status);
        }
    }

    public Task<SemanticRunStatus> GetStatusAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            return Task.FromResult(
                runs.GetValueOrDefault(runId) ??
                throw new SemanticBackendException(
                    "semantic_backend_run_not_found",
                    "The semantic backend does not recognize the run.",
                    HttpStatusCode.NotFound));
        }
    }

    public Task RequestCancellationAsync(RunId runId, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            if (runs.TryGetValue(runId, out var current))
            {
                runs[runId] = current with
                {
                    State = RunState.Cancelled,
                    FinalizedAt = timeProvider.GetUtcNow()
                };
            }
        }

        return Task.CompletedTask;
    }

    public Task<bool> IsReadyAsync(CancellationToken cancellationToken) =>
        Task.FromResult(true);
}
