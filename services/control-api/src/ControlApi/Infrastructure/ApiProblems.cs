using ControlApi.Persistence;
using ControlApi.Semantic;
using System.Diagnostics;
using Microsoft.AspNetCore.Diagnostics;
using Microsoft.AspNetCore.Mvc;

namespace ControlApi;

public static class ApiProblems
{
    public static ProblemDetails Create(
        HttpContext context,
        int status,
        string code,
        string title,
        string detail)
    {
        var problem = new ProblemDetails
        {
            Status = status,
            Title = title,
            Detail = detail,
            Type = $"https://semantic-data-nexus.dev/problems/{code}",
            Instance = context.Request.Path
        };
        problem.Extensions["code"] = code;
        problem.Extensions["traceId"] =
            Activity.Current?.TraceId.ToString() ?? context.TraceIdentifier;
        if (context.Items.TryGetValue(CorrelationMiddleware.ItemKey, out var correlationId))
        {
            problem.Extensions["correlationId"] = correlationId;
        }

        return problem;
    }
}

public sealed class ApiExceptionHandler(
    IProblemDetailsService problemDetailsService,
    ILogger<ApiExceptionHandler> logger) : IExceptionHandler
{
    private static readonly Action<ILogger, string, string, Exception?> LogServerFailure =
        LoggerMessage.Define<string, string>(
            LogLevel.Error,
            new EventId(1, "ApiServerFailure"),
            "Request failed with diagnostic code {DiagnosticCode} and trace ID {TraceId}.");

    private static readonly Action<ILogger, string, string, Exception?> LogClientFailure =
        LoggerMessage.Define<string, string>(
            LogLevel.Information,
            new EventId(2, "ApiClientFailure"),
            "Request rejected with diagnostic code {DiagnosticCode} and trace ID {TraceId}.");

    public async ValueTask<bool> TryHandleAsync(
        HttpContext httpContext,
        Exception exception,
        CancellationToken cancellationToken)
    {
        var (status, code, title, detail) = exception switch
        {
            RunNotFoundException =>
                (404, "run_not_found", "Run not found", exception.Message),
            OptimisticConcurrencyException =>
                (409, "optimistic_concurrency_conflict", "Concurrency conflict", exception.Message),
            IdempotencyConflictException =>
                (409, "idempotency_conflict", "Idempotency conflict", exception.Message),
            InvalidRunTransitionException =>
                (409, "invalid_run_transition", "Invalid run transition", exception.Message),
            SemanticBackendException { DiagnosticCode: "semantic_backend_timeout" } =>
                (504, "semantic_backend_timeout", "Semantic backend timeout", exception.Message),
            SemanticBackendException semanticException =>
                (502, semanticException.DiagnosticCode, "Semantic backend failure", semanticException.Message),
            BadHttpRequestException =>
                (400, "invalid_request", "Invalid request", "The request could not be parsed."),
            _ =>
                (500, "internal_error", "Internal server error", "An unexpected error occurred.")
        };

        var traceId = Activity.Current?.TraceId.ToString() ?? httpContext.TraceIdentifier;
        if (status >= 500)
        {
            LogServerFailure(logger, code, traceId, exception);
        }
        else
        {
            LogClientFailure(logger, code, traceId, exception);
        }

        httpContext.Response.StatusCode = status;
        return await problemDetailsService.TryWriteAsync(new ProblemDetailsContext
        {
            HttpContext = httpContext,
            ProblemDetails = ApiProblems.Create(httpContext, status, code, title, detail),
            Exception = exception
        });
    }
}
