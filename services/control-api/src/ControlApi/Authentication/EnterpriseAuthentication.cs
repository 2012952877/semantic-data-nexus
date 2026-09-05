using System.Security.Claims;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.Cookies;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Authentication.OpenIdConnect;
using Microsoft.AspNetCore.DataProtection;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using Microsoft.IdentityModel.Validators;

namespace ControlApi.Authentication;

public sealed class IdentityProvider
{
    public string Name { get; init; } = "";
    public string Authority { get; init; } = "";
    public string ClientId { get; init; } = "";
    public string ClientSecret { get; init; } = "";
    public string Audience { get; init; } = "";
    public string? TenantId { get; init; }
}

public static class EnterpriseAuthentication
{
    public const string SessionScheme = "NexusSession";

    public static IServiceCollection AddEnterpriseIdentity(
        this IServiceCollection services, IConfiguration configuration, IHostEnvironment environment)
    {
        var providers = configuration.GetSection("Identity:Providers").Get<IdentityProvider[]>() ?? [];
        if (providers.Length == 0 || providers.Select(p => p.Name).Distinct(StringComparer.Ordinal).Count() != providers.Length ||
            providers.Select(p => p.Authority).Distinct(StringComparer.Ordinal).Count() != providers.Length)
        {
            throw new InvalidOperationException("Identity:Providers requires uniquely named, explicitly registered issuers.");
        }
        foreach (var provider in providers)
        {
            if (provider.Name.Length is < 1 or > 32 || !provider.Name.All(char.IsAsciiLetterOrDigit) ||
                !Uri.TryCreate(provider.Authority, UriKind.Absolute, out var authority) ||
                authority.Scheme != "https" || authority.UserInfo.Length != 0 ||
                authority.Query.Length != 0 || authority.Fragment.Length != 0 ||
                provider.Authority.EndsWith('/') || string.IsNullOrWhiteSpace(provider.ClientId) ||
                string.IsNullOrWhiteSpace(provider.ClientSecret) || string.IsNullOrWhiteSpace(provider.Audience) ||
                provider.ClientId == provider.Audience ||
                authority.Segments.Any(segment => segment.Trim('/') is "common" or "organizations" or "consumers") ||
                (provider.TenantId is not null && (!Guid.TryParse(provider.TenantId, out _) ||
                    !provider.Authority.EndsWith($"/{provider.TenantId}/v2.0", StringComparison.Ordinal))))
            {
                throw new InvalidOperationException("Invalid registered identity provider. Require HTTPS, distinct browser/API audiences and explicit Entra tenant.");
            }
        }
        if (configuration["RunStorage:Provider"] != "Postgres")
        {
            throw new InvalidOperationException("Enterprise identity requires PostgreSQL membership and run storage.");
        }
        services.AddSingleton<IReadOnlyList<IdentityProvider>>(providers);
        services.AddSingleton<IdentityStore>();
        services.AddSingleton<IdentityAdministration>();
        services.AddSingleton(new ServiceContextSigner(configuration));
        services.AddTransient<ServiceContextHandler>();
        services.AddSingleton<PostgresTicketStore>();
        services.AddOptions<CookieAuthenticationOptions>(SessionScheme)
            .Configure<PostgresTicketStore>((options, store) => options.SessionStore = store);
        var protection = services.AddDataProtection().SetApplicationName("semantic-data-nexus-identity-v1");
        var keyRing = configuration["Identity:DataProtectionKeyRing"];
        if (string.IsNullOrWhiteSpace(keyRing) && !environment.IsDevelopment())
        {
            throw new InvalidOperationException("Enterprise identity requires a protected persistent Identity:DataProtectionKeyRing volume.");
        }
        if (!string.IsNullOrWhiteSpace(keyRing))
        {
            protection.PersistKeysToFileSystem(new DirectoryInfo(keyRing));
        }
        services.AddAntiforgery(options =>
        {
            options.HeaderName = "X-Nexus-CSRF";
            options.Cookie.Name = "__Host-nexus-csrf";
            options.Cookie.SecurePolicy = CookieSecurePolicy.Always;
            options.Cookie.HttpOnly = true;
            options.Cookie.SameSite = SameSiteMode.Strict;
        });
        var auth = services.AddAuthentication("Nexus")
            .AddPolicyScheme("Nexus", "OIDC session or registered access token", options =>
                options.ForwardDefaultSelector = context =>
                {
                    var bearer = context.Request.Headers.Authorization.ToString();
                    if (!bearer.StartsWith("Bearer ", StringComparison.OrdinalIgnoreCase))
                    {
                        return SessionScheme;
                    }
                    var token = bearer[7..];
                    var handler = new JsonWebTokenHandler();
                    // Routing is not verification. Only a configured handler can authenticate.
                    if (token.Length <= 16384 && handler.CanReadToken(token))
                    {
                        var issuer = handler.ReadJsonWebToken(token).Issuer;
                        var registered = providers.FirstOrDefault(p => p.Authority == issuer);
                        if (registered is not null)
                        {
                            return $"access-{registered.Name}";
                        }
                    }
                    return $"access-{providers[0].Name}";
                })
            .AddCookie(SessionScheme, options =>
            {
                options.Cookie.Name = "__Host-nexus-session";
                options.Cookie.Path = "/";
                options.Cookie.HttpOnly = true;
                options.Cookie.SecurePolicy = CookieSecurePolicy.Always;
                options.Cookie.SameSite = SameSiteMode.Lax;
                options.ExpireTimeSpan = TimeSpan.FromMinutes(30);
                options.SlidingExpiration = false;
                options.Events.OnRedirectToLogin = context =>
                {
                    context.Response.StatusCode = 401;
                    return Task.CompletedTask;
                };
                options.Events.OnRedirectToAccessDenied = context =>
                {
                    context.Response.StatusCode = 403;
                    return Task.CompletedTask;
                };
            });
        foreach (var provider in providers)
        {
            auth.AddOpenIdConnect($"oidc-{provider.Name}", options =>
            {
                options.SignInScheme = SessionScheme;
                options.Events = new OpenIdConnectEvents();
                options.Authority = provider.Authority;
                options.ClientId = provider.ClientId;
                options.ClientSecret = provider.ClientSecret;
                options.CallbackPath = $"/auth/callback/{provider.Name}";
                options.SignedOutCallbackPath = $"/auth/signed-out/{provider.Name}";
                options.ResponseType = OpenIdConnectResponseType.Code;
                options.ResponseMode = OpenIdConnectResponseMode.Query;
                options.UsePkce = true;
                options.UseTokenLifetime = true;
                options.SaveTokens = false;
                options.MapInboundClaims = false;
                options.RequireHttpsMetadata = true;
                options.Scope.Clear();
                options.Scope.Add("openid");
                options.Scope.Add("profile");
                options.ClaimActions.Clear();
                options.TokenValidationParameters = Validation(provider, provider.ClientId);
                options.Events.OnTokenValidated = async context =>
                {
                    var metadata = await context.Options.ConfigurationManager!.GetConfigurationAsync(context.HttpContext.RequestAborted);
                    var principal = context.Principal ?? throw new IdentityAccessException();
                    ValidateClaims(principal, provider, metadata.Issuer, provider.ClientId);
                    var expiry = VerifiedIdentity.FromPrincipal(principal).Authentication.ExpiresAt;
                    context.Properties!.ExpiresUtc = DateTimeOffset.UtcNow.AddMinutes(30) < expiry
                        ? DateTimeOffset.UtcNow.AddMinutes(30) : expiry;
                };
                options.Events.OnRemoteFailure = context =>
                {
                    context.HandleResponse();
                    context.Response.Redirect("/?authentication=failed");
                    return Task.CompletedTask;
                };
            });
            auth.AddJwtBearer($"access-{provider.Name}", options =>
            {
                options.Authority = provider.Authority;
                options.Audience = provider.Audience;
                options.Events = new JwtBearerEvents();
                options.MapInboundClaims = false;
                options.RequireHttpsMetadata = true;
                options.TokenValidationParameters = Validation(provider, provider.Audience);
                options.Events.OnTokenValidated = async context =>
                {
                    var metadata = await context.Options.ConfigurationManager!.GetConfigurationAsync(context.HttpContext.RequestAborted);
                    var principal = context.Principal ?? throw new IdentityAccessException();
                    ValidateClaims(principal, provider, metadata.Issuer, provider.Audience);
                    if (provider.TenantId is not null &&
                        (principal.FindFirstValue("ver") != "2.0" ||
                         !principal.FindAll("scp").Any(c => c.Value.Split(' ').Contains("access_as_user", StringComparer.Ordinal))))
                    {
                        context.Fail("A delegated API access token is required.");
                    }
                };
            });
        }
        services.AddAuthorization(options =>
        {
            foreach (var policy in new[] { Policies.Reader, Policies.Contributor, Policies.Admin, Policies.Compiler, "workspace:admin" })
            {
                options.AddPolicy(policy, rule => rule.RequireAuthenticatedUser().RequireAssertion(context =>
                    context.Resource is HttpContext http &&
                    http.Items[RunAccess.ItemKey] is TrustedContext trusted &&
                    trusted.Membership.Permissions.Contains(policy, StringComparer.Ordinal)));
            }
        });
        return services;
    }

