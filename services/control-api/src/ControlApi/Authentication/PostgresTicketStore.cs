using System.Security.Cryptography;
using Microsoft.AspNetCore.Authentication;
using Microsoft.AspNetCore.Authentication.Cookies;
using Microsoft.AspNetCore.DataProtection;
using Npgsql;

namespace ControlApi.Authentication;

public sealed class PostgresTicketStore(NpgsqlDataSource dataSource, IDataProtectionProvider protection) : ITicketStore
{
    private readonly IDataProtector protector = protection.CreateProtector("nexus.oidc-session.v1");

    public async Task<string> StoreAsync(AuthenticationTicket ticket)
    {
        var key = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        await RenewAsync(key, ticket);
        return key;
    }

    public async Task RenewAsync(string key, AuthenticationTicket ticket)
    {
        await using var command = dataSource.CreateCommand("""
            INSERT INTO identity_sessions (session_id, ticket, expires_at) VALUES ($1, $2, $3)
            ON CONFLICT (session_id) DO UPDATE SET ticket = EXCLUDED.ticket, expires_at = EXCLUDED.expires_at
            """);
        command.Parameters.AddWithValue(key);
        command.Parameters.AddWithValue(protector.Protect(TicketSerializer.Default.Serialize(ticket)));
        command.Parameters.AddWithValue(ticket.Properties.ExpiresUtc ?? throw new IdentityAccessException());
        await command.ExecuteNonQueryAsync();
    }

    public async Task<AuthenticationTicket?> RetrieveAsync(string key)
    {
        await using var command = dataSource.CreateCommand(
            "SELECT ticket FROM identity_sessions WHERE session_id = $1 AND expires_at > now()");
        command.Parameters.AddWithValue(key);
        var bytes = (byte[]?)await command.ExecuteScalarAsync();
        return bytes is null ? null : TicketSerializer.Default.Deserialize(protector.Unprotect(bytes));
    }

    public async Task RemoveAsync(string key)
    {
        await using var command = dataSource.CreateCommand("DELETE FROM identity_sessions WHERE session_id = $1");
        command.Parameters.AddWithValue(key);
        await command.ExecuteNonQueryAsync();
    }
}
