using System.Globalization;
using System.Security.Claims;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ControlApi.Authentication;

public sealed record WorkspaceScope(string TenantId, string WorkspaceId);
public sealed record TrustedPrincipal(string PrincipalId, string Issuer, string Subject);
public sealed record VerifiedAuthentication(
    string Method, string Audience, DateTimeOffset AuthenticatedAt, DateTimeOffset ExpiresAt);
public sealed record AuthorizedMembership(
    string MembershipId, long Revision, DateTimeOffset AuthorizedAt, IReadOnlyList<string> Permissions);
public sealed record TrustedContext(
    string ContractVersion,
    WorkspaceScope Scope,
    TrustedPrincipal Principal,
    VerifiedAuthentication Authentication,
    AuthorizedMembership Membership)
{
    public static readonly JsonSerializerOptions Json = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters = { new ContextTimestampConverter() }
    };
}

internal sealed class ContextTimestampConverter : JsonConverter<DateTimeOffset>
{
    public override DateTimeOffset Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options) =>
        reader.GetDateTimeOffset();

    public override void Write(Utf8JsonWriter writer, DateTimeOffset value, JsonSerializerOptions options) =>
        writer.WriteStringValue(value.ToString("yyyy-MM-dd'T'HH:mm:ss.ffffffzzz", CultureInfo.InvariantCulture));
}

public sealed class IdentityAccessException() : Exception("Current workspace access is not authorized.");

public sealed class RunAccess
{
    internal const string ItemKey = "nexus.trusted-context";
    private readonly IHttpContextAccessor? http;
    private readonly TrustedContext? fixedContext;
    public bool Legacy { get; }

    public RunAccess(IHttpContextAccessor http, bool legacy)
    {
        this.http = http;
        Legacy = legacy;
    }

    private RunAccess(bool legacy, TrustedContext? context)
    {
        Legacy = legacy;
        fixedContext = context;
    }

    public static RunAccess LegacyDevelopment() => new(true, null);
    public static RunAccess Verified(TrustedContext context) => new(false, context);

    public TrustedContext? Context => Legacy ? null :
        fixedContext ?? http?.HttpContext?.Items[ItemKey] as TrustedContext ??
        throw new IdentityAccessException();

    public WorkspaceScope? Scope => Context?.Scope;
}

public sealed record VerifiedIdentity(
    string Issuer, string Subject, string IdentityTenant, VerifiedAuthentication Authentication)
{
    public static VerifiedIdentity FromPrincipal(ClaimsPrincipal principal)
    {
        var issuer = principal.FindFirstValue("iss");
        var subject = principal.FindFirstValue("sub");
        var audience = principal.FindFirstValue("nexus_audience");
        if (string.IsNullOrWhiteSpace(issuer) || string.IsNullOrWhiteSpace(subject) ||
            string.IsNullOrWhiteSpace(audience) ||
            !long.TryParse(principal.FindFirstValue("iat"), CultureInfo.InvariantCulture, out var issued) ||
            !long.TryParse(principal.FindFirstValue("exp"), CultureInfo.InvariantCulture, out var expires))
        {
            throw new IdentityAccessException();
        }
        var authentication = new VerifiedAuthentication("oidc", audience,
            DateTimeOffset.FromUnixTimeSeconds(issued), DateTimeOffset.FromUnixTimeSeconds(expires));
        if (authentication.ExpiresAt <= DateTimeOffset.UtcNow ||
            authentication.AuthenticatedAt > DateTimeOffset.UtcNow ||
            authentication.ExpiresAt <= authentication.AuthenticatedAt)
        {
            throw new IdentityAccessException();
        }
        return new(issuer, subject, principal.FindFirstValue("tid") ?? "", authentication);
    }
}
