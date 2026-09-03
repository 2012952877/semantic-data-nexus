using System.Diagnostics;
using System.Text.RegularExpressions;

namespace ControlApi;

public sealed partial class CorrelationMiddleware(RequestDelegate next, ILogger<CorrelationMiddleware> logger)
{
    public const string ItemKey = "ControlApi.CorrelationId";
    public const string HeaderName = "X-Correlation-ID";

    public async Task InvokeAsync(HttpContext context)
    {
        var supplied = context.Request.Headers[HeaderName].FirstOrDefault();
        var correlationId = supplied is not null && ValidCorrelationId().IsMatch(supplied)
            ? supplied
            : Activity.Current?.TraceId.ToString() ?? context.TraceIdentifier;

        context.Items[ItemKey] = correlationId;
        context.Response.OnStarting(() =>
        {
            context.Response.Headers[HeaderName] = correlationId;
            return Task.CompletedTask;
        });

        using (logger.BeginScope(new Dictionary<string, object>
        {
            ["CorrelationId"] = correlationId,
            ["TraceId"] = context.TraceIdentifier
        }))
        {
            await next(context);
        }
    }

    [GeneratedRegex("^[A-Za-z0-9._-]{1,64}$", RegexOptions.CultureInvariant)]
    private static partial Regex ValidCorrelationId();
}
