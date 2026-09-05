using Microsoft.AspNetCore.Antiforgery;
using Microsoft.AspNetCore.Authentication;

namespace ControlApi.Authentication;

public sealed class WorkspaceMiddleware(RequestDelegate next)
{
    public async Task InvokeAsync(HttpContext context, RunAccess access)
    {
        if (access.Legacy)
        {
            await next(context);
            return;
        }
        if (context.Request.Path.StartsWithSegments("/api") || context.Request.Path.StartsWithSegments("/auth"))
        {
            context.Response.Headers.CacheControl = "no-store";
        }
        if (context.User.Identity?.IsAuthenticated == true)
        {
            // A browser cookie never substitutes for an anti-forgery token on mutation.
            var session = await context.AuthenticateAsync(EnterpriseAuthentication.SessionScheme);
            if (session.Succeeded && !HttpMethods.IsGet(context.Request.Method) && !HttpMethods.IsHead(context.Request.Method))
            {
                try
                {
                    await context.RequestServices.GetRequiredService<IAntiforgery>().ValidateRequestAsync(context);
                }
                catch (AntiforgeryValidationException)
                {
                    context.Response.StatusCode = 400;
                    await context.Response.WriteAsJsonAsync(ApiProblems.Create(context, 400, "csrf_required",
                        "Invalid request", "A valid anti-forgery token is required."));
                    return;
                }
            }
            if (context.Request.Path.StartsWithSegments("/api"))
            {
                var selector = context.Request.Headers["X-Workspace-Id"];
                if (selector.Count != 1 || string.IsNullOrWhiteSpace(selector[0]) || selector[0]!.Length > 128)
                {
                    throw new IdentityAccessException();
                }
                context.Items[RunAccess.ItemKey] = await context.RequestServices.GetRequiredService<IdentityStore>()
                    .ResolveAsync(VerifiedIdentity.FromPrincipal(context.User), selector[0]!, context.RequestAborted);
            }
        }
        await next(context);
    }
}
