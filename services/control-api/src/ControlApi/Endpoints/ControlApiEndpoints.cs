using System.Diagnostics;
using System.Net;
using System.Security.Claims;
using ControlApi.Authentication;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Persistence;
using ControlApi.Semantic;
using Microsoft.AspNetCore.Mvc;
using Microsoft.AspNetCore.Http.HttpResults;

namespace ControlApi.Endpoints;

public static class ControlApiEndpoints
{
    public static IEndpointRouteBuilder MapControlApi(this IEndpointRouteBuilder endpoints)
    {
        var api = endpoints.MapGroup("/api/v1")
            .WithOpenApi()
            .RequireRateLimiting("api");

        api.MapGet("/me", GetCurrentPrincipal)
            .RequireAuthorization(Policies.Reader);

        var runs = api.MapGroup("/runs");
        runs.MapPost("/", CreateRun)
            .RequireAuthorization(Policies.Contributor);
        runs.MapGet("/", ListRuns)
            .RequireAuthorization(Policies.Reader);
        runs.MapGet("/{runId}", GetRun)
            .RequireAuthorization(Policies.Reader);
        runs.MapGet("/{runId}/detail", GetRunDetail)
            .RequireAuthorization(Policies.Reader);
        runs.MapPost("/{runId}/cancel", CancelRun)
            .RequireAuthorization(Policies.Contributor);
        runs.MapGet("/{runId}/semantic-status", GetSemanticStatus)
            .RequireAuthorization(Policies.Reader);
        runs.MapPost("/{runId}/feedback", SubmitFeedback)
            .RequireAuthorization(Policies.Contributor);
        runs.MapGet("/{runId}/feedback", GetFeedback)
            .RequireAuthorization(Policies.Reader);

        api.MapGet("/statistics/summary", GetStatistics)
            .RequireAuthorization(Policies.Admin);

        return endpoints;
    }

