using System.Security.Cryptography;
using System.Text.Json;
using ControlApi.Persistence;
using Npgsql;

namespace ControlApi.Authentication;

// Operator-only CLI: never mapped to a public endpoint or run automatically at startup.
public static class IdentityMaintenance
{
    public static async Task ExecuteAsync(IConfiguration configuration, TextReader inputReader, CancellationToken ct)
    {
        var settings = new StorageSettings(configuration);
        if (!settings.IsPostgres)
        {
            throw new StorageConfigurationException("Identity maintenance requires Postgres.");
        }
        var input = await inputReader.ReadToEndAsync(ct);
        var change = JsonSerializer.Deserialize<MaintenanceChange>(input) ??
            throw new InvalidOperationException("Maintenance JSON is required on stdin.");
        if (!IdentityAdministration.ValidId(change.RequestId) || !IdentityAdministration.ValidId(change.WorkspaceId) ||
            !IdentityAdministration.ValidId(change.TenantId) || !IdentityAdministration.ValidId(change.PrincipalId) ||
            !IdentityAdministration.ValidId(change.MembershipId) || change.Subject.Length is < 1 or > 255 ||
            change.Subject.Any(char.IsWhiteSpace) || change.Name.Length is < 1 or > 128)
        {
            throw new InvalidOperationException("Invalid identity maintenance request.");
        }
        var provider = (configuration.GetSection("Identity:Providers").Get<IdentityProvider[]>() ?? [])
            .Single(p => p.Name == change.Provider);
        if (!Uri.TryCreate(provider.Authority, UriKind.Absolute, out var authority) || authority.Scheme != "https")
        {
            throw new InvalidOperationException("A registered HTTPS issuer is required.");
        }
        await using var source = NpgsqlDataSource.Create(settings.ConnectionString!);
        await new PostgresMigrations(source).ApplyAsync(ct);
        await using var connection = await source.OpenConnectionAsync(ct);
        await using var transaction = await connection.BeginTransactionAsync(ct);
        await using (var gate = new NpgsqlCommand("SELECT pg_advisory_xact_lock($1)", connection, transaction))
        {
            gate.Parameters.AddWithValue(IdentityStore.AuthorizationLock);
            await gate.ExecuteNonQueryAsync(ct);
        }
        var digest = Convert.ToHexString(SHA256.HashData(JsonSerializer.SerializeToUtf8Bytes(change))).ToLowerInvariant();
        await using var previous = new NpgsqlCommand("""
            SELECT payload_sha256 FROM identity_changes WHERE workspace_id = $1 AND actor_id = $2 AND request_id = $3
            """, connection, transaction);
        previous.Parameters.AddWithValue(change.WorkspaceId);
        previous.Parameters.AddWithValue(change.PrincipalId);
        previous.Parameters.AddWithValue(change.RequestId);
        var saved = (string?)await previous.ExecuteScalarAsync(ct);
        if (saved is not null)
        {
            if (saved != digest)
            {
                throw new IdempotencyConflictException("Maintenance request ID conflict.");
            }
            await transaction.CommitAsync(ct);
            return;
        }
        if (change.Operation == "bootstrap")
        {
            await using var command = new NpgsqlCommand("""
                WITH principal AS (
                    INSERT INTO identity_principals (principal_id, issuer, subject, identity_tenant)
                    VALUES ($1,$2,$3,$4)
                    ON CONFLICT (principal_id) DO UPDATE SET issuer = identity_principals.issuer
                    WHERE identity_principals.issuer = EXCLUDED.issuer AND identity_principals.subject = EXCLUDED.subject
                      AND identity_principals.identity_tenant = EXCLUDED.identity_tenant AND identity_principals.active
                    RETURNING principal_id
                ), workspace AS (
                    INSERT INTO identity_workspaces (workspace_id, tenant_id, name) VALUES ($5,$6,$7) RETURNING workspace_id
                )
                INSERT INTO identity_memberships (membership_id, workspace_id, principal_id, role)
                SELECT $8,workspace_id,principal_id,'admin' FROM principal CROSS JOIN workspace
                """, connection, transaction);
            command.Parameters.AddWithValue(change.PrincipalId);
            command.Parameters.AddWithValue(provider.Authority);
            command.Parameters.AddWithValue(change.Subject);
            command.Parameters.AddWithValue(provider.TenantId ?? "");
            command.Parameters.AddWithValue(change.WorkspaceId);
            command.Parameters.AddWithValue(change.TenantId);
            command.Parameters.AddWithValue(change.Name);
            command.Parameters.AddWithValue(change.MembershipId);
            if (await command.ExecuteNonQueryAsync(ct) != 1)
            {
                throw new InvalidOperationException("Bootstrap identity preconditions were not met.");
            }
        }
        else if (change.Operation == "adopt-legacy")
        {
            if (!Domain.RunId.TryParse(change.RunId, null, out _) || string.IsNullOrWhiteSpace(change.LegacySubject))
            {
                throw new InvalidOperationException("Adoption requires an explicit run ID and expected legacy subject.");
            }
            await using var adopt = new NpgsqlCommand("""
                UPDATE control_runs SET tenant_id = $1, workspace_id = $2, subject = $3,
                    metadata = jsonb_set(metadata, '{CreatedBy}', to_jsonb($3::text))
                WHERE run_id = $4 AND tenant_id IS NULL AND workspace_id IS NULL AND subject = $5
                  AND EXISTS (SELECT 1 FROM identity_access WHERE principal_id = $3
                    AND tenant_id = $1 AND workspace_id = $2 AND issuer = $6 AND subject = $7)
                """, connection, transaction);
            adopt.Parameters.AddWithValue(change.TenantId);
            adopt.Parameters.AddWithValue(change.WorkspaceId);
            adopt.Parameters.AddWithValue(change.PrincipalId);
            adopt.Parameters.AddWithValue(change.RunId!);
            adopt.Parameters.AddWithValue(change.LegacySubject);
            adopt.Parameters.AddWithValue(provider.Authority);
            adopt.Parameters.AddWithValue(change.Subject);
            if (await adopt.ExecuteNonQueryAsync(ct) != 1)
            {
                throw new InvalidOperationException("Legacy adoption preconditions were not met.");
            }
        }
        else if (change.Operation == "revoke-principal")
        {
            await using var revoke = new NpgsqlCommand("""
                WITH revoked AS (
                    UPDATE identity_principals SET active = false
                    WHERE principal_id = $1 AND issuer = $2 AND subject = $3 RETURNING principal_id
                )
                UPDATE identity_workspaces SET revision = revision + 1 WHERE workspace_id IN
                    (SELECT workspace_id FROM identity_memberships JOIN revoked USING (principal_id))
                """, connection, transaction);
            revoke.Parameters.AddWithValue(change.PrincipalId);
            revoke.Parameters.AddWithValue(provider.Authority);
            revoke.Parameters.AddWithValue(change.Subject);
            if (await revoke.ExecuteNonQueryAsync(ct) == 0)
            {
                throw new InvalidOperationException("Revocation identity preconditions were not met.");
            }
        }
        else
        {
            throw new InvalidOperationException("Unknown identity maintenance operation.");
        }
        await using var audit = new NpgsqlCommand("""
            INSERT INTO identity_changes (request_id, workspace_id, actor_id, action, target_id, payload_sha256)
            VALUES ($1,$2,$3,$4,$5,$6)
            """, connection, transaction);
        audit.Parameters.AddWithValue(change.RequestId);
        audit.Parameters.AddWithValue(change.WorkspaceId);
        audit.Parameters.AddWithValue(change.PrincipalId);
        audit.Parameters.AddWithValue($"operator:{change.Operation}");
        audit.Parameters.AddWithValue(change.RunId ?? change.PrincipalId);
        audit.Parameters.AddWithValue(digest);
        await audit.ExecuteNonQueryAsync(ct);
        await transaction.CommitAsync(ct);
    }
}

public sealed record MaintenanceChange(string Operation, string RequestId, string WorkspaceId, string TenantId,
    string Name, string PrincipalId, string MembershipId, string Provider, string Subject,
    string? RunId = null, string? LegacySubject = null);
