using System.Security.Claims;
using Microsoft.AspNetCore.Antiforgery;
using Microsoft.AspNetCore.Authentication;

namespace ControlApi.Authentication;

public static class IdentityEndpoints
{
    public static IEndpointRouteBuilder MapIdentityEndpoints(this IEndpointRouteBuilder endpoints)
    {
        var legacy = endpoints.ServiceProvider.GetRequiredService<RunAccess>().Legacy;
        endpoints.MapGet("/auth/session", async (HttpContext http) =>
        {
            if (legacy)
            {
                return Results.Ok(new { mode = "legacy-development", authenticated = false });
            }
            var providers = http.RequestServices.GetRequiredService<IReadOnlyList<IdentityProvider>>();
            if (http.User.Identity?.IsAuthenticated != true)
            {
                return Results.Ok(new { mode = "oidc", authenticated = false, providers = providers.Select(p => p.Name) });
            }
            var memberships = await http.RequestServices.GetRequiredService<IdentityStore>()
                .ListAsync(VerifiedIdentity.FromPrincipal(http.User), http.RequestAborted);
            var csrf = http.RequestServices.GetRequiredService<IAntiforgery>().GetAndStoreTokens(http);
            return Results.Ok(new
            {
                mode = "oidc",
                authenticated = true,
                name = http.User.FindFirstValue("name") ?? "Signed-in account",
                workspaces = memberships,
                csrfToken = csrf.RequestToken
            });
        });
        if (legacy)
        {
            return endpoints;
        }
        endpoints.MapGet("/auth/login/{provider}", (string provider, IReadOnlyList<IdentityProvider> providers) =>
            providers.Any(p => p.Name == provider)
                ? Results.Challenge(new AuthenticationProperties { RedirectUri = "/" }, [$"oidc-{provider}"])
                : Results.NotFound());
        endpoints.MapPost("/auth/logout", async (HttpContext http) =>
        {
            await http.SignOutAsync(EnterpriseAuthentication.SessionScheme);
            return Results.NoContent();
        }).RequireAuthorization();
        var admin = endpoints.MapGroup("/api/v1/workspace").RequireAuthorization("workspace:admin");
        admin.MapPut("/memberships", async (MembershipChange change, RunAccess access, IdentityAdministration administration, CancellationToken ct) =>
        {
            await administration.MembershipAsync(access.Context!, change, ct);
            return Results.NoContent();
        });
        admin.MapPut("/groups", async (GroupChange change, RunAccess access, IdentityAdministration administration, CancellationToken ct) =>
        {
            await administration.GroupAsync(access.Context!, change, ct);
            return Results.NoContent();
        });
        admin.MapPut("/grants", async (GrantChange change, RunAccess access, IdentityAdministration administration, CancellationToken ct) =>
        {
            await administration.GrantAsync(access.Context!, change, ct);
            return Results.NoContent();
        });
        admin.MapGet("/access", async (RunAccess access, IdentityAdministration administration, CancellationToken ct) =>
            Results.Ok(await administration.ListAsync(access.Context!, ct)));
        return endpoints;
    }
}