    private static Ok<CurrentPrincipalResponse> GetCurrentPrincipal(ClaimsPrincipal principal)
    {
        var subject = GetSubject(principal);
        var roles = principal.FindAll("roles")
            .Concat(principal.FindAll(ClaimTypes.Role))
            .Select(claim => claim.Value)
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Order(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        var scopes = principal.FindAll("scp")
            .SelectMany(claim => claim.Value.Split(' ', StringSplitOptions.RemoveEmptyEntries))
            .Distinct(StringComparer.OrdinalIgnoreCase)
            .Order(StringComparer.OrdinalIgnoreCase)
            .ToArray();
        return TypedResults.Ok(new CurrentPrincipalResponse(
            subject,
            principal.Identity?.Name,
            roles,
            scopes));
    }

    private static async Task<IResult> CreateRun(
        CreateRunRequest request,
        ClaimsPrincipal principal,
        HttpContext context,
        IRunRepository repository,
        ISemanticBackendClient semanticBackend,
        IRunDispatchCoordinator dispatchCoordinator,
        CancellationToken cancellationToken)
    {
        var validation = Validate(request, context);
        if (validation is not null)
        {
            return validation;
        }

        var subject = GetSubject(principal);
        var result = await repository.CreateAsync(request, subject, cancellationToken);
        await using var dispatchLease = await dispatchCoordinator.AcquireAsync(
            result.Run.Id,
            cancellationToken);
        var current = await repository.GetAsync(result.Run.Id, cancellationToken) ??
            throw new RunNotFoundException(result.Run.Id);
        if (!current.State.RequiresStartReconciliation())
        {
            return result.Created
                ? TypedResults.Accepted($"/api/v1/runs/{current.Id}", current)
                : TypedResults.Ok(current);
        }

        try
        {
            var status = current.State switch
            {
                RunState.StartPending => await StartSemanticRun(
                    current,
                    subject,
                    context,
                    semanticBackend,
                    cancellationToken),
                RunState.DispatchUnknown => await ReconcileOrStartSemanticRun(
                    current,
                    subject,
                    context,
                    semanticBackend,
                    cancellationToken),
                _ => throw new InvalidOperationException(
                    $"Run state '{current.State}' does not require start reconciliation.")
            };
            var updated = await repository.ApplySemanticStatusAsync(
                current.Id,
                current.Version,
                status,
                CancellationToken.None);
            return result.Created
                ? TypedResults.Accepted($"/api/v1/runs/{updated.Id}", updated)
                : TypedResults.Ok(updated);
        }
        catch (SemanticBackendException exception)
        {
            if (exception.FailureKind == SemanticFailureKind.Rejected)
            {
                await repository.MarkFailedAsync(
                    result.Run.Id,
                    exception.DiagnosticCode,
                    "The semantic backend definitively rejected the run.",
                    CancellationToken.None);
            }
            else
            {
                await repository.MarkStartDispatchUnknownAsync(
                    result.Run.Id,
                    exception.DiagnosticCode,
                    CancellationToken.None);
            }

            throw;
        }
        catch (OperationCanceledException)
        {
            await repository.MarkStartDispatchUnknownAsync(
                result.Run.Id,
                "semantic_backend_start_cancelled",
                CancellationToken.None);
            throw;
        }
    }

    private static Task<SemanticRunStatus> StartSemanticRun(
        RunMetadata run,
        string subject,
        HttpContext context,
        ISemanticBackendClient semanticBackend,
        CancellationToken cancellationToken) =>
        semanticBackend.StartAsync(
            new SemanticRunStart(
                run.Id,
                run.ClientRequestId,
                run.Workload,
                run.Question,
                run.EvaluationClock,
                run.EvaluationTimezone,
                run.CompilationMode,
                run.ExecutionMode,
                run.OutputMode,
                subject,
                Activity.Current?.TraceId.ToString() ?? context.TraceIdentifier),
            cancellationToken);

    private static async Task<SemanticRunStatus> ReconcileOrStartSemanticRun(
        RunMetadata run,
        string subject,
        HttpContext context,
        ISemanticBackendClient semanticBackend,
        CancellationToken cancellationToken)
    {
        try
        {
            return await semanticBackend.GetStatusAsync(run.Id, cancellationToken);
        }
        catch (SemanticBackendException exception)
            when (exception.FailureKind == SemanticFailureKind.NotFound)
        {
            return await StartSemanticRun(
                run,
                subject,
                context,
                semanticBackend,
                cancellationToken);
        }
    }

    private static async Task<IResult> ListRuns(
        int? limit,
        HttpContext context,
        IRunRepository repository,
        CancellationToken cancellationToken)
    {
        var effectiveLimit = limit ?? 50;
        if (effectiveLimit is < 1 or > 100)
        {
            return Invalid(
                context,
                "invalid_limit",
                "Limit must be between 1 and 100.");
        }

        var items = await repository.ListAsync(effectiveLimit, cancellationToken);
        return TypedResults.Ok(new RunListResponse(items, items.Count));
    }

    private static async Task<IResult> GetRun(
        string runId,
        HttpContext context,
        IRunRepository repository,
        CancellationToken cancellationToken)
    {
        if (!TryRunId(runId, context, out var id, out var problem))
        {
            return problem;
        }

        var run = await repository.GetAsync(id, cancellationToken);
        return run is null
            ? NotFound(context)
            : TypedResults.Ok(run);
    }

    private static async Task<IResult> CancelRun(
        string runId,
        CancelRunRequest? request,
        HttpContext context,
        IRunRepository repository,
        ISemanticBackendClient semanticBackend,
        IRunDispatchCoordinator dispatchCoordinator,
        CancellationToken cancellationToken)
    {
        if (!TryRunId(runId, context, out var id, out var problem))
        {
            return problem;
        }

        if (await repository.GetAsync(id, cancellationToken) is null)
        {
            return NotFound(context);
        }

        await using var dispatchLease = await dispatchCoordinator.AcquireAsync(
            id,
            cancellationToken);
        var current = await repository.GetAsync(id, cancellationToken) ??
            throw new RunNotFoundException(id);
        var expectedVersion = request?.ExpectedVersion;
        MutationResult result;
        if (current.State == RunState.DispatchUnknown)
        {
            if (expectedVersion is not null && current.Version != expectedVersion)
            {
                throw new OptimisticConcurrencyException(
                    $"Run version {expectedVersion} is stale; current version is {current.Version}.");
            }

            result = await repository.RequestCancellationAsync(
                id,
                expectedVersion,
                cancellationToken);
            try
            {
                var status = await semanticBackend.GetStatusAsync(id, cancellationToken);
                current = await repository.ApplySemanticStatusAsync(
                    id,
                    result.Run.Version,
                    status,
                    CancellationToken.None);
                if (current.State.IsTerminal())
                {
                    return TypedResults.Accepted($"/api/v1/runs/{id}", current);
                }
            }
            catch (SemanticBackendException exception)
                when (exception.FailureKind == SemanticFailureKind.NotFound)
            {
                var cancelled = await repository.FinalizeCancellationWithoutBackendAsync(
                    id,
                    expectedVersion: null,
                    expectedGeneration: result.CancellationGeneration,
                    CancellationToken.None);
                return TypedResults.Accepted($"/api/v1/runs/{id}", cancelled);
            }
        }
        else
        {
            result = await repository.RequestCancellationAsync(
                id,
                expectedVersion,
                cancellationToken);
        }
        var responseRun = result.Run;
        if (result.RequiresDispatch)
        {
            try
            {
                await semanticBackend.RequestCancellationAsync(id, cancellationToken);
                responseRun = await repository.MarkCancellationDeliveredAsync(
                    id,
                    result.CancellationGeneration,
                    CancellationToken.None);
            }
            catch (SemanticBackendException exception)
                when (exception.FailureKind == SemanticFailureKind.NotFound)
            {
                responseRun = await ReconcileMissingCancellation(
                    id,
                    result,
                    repository,
                    semanticBackend,
                    cancellationToken);
            }
        }

        return TypedResults.Accepted($"/api/v1/runs/{id}", responseRun);
    }

    private static async Task<IResult> GetRunDetail(
        string runId,
        HttpContext context,
        IRunRepository repository,
        ISemanticBackendClient semanticBackend,
        CancellationToken cancellationToken)
    {
        if (!TryRunId(runId, context, out var id, out var problem))
        {
            return problem;
        }

        var run = await repository.GetAsync(id, cancellationToken);
        if (run is null)
        {
            return NotFound(context);
        }

        var detail = await semanticBackend.GetDetailAsync(id, cancellationToken);
        if (!string.Equals(detail.Question, run.Question, StringComparison.Ordinal))
        {
            throw SemanticRunDetailValidator.Invalid(
                "The semantic backend returned a question that does not match the run.");
        }

        return TypedResults.Ok(detail);
    }

    private static async Task<RunMetadata> ReconcileMissingCancellation(
        RunId id,
        MutationResult cancellation,
        IRunRepository repository,
        ISemanticBackendClient semanticBackend,
        CancellationToken cancellationToken)
    {
        try
        {
            var current = await repository.GetAsync(id, cancellationToken) ??
                throw new RunNotFoundException(id);
            var status = await semanticBackend.GetStatusAsync(id, cancellationToken);
            var updated = await repository.ApplySemanticStatusAsync(
                id,
                current.Version,
                status,
                CancellationToken.None);
            if (!updated.State.IsTerminal())
            {
                throw new SemanticBackendException(
                    "semantic_backend_cancel_failed",
                    "The semantic backend run exists but did not accept cancellation.",
                    HttpStatusCode.NotFound,
                    SemanticFailureKind.UnknownOutcome);
            }

            return updated;
        }
        catch (SemanticBackendException exception)
            when (exception.FailureKind == SemanticFailureKind.NotFound)
        {
            return await repository.FinalizeCancellationWithoutBackendAsync(
                id,
                expectedVersion: null,
                expectedGeneration: cancellation.CancellationGeneration,
                CancellationToken.None);
        }
    }

    private static async Task<IResult> GetSemanticStatus(
        string runId,
        HttpContext context,
        IRunRepository repository,
        ISemanticBackendClient semanticBackend,
        IRunDispatchCoordinator dispatchCoordinator,
        CancellationToken cancellationToken)
    {
        if (!TryRunId(runId, context, out var id, out var problem))
        {
            return problem;
        }

        if (await repository.GetAsync(id, cancellationToken) is null)
        {
            return NotFound(context);
        }

        await using var dispatchLease = await dispatchCoordinator.AcquireAsync(
            id,
            cancellationToken);
        var run = await repository.GetAsync(id, cancellationToken);
        if (run is null)
        {
            return NotFound(context);
        }

        var status = await semanticBackend.GetStatusAsync(id, cancellationToken);
        var updated = await repository.ApplySemanticStatusAsync(
            id,
            run.Version,
            status,
            cancellationToken);
        return TypedResults.Ok(updated);
    }

    private static async Task<IResult> SubmitFeedback(
        string runId,
        SubmitFeedbackRequest request,
        ClaimsPrincipal principal,
        HttpContext context,
        IRunRepository repository,
        IRunDispatchCoordinator dispatchCoordinator,
        CancellationToken cancellationToken)
    {
        if (!TryRunId(runId, context, out var id, out var problem))
        {
            return problem;
        }

        var validation = Validate(request, context);
        if (validation is not null)
        {
            return validation;
        }

        if (await repository.GetAsync(id, cancellationToken) is null)
        {
            return NotFound(context);
        }

        await using var dispatchLease = await dispatchCoordinator.AcquireAsync(
            id,
            cancellationToken);
        var created = await repository.SubmitFeedbackAsync(
            id,
            request,
            GetSubject(principal),
            cancellationToken);
        return TypedResults.Ok(created);
    }

    private static async Task<IResult> GetFeedback(
        string runId,
        HttpContext context,
        IRunRepository repository,
        CancellationToken cancellationToken)
    {
        if (!TryRunId(runId, context, out var id, out var problem))
        {
            return problem;
        }

        var items = await repository.GetFeedbackAsync(id, cancellationToken);
        return TypedResults.Ok(items);
    }

    private static async Task<IResult> GetStatistics(
        IRunRepository repository,
        CancellationToken cancellationToken) =>
        TypedResults.Ok(await repository.GetStatisticsAsync(cancellationToken));

    private static ProblemHttpResult? Validate(CreateRunRequest request, HttpContext context)
    {
        if (string.IsNullOrWhiteSpace(request.ClientRequestId) ||
            request.ClientRequestId.Length > 64 ||
            !request.ClientRequestId.All(character =>
                char.IsAsciiLetterOrDigit(character) || character is '-' or '_' or '.'))
        {
            return Invalid(
                context,
                "invalid_client_request_id",
                "ClientRequestId must contain 1-64 ASCII letters, digits, '.', '-', or '_'.");
        }

        if (string.IsNullOrWhiteSpace(request.Workload) ||
            request.Workload.Length > 64 ||
            !request.Workload.All(character =>
                char.IsAsciiLetterOrDigit(character) || character is '-' or '_' or '.'))
        {
            return Invalid(
                context,
                "invalid_workload",
                "Workload must contain 1-64 ASCII letters, digits, '.', '-', or '_'.");
        }

        if (string.IsNullOrWhiteSpace(request.Question) ||
            request.Question.Length > 4_000)
        {
            return Invalid(
                context,
                "invalid_question",
                "Question must contain between 1 and 4000 characters.");
        }

        if (request.EvaluationClock == default)
        {
            return Invalid(
                context,
                "invalid_evaluation_clock",
                "EvaluationClock must be a timezone-aware timestamp.");
        }

        if (!IsIanaTimeZone(request.EvaluationTimezone))
        {
            return Invalid(
                context,
                "invalid_evaluation_timezone",
                "EvaluationTimezone must be a valid IANA time zone name.");
        }

        if (!Enum.IsDefined(request.CompilationMode) ||
            !Enum.IsDefined(request.ExecutionMode) ||
            !Enum.IsDefined(request.OutputMode))
        {
            return Invalid(
                context,
                "invalid_run_options",
                "CompilationMode, ExecutionMode, and OutputMode must use supported values.");
        }

        return null;
    }

    private static bool IsIanaTimeZone(string? value)
    {
        if (string.IsNullOrWhiteSpace(value) ||
            value.Length > 100 ||
            !value.Contains('/'))
        {
            return false;
        }

        try
        {
            var zone = TimeZoneInfo.FindSystemTimeZoneById(value);
            if (OperatingSystem.IsWindows() &&
                !TimeZoneInfo.TryConvertIanaIdToWindowsId(value, out _))
            {
                return false;
            }

            _ = zone.GetUtcOffset(DateTimeOffset.UnixEpoch);
            return true;
        }
        catch (TimeZoneNotFoundException)
        {
            return false;
        }
        catch (InvalidTimeZoneException)
        {
            return false;
        }
    }

    private static ProblemHttpResult? Validate(SubmitFeedbackRequest request, HttpContext context)
    {
        if (string.IsNullOrWhiteSpace(request.SubmissionId) ||
            request.SubmissionId.Length > 64 ||
            request.Rating is < 1 or > 5 ||
            !Enum.IsDefined(request.Outcome) ||
            request.ReasonCodes is null ||
            request.ReasonCodes.Count > 10 ||
            request.ReasonCodes.Any(code =>
                string.IsNullOrWhiteSpace(code) ||
                code.Length > 32 ||
                !code.All(character =>
                    char.IsAsciiLetterOrDigit(character) || character is '-' or '_' or '.')))
        {
            return Invalid(
                context,
                "invalid_feedback",
                "Feedback requires a submission ID, a 1-5 rating, an outcome, and at most 10 structured reason codes.");
        }

        return null;
    }

    private static bool TryRunId(
        string value,
        HttpContext context,
        out RunId runId,
        out IResult problem)
    {
        if (RunId.TryParse(value, null, out runId))
        {
            problem = null!;
            return true;
        }

        problem = Invalid(
            context,
            "invalid_run_id",
            "Run IDs must use the canonical run_<32 lowercase hex characters> format.");
        return false;
    }

    private static ProblemHttpResult Invalid(
        HttpContext context,
        string code,
        string detail) =>
        TypedResults.Problem(ApiProblems.Create(
            context,
            400,
            code,
            "Invalid request",
            detail));

    private static ProblemHttpResult NotFound(HttpContext context) =>
        TypedResults.Problem(ApiProblems.Create(
            context,
            404,
            "run_not_found",
            "Run not found",
            "The requested run does not exist."));

    private static string GetSubject(ClaimsPrincipal principal) =>
        principal.FindFirstValue("sub") ??
        principal.FindFirstValue(ClaimTypes.NameIdentifier) ??
        throw new InvalidOperationException("Authenticated principal has no stable subject identifier.");
}
