using Npgsql;

namespace ControlApi.Authentication;

public sealed record WorkspaceMembership(string TenantId, string WorkspaceId, string Name,
    string MembershipId, long Revision, IReadOnlyList<string> Permissions);

public sealed class IdentityStore(NpgsqlDataSource dataSource)
{
    public const long AuthorizationLock = 731320032;

    public async Task<IReadOnlyList<WorkspaceMembership>> ListAsync(
        VerifiedIdentity identity, CancellationToken cancellationToken)
    {
        await using var command = dataSource.CreateCommand("""
            SELECT tenant_id, workspace_id, name, membership_id, revision, permissions
            FROM identity_access WHERE issuer = $1 AND subject = $2 AND identity_tenant = $3
            ORDER BY tenant_id, workspace_id
            """);
        command.Parameters.AddWithValue(identity.Issuer);
        command.Parameters.AddWithValue(identity.Subject);
        command.Parameters.AddWithValue(identity.IdentityTenant);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        var memberships = new List<WorkspaceMembership>();
        while (await reader.ReadAsync(cancellationToken))
        {
            memberships.Add(new(reader.GetString(0), reader.GetString(1), reader.GetString(2),
                reader.GetString(3), reader.GetInt64(4), reader.GetFieldValue<string[]>(5)));
        }
        return memberships;
    }

    public async Task<TrustedContext> ResolveAsync(
        VerifiedIdentity identity, string workspaceId, CancellationToken cancellationToken)
    {
        await using var command = dataSource.CreateCommand("""
            SELECT principal_id, tenant_id, membership_id, revision, permissions
            FROM identity_access
            WHERE issuer = $1 AND subject = $2 AND identity_tenant = $3 AND workspace_id = $4
            """);
        command.Parameters.AddWithValue(identity.Issuer);
        command.Parameters.AddWithValue(identity.Subject);
        command.Parameters.AddWithValue(identity.IdentityTenant);
        command.Parameters.AddWithValue(workspaceId);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        if (!await reader.ReadAsync(cancellationToken))
        {
            throw new IdentityAccessException();
        }
        return new("trusted-context/v1", new(reader.GetString(1), workspaceId),
            new(reader.GetString(0), identity.Issuer, identity.Subject), identity.Authentication,
            new(reader.GetString(2), reader.GetInt64(3), DateTimeOffset.UtcNow,
                reader.GetFieldValue<string[]>(4)));
    }

    // All identity administration takes the exclusive version of this transaction lock.
    // It fences revocation against a control-store operation, not against long-running work.
    public static async Task ReauthorizeAsync(
        NpgsqlConnection connection, NpgsqlTransaction transaction, TrustedContext context,
        string permission, CancellationToken cancellationToken)
    {
        if (context.Authentication.ExpiresAt <= DateTimeOffset.UtcNow)
        {
            throw new IdentityAccessException();
        }
        await using var gate = new NpgsqlCommand(
            "SELECT pg_advisory_xact_lock_shared($1)", connection, transaction);
        gate.Parameters.AddWithValue(AuthorizationLock);
        await gate.ExecuteNonQueryAsync(cancellationToken);
        await using var command = new NpgsqlCommand("""
            SELECT 1 FROM identity_access
            WHERE principal_id = $1 AND issuer = $2 AND subject = $3
              AND tenant_id = $4 AND workspace_id = $5 AND membership_id = $6
              AND revision = $7 AND $8 = ANY(permissions)
            """, connection, transaction);
        command.Parameters.AddWithValue(context.Principal.PrincipalId);
        command.Parameters.AddWithValue(context.Principal.Issuer);
        command.Parameters.AddWithValue(context.Principal.Subject);
        command.Parameters.AddWithValue(context.Scope.TenantId);
        command.Parameters.AddWithValue(context.Scope.WorkspaceId);
        command.Parameters.AddWithValue(context.Membership.MembershipId);
        command.Parameters.AddWithValue(context.Membership.Revision);
        command.Parameters.AddWithValue(permission);
        if (await command.ExecuteScalarAsync(cancellationToken) is null)
        {
            throw new IdentityAccessException();
        }
    }
}
