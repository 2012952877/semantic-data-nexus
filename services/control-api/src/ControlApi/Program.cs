using System.Diagnostics;
using System.Net;
using System.Threading.RateLimiting;
using Azure.Monitor.OpenTelemetry.AspNetCore;
using ControlApi;
using ControlApi.Authentication;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Endpoints;
using ControlApi.Infrastructure;
using ControlApi.Persistence;
using ControlApi.Semantic;
using Microsoft.AspNetCore.HttpOverrides;
using Microsoft.AspNetCore.RateLimiting;
using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.OpenApi.Models;
using OpenTelemetry.Logs;
using OpenTelemetry.Metrics;
using OpenTelemetry.Resources;
using OpenTelemetry.Trace;

var builder = WebApplication.CreateBuilder(args);

builder.Logging.Configure(options =>
    options.ActivityTrackingOptions =
        ActivityTrackingOptions.TraceId |
        ActivityTrackingOptions.SpanId |
        ActivityTrackingOptions.ParentId);

builder.Services.AddProblemDetails();
builder.Services.AddExceptionHandler<ApiExceptionHandler>();
builder.Services.AddEndpointsApiExplorer();
builder.Services.AddSwaggerGen(options =>
{
    options.AddSecurityDefinition("Bearer", new OpenApiSecurityScheme
    {
        Type = SecuritySchemeType.Http,
        Scheme = "bearer",
        BearerFormat = "JWT",
        Description = "Microsoft Entra bearer token."
    });
    options.OperationFilter<OpenApiSecurityOperationFilter>();
    options.MapType<RunId>(() => new OpenApiSchema
    {
        Type = "string",
        Pattern = "^run_[0-9a-f]{32}$"
    });
    options.MapType<SemanticScalarValue>(() => new OpenApiSchema
    {
        Nullable = true,
        OneOf =
        [
            new OpenApiSchema { Type = "string" },
            new OpenApiSchema { Type = "integer", Format = "int64" },
            new OpenApiSchema { Type = "number", Format = "decimal" },
            new OpenApiSchema { Type = "boolean" }
        ]
    });
});
builder.Services.ConfigureHttpJsonOptions(options =>
{
    JsonContractOptions.Configure(options.SerializerOptions);
    SemanticJsonContractOptions.Configure(options.SerializerOptions);
});
builder.Services.AddSingleton(TimeProvider.System);
builder.Services.AddSingleton<IRunRepository, InMemoryRunRepository>();
builder.Services.AddSingleton<IRunDispatchCoordinator, RunDispatchCoordinator>();

var localAuthOptions = builder.Configuration
    .GetSection(LocalDevelopmentAuthOptions.SectionName)
    .Get<LocalDevelopmentAuthOptions>() ?? new LocalDevelopmentAuthOptions();
var semanticOptions = builder.Configuration
    .GetSection(SemanticBackendOptions.SectionName)
    .Get<SemanticBackendOptions>() ?? new SemanticBackendOptions();
HostingSafety.Validate(
    builder.Environment.EnvironmentName,
    localAuthOptions.Enabled,
    semanticOptions.UseFake);

builder.Services.AddControlApiAuthentication(builder.Configuration, builder.Environment);

builder.Services.Configure<ForwardedHeadersOptions>(options =>
{
    options.ForwardedHeaders =
        ForwardedHeaders.XForwardedFor |
        ForwardedHeaders.XForwardedProto;
    options.ForwardLimit = 1;
    options.RequireHeaderSymmetry = true;
    foreach (var configuredProxy in builder.Configuration
        .GetSection("ForwardedHeaders:KnownProxies")
        .Get<string[]>() ?? [])
    {
        if (!IPAddress.TryParse(configuredProxy, out var proxy))
        {
            throw new InvalidOperationException(
                "ForwardedHeaders:KnownProxies entries must be valid IP addresses.");
        }

        options.KnownProxies.Add(proxy);
    }
});

if (semanticOptions.TimeoutSeconds is < 1 or > 30)
{
    throw new InvalidOperationException(
        "SemanticBackend:TimeoutSeconds must be between 1 and 30.");
}

if (semanticOptions.UseFake)
{
    builder.Services.AddSingleton<ISemanticBackendClient, FakeSemanticBackendClient>();
}
else
{
    if (!Uri.TryCreate(semanticOptions.BaseUri, UriKind.Absolute, out var semanticBaseUri) ||
        semanticBaseUri.Scheme is not ("http" or "https"))
    {
        throw new InvalidOperationException(
            "SemanticBackend:BaseUri must be an absolute HTTP or HTTPS URI when UseFake is false.");
    }

    if (builder.Environment.IsProduction() &&
        semanticBaseUri.Scheme != Uri.UriSchemeHttps)
    {
        throw new InvalidOperationException(
            "Production requires an HTTPS SemanticBackend:BaseUri.");
    }

    builder.Services.AddHttpClient<ISemanticBackendClient, HttpSemanticBackendClient>(client =>
    {
        client.BaseAddress = semanticBaseUri;
        client.Timeout = TimeSpan.FromSeconds(semanticOptions.TimeoutSeconds);
        client.MaxResponseContentBufferSize = 1024 * 1024;
    });
}

