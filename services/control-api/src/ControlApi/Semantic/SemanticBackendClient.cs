using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using ControlApi.Contracts;
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
    string ClientRequestId,
    string Workload,
    string Question,
    DateTimeOffset EvaluationClock,
    string EvaluationTimezone,
    CompilationMode CompilationMode,
    ExecutionMode ExecutionMode,
    OutputMode OutputMode,
    string RequestedBy,
    string TraceId);

public sealed record SemanticRunStatus(
    RunId RunId,
    RunState State,
    DateTimeOffset? StartedAt,
    DateTimeOffset? FinalizedAt,
    IReadOnlyList<StageSummary> Stages,
    TokenUsage TokenUsage,
    IReadOnlyList<DiagnosticSummary> Diagnostics);

public interface ISemanticBackendClient
{
    Task<SemanticRunStatus> StartAsync(
        SemanticRunStart request,
        CancellationToken cancellationToken);

    Task<SemanticRunStatus> GetStatusAsync(RunId runId, CancellationToken cancellationToken);
    Task<SemanticRunDetail> GetDetailAsync(RunId runId, CancellationToken cancellationToken);
    Task RequestCancellationAsync(RunId runId, CancellationToken cancellationToken);
    Task<bool> IsReadyAsync(CancellationToken cancellationToken);
}

public enum SemanticFailureKind
{
    UnknownOutcome,
    Rejected,
    NotFound,
    InvalidResponse
}

public sealed class SemanticBackendException(
    string diagnosticCode,
    string message,
    HttpStatusCode? backendStatus = null,
    SemanticFailureKind failureKind = SemanticFailureKind.UnknownOutcome,
    Exception? innerException = null)
    : Exception(message, innerException)
{
    public string DiagnosticCode { get; } = diagnosticCode;
    public HttpStatusCode? BackendStatus { get; } = backendStatus;
    public SemanticFailureKind FailureKind { get; } = failureKind;
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
            request.RunId,
            true,
            cancellationToken);

    public Task<SemanticRunStatus> GetStatusAsync(RunId runId, CancellationToken cancellationToken) =>
        SendAsync(
            new HttpRequestMessage(HttpMethod.Get, $"v1/runs/{Uri.EscapeDataString(runId.Value)}"),
            "semantic_backend_status_failed",
            runId,
            false,
            cancellationToken);

    public async Task<SemanticRunDetail> GetDetailAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        try
        {
            using var request = new HttpRequestMessage(
                HttpMethod.Get,
                $"v1/runs/{Uri.EscapeDataString(runId.Value)}/detail");
            using var response = await SendCoreAsync(request, cancellationToken);
            if (!response.IsSuccessStatusCode)
            {
                throw CreateFailure("semantic_backend_detail_failed", response.StatusCode);
            }

            var result = await response.Content.ReadFromJsonAsync<SemanticRunDetail>(
                    SerializerOptions,
                    cancellationToken)
                .ConfigureAwait(false);
            if (result is null)
            {
                throw InvalidResponse("The semantic backend returned an empty detail response.");
            }

            SemanticRunDetailValidator.Validate(result, runId);
            return result;
        }
        catch (JsonException exception)
        {
            throw new SemanticBackendException(
                "semantic_backend_invalid_response",
                "The semantic backend returned an invalid detail response.",
                failureKind: SemanticFailureKind.InvalidResponse,
                innerException: exception);
        }
        catch (NotSupportedException exception)
        {
            throw new SemanticBackendException(
                "semantic_backend_invalid_response",
                "The semantic backend returned an unsupported detail response.",
                failureKind: SemanticFailureKind.InvalidResponse,
                innerException: exception);
        }
    }

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
        RunId expectedRunId,
        bool isStart,
        CancellationToken cancellationToken)
    {
        try
        {
            using (request)
            using (var response = await SendCoreAsync(request, cancellationToken))
            {
                if (!response.IsSuccessStatusCode)
                {
                    throw CreateFailure(failureCode, response.StatusCode, isStart);
                }

                var result = await response.Content.ReadFromJsonAsync<SemanticRunStatus>(
                        SerializerOptions,
                        cancellationToken)
                    .ConfigureAwait(false);
                if (result is null)
                {
                    throw InvalidResponse("The semantic backend returned an empty response.");
                }

                SemanticRunStatusValidator.Validate(result, expectedRunId);
                return result;
            }
        }
        catch (JsonException exception)
        {
            throw new SemanticBackendException(
                "semantic_backend_invalid_response",
                "The semantic backend returned an invalid response.",
                failureKind: SemanticFailureKind.InvalidResponse,
                innerException: exception);
        }
        catch (NotSupportedException exception)
        {
            throw new SemanticBackendException(
                "semantic_backend_invalid_response",
                "The semantic backend returned an unsupported response.",
                failureKind: SemanticFailureKind.InvalidResponse,
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

    private static SemanticBackendException CreateFailure(
        string code,
        HttpStatusCode status,
        bool isStart = false)
    {
        var statusCode = (int)status;
        var failureKind = !isStart && status == HttpStatusCode.NotFound
            ? SemanticFailureKind.NotFound
            : isStart &&
              statusCode is >= 400 and < 500 &&
              status is not HttpStatusCode.RequestTimeout and
                  not HttpStatusCode.Conflict and
                  not HttpStatusCode.TooManyRequests
                ? SemanticFailureKind.Rejected
                : SemanticFailureKind.UnknownOutcome;
        return new SemanticBackendException(
            code,
            "The semantic backend rejected the control-plane request.",
            status,
            failureKind);
    }

    private static SemanticBackendException InvalidResponse(string message) =>
        new(
            "semantic_backend_invalid_response",
            message,
            failureKind: SemanticFailureKind.InvalidResponse);

    private static JsonSerializerOptions CreateSerializerOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web)
        {
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
        };
        JsonContractOptions.Configure(options);
        SemanticJsonContractOptions.Configure(options);
        return options;
    }
}

