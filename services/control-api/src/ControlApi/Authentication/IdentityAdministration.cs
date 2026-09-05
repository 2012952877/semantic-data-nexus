using System.Security.Cryptography;
using System.Text.Json;
using Npgsql;
using NpgsqlTypes;
using ControlApi.Persistence;

namespace ControlApi.Authentication;

public sealed record MembershipChange(string RequestId, string MembershipId, string Provider, string Subject, string? Role, bool Active);
public sealed record GroupChange(string RequestId, string GroupId, string Name, string Role, bool Active, string[] MembershipIds);
public sealed record CatalogAccessIds(string[] EntityIds, string[] FieldIds, string[] MetricIds, string[] RelationIds, string[] MemberIds);
public sealed record GrantChange(string RequestId, string GrantId, string? MembershipId, string? GroupId,
    string ResourceKind, string ResourceId, long Revision, string ContentSha256, string Permission, CatalogAccessIds AllowedIds, bool Active);

public sealed class IdentityAdministration(NpgsqlDataSource dataSource, IReadOnlyList<IdentityProvider> providers)
{
    internal static bool ValidId(string? value) => value is { Length: > 0 and <= 128 } &&
        char.IsAsciiLetterOrDigit(value[0]) && value.All(c => char.IsAsciiLetterOrDigit(c) || c is '.' or '_' or ':' or '-');

    private static void Validate(bool condition)
    {
        if (!condition)
        {
            throw new BadHttpRequestException("Invalid identity change.");
        }
    }