    internal static TokenValidationParameters Validation(IdentityProvider provider, string audience)
    {
        var parameters = new TokenValidationParameters
        {
            ValidateIssuer = true,
            ValidIssuer = provider.Authority,
            ValidateAudience = true,
            ValidAudience = audience,
            ValidateLifetime = true,
            RequireExpirationTime = true,
            RequireSignedTokens = true,
            ValidateIssuerSigningKey = true,
            ValidAlgorithms = [SecurityAlgorithms.RsaSha256],
            ClockSkew = TimeSpan.Zero,
            NameClaimType = "name"
        };
        if (provider.TenantId is not null)
        {
            parameters.EnableAadSigningKeyIssuerValidation();
        }
        return parameters;
    }

    private static void ValidateClaims(ClaimsPrincipal principal, IdentityProvider provider, string discoveryIssuer, string audience)
    {
        if (discoveryIssuer != provider.Authority || principal.FindFirstValue("iss") != provider.Authority ||
            (provider.TenantId is not null && principal.FindFirstValue("tid") != provider.TenantId) ||
            (provider.TenantId is null && principal.HasClaim(c => c.Type == "tid")))
        {
            throw new SecurityTokenInvalidIssuerException("Issuer and tenant must match the registered provider.");
        }
        var identity = (ClaimsIdentity)principal.Identity!;
        foreach (var claim in identity.FindAll("nexus_audience").ToArray())
        {
            identity.RemoveClaim(claim);
        }
        identity.AddClaim(new Claim("nexus_audience", audience));
        _ = VerifiedIdentity.FromPrincipal(principal);
    }
}