public sealed class FakeSemanticBackendClient(TimeProvider timeProvider) : ISemanticBackendClient
{
    private readonly object gate = new();
    private readonly Dictionary<RunId, SemanticRunStatus> runs = [];
    private readonly Dictionary<RunId, string> questions = [];

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
                timeProvider.GetUtcNow(),
                null,
                [],
                new TokenUsage(0, 0),
                []);
            runs[request.RunId] = status;
            questions[request.RunId] = request.Question;
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
                    HttpStatusCode.NotFound,
                    SemanticFailureKind.NotFound));
        }
    }

    public Task<SemanticRunDetail> GetDetailAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            if (!runs.ContainsKey(runId))
            {
                throw new SemanticBackendException(
                    "semantic_backend_run_not_found",
                    "The semantic backend does not recognize the run.",
                    HttpStatusCode.NotFound,
                    SemanticFailureKind.NotFound);
            }

            return Task.FromResult(new SemanticRunDetail(
                runId,
                questions[runId],
                new SemanticSqgSummary(
                    "0.1",
                    questions[runId],
                    "synthetic",
                    [],
                    [],
                    [],
                    [],
                    []),
                [],
                null,
                null,
                new SemanticLineage("query-runtime/v0", runId, [], []),
                []));
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

public static class SemanticRunStatusValidator
{
    private const int MaximumStages = 100;
    private const int MaximumNodesPerStage = 1_000;
    private const int MaximumDiagnostics = 100;

    public static void Validate(SemanticRunStatus status, RunId expectedRunId)
    {
        if (status.RunId != expectedRunId)
        {
            throw Invalid("The semantic backend returned a different run ID.");
        }

        if (!IsBackendState(status.State))
        {
            throw Invalid("The semantic backend returned an unsupported run state.");
        }

        ValidateRunTimeline(status);

        if (status.TokenUsage is null ||
            status.TokenUsage.InputTokens < 0 ||
            status.TokenUsage.OutputTokens < 0 ||
            status.TokenUsage.InputTokens > long.MaxValue - status.TokenUsage.OutputTokens)
        {
            throw Invalid("The semantic backend returned invalid token usage.");
        }

        if (status.Stages is null || status.Stages.Count > MaximumStages)
        {
            throw Invalid("The semantic backend returned an invalid stage collection.");
        }

        foreach (var stage in status.Stages)
        {
            if (stage is null ||
                !ValidLabel(stage.StageId) ||
                !ValidLabel(stage.Name) ||
                !IsBackendState(stage.State) ||
                stage.Nodes is null ||
                stage.Nodes.Count > MaximumNodesPerStage)
            {
                throw Invalid("The semantic backend returned an invalid stage.");
            }

            ValidateTimeline(stage.State, stage.StartedAt, stage.CompletedAt, "stage");
            foreach (var node in stage.Nodes)
            {
                if (node is null ||
                    !ValidLabel(node.NodeId) ||
                    !ValidLabel(node.Kind) ||
                    !IsBackendState(node.State))
                {
                    throw Invalid("The semantic backend returned an invalid node.");
                }

                ValidateTimeline(node.State, node.StartedAt, node.CompletedAt, "node");
            }
        }

        if (status.Diagnostics is null || status.Diagnostics.Count > MaximumDiagnostics)
        {
            throw Invalid("The semantic backend returned an invalid diagnostic collection.");
        }

        foreach (var diagnostic in status.Diagnostics)
        {
            if (diagnostic is null ||
                !ValidLabel(diagnostic.Code) ||
                string.IsNullOrWhiteSpace(diagnostic.Message) ||
                diagnostic.Message.Length > 512 ||
                diagnostic.OccurredAt == default ||
                diagnostic.Stage?.Length > 64)
            {
                throw Invalid("The semantic backend returned an invalid diagnostic.");
            }
        }
    }

    private static void ValidateRunTimeline(SemanticRunStatus status)
    {
        if (status.State == RunState.Queued)
        {
            if (status.StartedAt is not null || status.FinalizedAt is not null)
            {
                throw Invalid("A queued run cannot have execution timestamps.");
            }

            return;
        }

        ValidateTimeline(status.State, status.StartedAt, status.FinalizedAt, "run");
    }

    private static void ValidateTimeline(
        RunState state,
        DateTimeOffset? startedAt,
        DateTimeOffset? completedAt,
        string subject)
    {
        if (state == RunState.Queued)
        {
            if (startedAt is not null || completedAt is not null)
            {
                throw Invalid($"The queued backend {subject} cannot have timestamps.");
            }

            return;
        }

        if (startedAt is null)
        {
            throw Invalid($"The backend {subject} is missing its start timestamp.");
        }

        if (state.IsTerminal())
        {
            if (completedAt is null || completedAt < startedAt)
            {
                throw Invalid($"The backend {subject} has invalid completion timestamps.");
            }
        }
        else if (completedAt is not null)
        {
            throw Invalid($"The backend {subject} is active but has a completion timestamp.");
        }
    }

    private static bool IsBackendState(RunState state) =>
        state is RunState.Queued or
            RunState.Starting or
            RunState.Running or
            RunState.Cancelled or
            RunState.Succeeded or
            RunState.Failed;

    private static bool ValidLabel(string? value) =>
        !string.IsNullOrWhiteSpace(value) && value.Length <= 64;

    private static SemanticBackendException Invalid(string message) =>
        new(
            "semantic_backend_invalid_response",
            message,
            failureKind: SemanticFailureKind.InvalidResponse);
}