    public Task MembershipAsync(TrustedContext context, MembershipChange change, CancellationToken ct)
    {
        var provider = providers.SingleOrDefault(p => p.Name == change.Provider);
        Validate(provider is not null && ValidId(change.MembershipId) &&
            change.Subject is { Length: > 0 and <= 255 } && !change.Subject.Any(char.IsWhiteSpace) &&
            change.Role is null or "reader" or "contributor" or "admin");
        return Change(context, change.RequestId, "membership", change.MembershipId, change, async (connection, transaction) =>
        {
            await using var principal = new NpgsqlCommand("""
                INSERT INTO identity_principals (principal_id, issuer, subject, identity_tenant)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (issuer, subject, identity_tenant) DO UPDATE SET issuer = EXCLUDED.issuer
                RETURNING principal_id
                """, connection, transaction);
            principal.Parameters.AddWithValue($"principal-{Guid.NewGuid():N}");
            principal.Parameters.AddWithValue(provider!.Authority);
            principal.Parameters.AddWithValue(change.Subject);
            principal.Parameters.AddWithValue(provider.TenantId ?? "");
            var principalId = (string)(await principal.ExecuteScalarAsync(ct))!;
            await using var command = new NpgsqlCommand("""
                INSERT INTO identity_memberships (membership_id, workspace_id, principal_id, role, active)
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (membership_id) DO UPDATE SET role = EXCLUDED.role, active = EXCLUDED.active
                WHERE identity_memberships.workspace_id = EXCLUDED.workspace_id
                  AND identity_memberships.principal_id = EXCLUDED.principal_id
                """, connection, transaction);
            command.Parameters.AddWithValue(change.MembershipId);
            command.Parameters.AddWithValue(context.Scope.WorkspaceId);
            command.Parameters.AddWithValue(principalId);
            command.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)change.Role ?? DBNull.Value);
            command.Parameters.AddWithValue(change.Active);
            Validate(await command.ExecuteNonQueryAsync(ct) == 1);
        }, ct);
    }

    public Task GroupAsync(TrustedContext context, GroupChange change, CancellationToken ct)
    {
        Validate(ValidId(change.GroupId) && change.Name is { Length: > 0 and <= 128 } &&
            change.Role is "reader" or "contributor" or "admin" &&
            change.MembershipIds is { Length: <= 1000 } && change.MembershipIds.All(ValidId) &&
            change.MembershipIds.Distinct(StringComparer.Ordinal).Count() == change.MembershipIds.Length);
        return Change(context, change.RequestId, "group", change.GroupId, change, async (connection, transaction) =>
        {
            await using var command = new NpgsqlCommand("""
                INSERT INTO identity_groups (group_id, workspace_id, name, role, active) VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (group_id) DO UPDATE SET name = EXCLUDED.name, role = EXCLUDED.role, active = EXCLUDED.active
                WHERE identity_groups.workspace_id = EXCLUDED.workspace_id
                """, connection, transaction);
            command.Parameters.AddWithValue(change.GroupId);
            command.Parameters.AddWithValue(context.Scope.WorkspaceId);
            command.Parameters.AddWithValue(change.Name);
            command.Parameters.AddWithValue(change.Role);
            command.Parameters.AddWithValue(change.Active);
            Validate(await command.ExecuteNonQueryAsync(ct) == 1);
            await using var remove = new NpgsqlCommand(
                "DELETE FROM identity_group_members WHERE workspace_id = $1 AND group_id = $2", connection, transaction);
            remove.Parameters.AddWithValue(context.Scope.WorkspaceId);
            remove.Parameters.AddWithValue(change.GroupId);
            await remove.ExecuteNonQueryAsync(ct);
            await using var members = new NpgsqlCommand("""
                INSERT INTO identity_group_members (workspace_id, group_id, membership_id)
                SELECT $1, $2, unnest($3::text[])
                """, connection, transaction);
            members.Parameters.AddWithValue(context.Scope.WorkspaceId);
            members.Parameters.AddWithValue(change.GroupId);
            members.Parameters.AddWithValue(change.MembershipIds);
            await members.ExecuteNonQueryAsync(ct);
        }, ct);
    }

    public Task GrantAsync(TrustedContext context, GrantChange change, CancellationToken ct)
    {
        Validate(ValidId(change.GrantId) && ValidId(change.ResourceId) && change.Revision > 0 &&
            change.ResourceKind is "ontology" or "knowledge-base" or "resolver" or "credential-reference" or "llm" or
                "prompt-template" or "compute-engine" or "result-store" or "run" or "dataset" or "user" or "group" or "workspace" or "policy" &&
            (change.MembershipId is null) != (change.GroupId is null) &&
            (change.MembershipId is null || ValidId(change.MembershipId)) &&
            (change.GroupId is null || ValidId(change.GroupId)) &&
            change.ContentSha256 is { Length: 64 } && change.ContentSha256.All(c => char.IsAsciiDigit(c) || c is >= 'a' and <= 'f') &&
            change.Permission is "resource:read" or "compiler:query" && change.AllowedIds is not null);
        var ids = change.AllowedIds!;
        Validate(new[] { ids.EntityIds, ids.FieldIds, ids.MetricIds, ids.RelationIds, ids.MemberIds }
            .All(list => list is { Length: <= 10000 } && list.All(ValidId) && list.Distinct(StringComparer.Ordinal).Count() == list.Length));
        return Change(context, change.RequestId, "grant", change.GrantId, change, async (connection, transaction) =>
        {
            await using var command = new NpgsqlCommand("""
                INSERT INTO identity_resource_grants
                    (grant_id, workspace_id, membership_id, group_id, resource_kind, resource_id, revision, content_sha256, permission, allowed_ids, active)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                ON CONFLICT (grant_id) DO UPDATE SET membership_id = EXCLUDED.membership_id, group_id = EXCLUDED.group_id,
                    resource_kind = EXCLUDED.resource_kind, resource_id = EXCLUDED.resource_id, revision = EXCLUDED.revision,
                    content_sha256 = EXCLUDED.content_sha256, permission = EXCLUDED.permission,
                    allowed_ids = EXCLUDED.allowed_ids, active = EXCLUDED.active
                WHERE identity_resource_grants.workspace_id = EXCLUDED.workspace_id
                """, connection, transaction);
            command.Parameters.AddWithValue(change.GrantId);
            command.Parameters.AddWithValue(context.Scope.WorkspaceId);
            command.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)change.MembershipId ?? DBNull.Value);
            command.Parameters.AddWithValue(NpgsqlDbType.Text, (object?)change.GroupId ?? DBNull.Value);
            command.Parameters.AddWithValue(change.ResourceKind);
            command.Parameters.AddWithValue(change.ResourceId);
            command.Parameters.AddWithValue(change.Revision);
            command.Parameters.AddWithValue(change.ContentSha256);
            command.Parameters.AddWithValue(change.Permission);
            command.Parameters.AddWithValue(NpgsqlDbType.Jsonb, JsonSerializer.Serialize(ids, TrustedContext.Json));
            command.Parameters.AddWithValue(change.Active);
            Validate(await command.ExecuteNonQueryAsync(ct) == 1);
        }, ct);
    }

    private async Task Change<T>(TrustedContext context, string requestId, string action, string targetId, T change,
        Func<NpgsqlConnection, NpgsqlTransaction, Task> apply, CancellationToken ct)
    {
        Validate(ValidId(requestId));
        var digest = Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(change))).ToLowerInvariant();
        await using var connection = await dataSource.OpenConnectionAsync(ct);
        await using var transaction = await connection.BeginTransactionAsync(ct);
        await using (var gate = new NpgsqlCommand("SELECT pg_advisory_xact_lock($1)", connection, transaction))
        {
            gate.Parameters.AddWithValue(IdentityStore.AuthorizationLock);
            await gate.ExecuteNonQueryAsync(ct);
        }
        await IdentityStore.ReauthorizeAsync(connection, transaction, context, "workspace:admin", ct);
        await using var existing = new NpgsqlCommand("""
            SELECT payload_sha256 FROM identity_changes
            WHERE workspace_id = $1 AND actor_kind = 'oidc' AND actor_id = $2 AND request_id = $3
            """, connection, transaction);
        existing.Parameters.AddWithValue(context.Scope.WorkspaceId);
        existing.Parameters.AddWithValue(context.Principal.PrincipalId);
        existing.Parameters.AddWithValue(requestId);
        var previous = (string?)await existing.ExecuteScalarAsync(ct);
        if (previous is not null)
        {
            if (previous != digest)
            {
                throw new IdempotencyConflictException("Identity request ID is already bound to another change.");
            }
            IdentityStore.EnsureUnexpired(context);
            await transaction.CommitAsync(ct);
            return;
        }
        try
        {
            await apply(connection, transaction);
        }
        catch (PostgresException exception) when (exception.SqlState is PostgresErrorCodes.UniqueViolation or PostgresErrorCodes.ForeignKeyViolation)
        {
            throw new BadHttpRequestException("Identity change conflicts with the selected workspace.");
        }
        await using var record = new NpgsqlCommand("""
            WITH revised AS (
                UPDATE identity_workspaces SET revision = revision + 1 WHERE workspace_id = $1 RETURNING workspace_id
            )
            INSERT INTO identity_changes (workspace_id, actor_id, request_id, action, target_id, payload_sha256)
            SELECT workspace_id,$2,$3,$4,$5,$6 FROM revised
            """, connection, transaction);
        record.Parameters.AddWithValue(context.Scope.WorkspaceId);
        record.Parameters.AddWithValue(context.Principal.PrincipalId);
        record.Parameters.AddWithValue(requestId);
        record.Parameters.AddWithValue(action);
        record.Parameters.AddWithValue(targetId);
        record.Parameters.AddWithValue(digest);
        await record.ExecuteNonQueryAsync(ct);
        IdentityStore.EnsureUnexpired(context);
        await transaction.CommitAsync(ct);
    }

    public async Task<JsonElement> ListAsync(TrustedContext context, CancellationToken ct)
    {
        await using var connection = await dataSource.OpenConnectionAsync(ct);
        await using var transaction = await connection.BeginTransactionAsync(ct);
        await IdentityStore.ReauthorizeAsync(connection, transaction, context, "workspace:admin", ct);
        await using var command = new NpgsqlCommand("""
            SELECT jsonb_build_object(
                'memberships', (SELECT coalesce(jsonb_agg(to_jsonb(m)), '[]') FROM identity_memberships m WHERE workspace_id = $1),
                'groups', (SELECT coalesce(jsonb_agg(to_jsonb(g)), '[]') FROM identity_groups g WHERE workspace_id = $1),
                'groupMembers', (SELECT coalesce(jsonb_agg(to_jsonb(gm)), '[]') FROM identity_group_members gm WHERE workspace_id = $1),
                'grants', (SELECT coalesce(jsonb_agg(to_jsonb(g)), '[]') FROM identity_resource_grants g WHERE workspace_id = $1)
            )::text
            """, connection, transaction);
        command.Parameters.AddWithValue(context.Scope.WorkspaceId);
        var json = (string)(await command.ExecuteScalarAsync(ct))!;
        IdentityStore.EnsureUnexpired(context);
        await transaction.CommitAsync(ct);
        return JsonSerializer.Deserialize<JsonElement>(json);
    }
}
