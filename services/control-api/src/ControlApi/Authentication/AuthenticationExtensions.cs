using System.Security.Claims;
using System.Text.Encodings.Web;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Authorization;
using Microsoft.AspNetCore.Authorization.Policy;
using Microsoft.AspNetCore.Mvc;
using Microsoft.Extensions.Options;
using Microsoft.Identity.Web;

namespace ControlApi.Authentication;

public static class Policies
{
    public const string Reader = "run.reader";
    public const string Contributor = "run.contributor";
    public const string Admin = "run.admin";
}

public sealed class LocalDevelopmentAuthOptions
{
    public const string SectionName = "LocalDevelopmentAuth";
    public bool Enabled { get; init; }
}

public sealed class EntraBoundaryOptions
{
    public string? Instance { get; init; }
    public string? TenantId { get; init; }
    public string? ClientId { get; init; }
}

public static class AuthenticationExtensions
{
    public const string DevelopmentScheme = "LocalDevelopment";

    public static IServiceCollection AddControlApiAuthentication(
        this IServiceCollection services,
        IConfiguration configuration,
        IHostEnvironment environment)
    {
        var localOptions = configuration
            .GetSection(LocalDevelopmentAuthOptions.SectionName)
            .Get<LocalDevelopmentAuthOptions>() ?? new LocalDevelopmentAuthOptions();

        if (!environment.IsDevelopment() && localOptions.Enabled)
        {
            throw new InvalidOperationException(
                "Local development authentication can only be enabled in Development.");
        }

        if (localOptions.Enabled)
        {
            services
                .AddAuthentication(DevelopmentScheme)
                .AddScheme<AuthenticationSchemeOptions, LocalDevelopmentAuthenticationHandler>(
                    DevelopmentScheme,
                    _ => { });
        }
        else
        {
            if (environment.IsProduction())
            {
                var entra = configuration.GetSection("AzureAd").Get<EntraBoundaryOptions>();
                if (entra is null ||
                    string.IsNullOrWhiteSpace(entra.Instance) ||
                    string.IsNullOrWhiteSpace(entra.TenantId) ||
                    string.IsNullOrWhiteSpace(entra.ClientId))
                {
                    throw new InvalidOperationException(
                        "Production requires AzureAd:Instance, AzureAd:TenantId, and AzureAd:ClientId.");
                }
            }

            services
                .AddAuthentication(JwtBearerDefaults.AuthenticationScheme)
                .AddMicrosoftIdentityWebApi(
                    configuration.GetSection("AzureAd"),
                    jwtBearerScheme: JwtBearerDefaults.AuthenticationScheme);
            services.PostConfigure<JwtBearerOptions>(
                JwtBearerDefaults.AuthenticationScheme,
                options => options.MapInboundClaims = false);
        }

        services.AddAuthorization(options =>
        {
            options.AddPolicy(Policies.Reader, policy =>
                policy.RequireAuthenticatedUser().RequireAssertion(context =>
                    HasAnyPermission(context.User, "reader", "contributor", "admin")));
            options.AddPolicy(Policies.Contributor, policy =>
                policy.RequireAuthenticatedUser().RequireAssertion(context =>
                    HasAnyPermission(context.User, "contributor", "admin")));
            options.AddPolicy(Policies.Admin, policy =>
                policy.RequireAuthenticatedUser().RequireAssertion(context =>
                    HasAnyPermission(context.User, "admin")));
        });
        services.AddSingleton<IAuthorizationMiddlewareResultHandler, ProblemAuthorizationResultHandler>();
        return services;
    }

    private static bool HasAnyPermission(ClaimsPrincipal principal, params string[] permissions)
    {
        var roles = principal.FindAll("roles")
            .Concat(principal.FindAll(ClaimTypes.Role))
            .Select(claim => claim.Value);
        var scopes = principal.FindAll("scp")
            .SelectMany(claim => claim.Value.Split(' ', StringSplitOptions.RemoveEmptyEntries));
        return roles.Concat(scopes).Any(value =>
            permissions.Contains(value, StringComparer.OrdinalIgnoreCase));
    }
}

public sealed class LocalDevelopmentAuthenticationHandler(
    IOptionsMonitor<AuthenticationSchemeOptions> options,
    ILoggerFactory logger,
    UrlEncoder encoder)
    : AuthenticationHandler<AuthenticationSchemeOptions>(options, logger, encoder)
{
    protected override Task<AuthenticateResult> HandleAuthenticateAsync()
    {
        if (!Request.Headers.TryGetValue("X-Dev-Subject", out var subject) ||
            string.IsNullOrWhiteSpace(subject))
        {
            return Task.FromResult(AuthenticateResult.NoResult());
        }

        var claims = new List<Claim>
        {
            new(ClaimTypes.NameIdentifier, subject.ToString()),
            new("sub", subject.ToString())
        };
        if (Request.Headers.TryGetValue("X-Dev-Name", out var displayName) &&
            !string.IsNullOrWhiteSpace(displayName))
        {
            claims.Add(new Claim(ClaimTypes.Name, displayName.ToString()));
        }

        var roles = Request.Headers.TryGetValue("X-Dev-Roles", out var roleHeader)
            ? roleHeader.ToString().Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
            : ["reader"];
        claims.AddRange(roles.Select(role => new Claim("roles", role)));

        var identity = new ClaimsIdentity(claims, Scheme.Name, ClaimTypes.Name, "roles");
        var ticket = new AuthenticationTicket(new ClaimsPrincipal(identity), Scheme.Name);
        return Task.FromResult(AuthenticateResult.Success(ticket));
    }
}

public sealed class ProblemAuthorizationResultHandler : IAuthorizationMiddlewareResultHandler
{
    private readonly AuthorizationMiddlewareResultHandler defaultHandler = new();

    public async Task HandleAsync(
        RequestDelegate next,
        HttpContext context,
        AuthorizationPolicy policy,
        PolicyAuthorizationResult authorizeResult)
    {
        if (authorizeResult.Succeeded)
        {
            await next(context);
            return;
        }

        if (authorizeResult.Forbidden || authorizeResult.Challenged)
        {
            var status = authorizeResult.Forbidden
                ? StatusCodes.Status403Forbidden
                : StatusCodes.Status401Unauthorized;
            var code = authorizeResult.Forbidden ? "authorization_denied" : "authentication_required";
            var problem = ApiProblems.Create(
                context,
                status,
                code,
                authorizeResult.Forbidden ? "Forbidden" : "Unauthorized",
                authorizeResult.Forbidden
                    ? "The current principal does not satisfy the required policy."
                    : "A valid bearer token is required.");
            context.Response.StatusCode = status;
            await context.Response.WriteAsJsonAsync(problem);
            return;
        }

        await defaultHandler.HandleAsync(next, context, policy, authorizeResult);
    }
}
