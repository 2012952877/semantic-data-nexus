using System.Security.Claims;
using System.Diagnostics;
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
        CancellationToken cancellationToken)
    {
        var validation = Validate(request, context);
        if (validation is not null)
        {
            return validation;
        }

        var subject = GetSubject(principal);
        var result = await repository.CreateAsync(request, subject, cancellationToken);
        if (!result.Created)
        {
            return TypedResults.Ok(result.Run);
        }

        try
        {
            var status = await semanticBackend.StartAsync(
                new SemanticRunStart(
                    result.Run.Id,
                    result.Run.Workload,
                    subject,
                    Activity.Current?.TraceId.ToString() ?? context.TraceIdentifier),
                cancellationToken);
            var updated = await repository.ApplySemanticStatusAsync(
                result.Run.Id,
                result.Run.Version,
                status,
                cancellationToken);
            return TypedResults.Accepted($"/api/v1/runs/{updated.Id}", updated);
        }
        catch (SemanticBackendException exception)
        {
            await repository.MarkFailedAsync(
                result.Run.Id,
                exception.DiagnosticCode,
                "The semantic backend did not accept the run.",
                cancellationToken);
            throw;
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
        CancellationToken cancellationToken)
    {
        if (!TryRunId(runId, context, out var id, out var problem))
        {
            return problem;
        }

        var result = await repository.RequestCancellationAsync(
            id,
            request?.ExpectedVersion,
            cancellationToken);
        if (result.Run.State == RunState.CancelRequested)
        {
            await semanticBackend.RequestCancellationAsync(id, cancellationToken);
        }

        return TypedResults.Accepted($"/api/v1/runs/{id}", result.Run);
    }

    private static async Task<IResult> GetSemanticStatus(
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

        return null;
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
                "Feedback requires a submission ID, a 1-5 rating, and at most 10 structured reason codes.");
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