builder.Services.AddHealthChecks()
    .AddCheck("self", () => HealthCheckResult.Healthy(), tags: ["live"])
    .AddCheck<SemanticBackendHealthCheck>("semantic-backend", tags: ["ready"]);

builder.Services.AddRateLimiter(options =>
{
    var permitLimit = builder.Configuration.GetValue("RateLimiting:PermitLimit", 100);
    var windowSeconds = builder.Configuration.GetValue("RateLimiting:WindowSeconds", 60);
    if (permitLimit is < 1 or > 10_000 || windowSeconds is < 1 or > 3_600)
    {
        throw new InvalidOperationException(
            "Rate limiting requires PermitLimit 1-10000 and WindowSeconds 1-3600.");
    }

    options.RejectionStatusCode = StatusCodes.Status429TooManyRequests;
    options.AddPolicy("api", context =>
        RateLimitPartition.GetFixedWindowLimiter(
            context.User.FindFirst("sub")?.Value ??
            context.Connection.RemoteIpAddress?.ToString() ??
            "anonymous",
            _ => new FixedWindowRateLimiterOptions
            {
                PermitLimit = permitLimit,
                Window = TimeSpan.FromSeconds(windowSeconds),
                QueueLimit = 0,
                AutoReplenishment = true
            }));
    options.OnRejected = async (context, cancellationToken) =>
    {
        var problem = ApiProblems.Create(
            context.HttpContext,
            429,
            "rate_limit_exceeded",
            "Too many requests",
            "The control-plane request limit was exceeded.");
        await context.HttpContext.Response.WriteAsJsonAsync(problem, cancellationToken);
    };
});

var openTelemetry = builder.Services.AddOpenTelemetry()
    .ConfigureResource(resource => resource.AddService(
        serviceName: "semantic-data-nexus-control-api",
        serviceVersion: typeof(Program).Assembly.GetName().Version?.ToString()))
    .WithTracing(tracing => tracing
        .AddAspNetCoreInstrumentation(options =>
            options.Filter = context =>
                !context.Request.Path.StartsWithSegments("/health"))
        .AddHttpClientInstrumentation())
    .WithMetrics(metrics => metrics
        .AddAspNetCoreInstrumentation()
        .AddHttpClientInstrumentation()
        .AddRuntimeInstrumentation());

var otlpEndpoint = builder.Configuration["OpenTelemetry:Otlp:Endpoint"];
if (!string.IsNullOrWhiteSpace(otlpEndpoint) &&
    !Uri.TryCreate(otlpEndpoint, UriKind.Absolute, out _))
{
    throw new InvalidOperationException(
        "OpenTelemetry:Otlp:Endpoint must be an absolute URI when configured.");
}

if (Uri.TryCreate(otlpEndpoint, UriKind.Absolute, out var exporterEndpoint))
{
    openTelemetry
        .WithTracing(tracing => tracing.AddOtlpExporter(options =>
            options.Endpoint = exporterEndpoint))
        .WithMetrics(metrics => metrics.AddOtlpExporter(options =>
            options.Endpoint = exporterEndpoint));
    builder.Logging.AddOpenTelemetry(logging => logging.AddOtlpExporter(options =>
        options.Endpoint = exporterEndpoint));
}

var applicationInsightsConnectionString =
    builder.Configuration["ApplicationInsights:ConnectionString"];
if (!string.IsNullOrWhiteSpace(applicationInsightsConnectionString))
{
    openTelemetry.UseAzureMonitor(options =>
        options.ConnectionString = applicationInsightsConnectionString);
}

var app = builder.Build();

app.UseForwardedHeaders();
if (app.Environment.IsProduction())
{
    app.UseHsts();
    app.UseHttpsRedirection();
}

app.UseMiddleware<CorrelationMiddleware>();
app.UseExceptionHandler();
app.UseAuthentication();
app.UseRateLimiter();
app.UseAuthorization();

if (app.Environment.IsDevelopment() ||
    builder.Configuration.GetValue<bool>("OpenApi:Enabled"))
{
    app.UseSwagger();
    app.UseSwaggerUI();
}

app.MapHealthChecks("/health/live", new()
{
    Predicate = check => check.Tags.Contains("live")
});
app.MapHealthChecks("/health/ready", new()
{
    Predicate = check => check.Tags.Contains("ready")
});
app.MapControlApi();

app.Run();

public partial class Program;
