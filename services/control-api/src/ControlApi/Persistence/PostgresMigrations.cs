using System.Security.Cryptography;
using System.Text;
using Npgsql;

namespace ControlApi.Persistence;

public sealed class PostgresMigrations(NpgsqlDataSource dataSource)
{
    public async Task ApplyAsync(CancellationToken cancellationToken)
    {
        await using var connection = await dataSource.OpenConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        await using (var command = new NpgsqlCommand("""
            SELECT pg_advisory_xact_lock(731310031);
            CREATE TABLE IF NOT EXISTS control_schema_versions (
                version integer PRIMARY KEY,
                checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now()
            );
            """, connection, transaction))
        {
            await command.ExecuteNonQueryAsync(cancellationToken);
        }
        var assembly = typeof(PostgresMigrations).Assembly;
        var resources = assembly.GetManifestResourceNames()
            .Where(name => name.Contains(".Persistence.Migrations.", StringComparison.Ordinal) &&
                name.EndsWith(".sql", StringComparison.Ordinal))
            .Order(StringComparer.Ordinal).ToArray();
        await using (var command = new NpgsqlCommand(
            "SELECT count(*) FROM control_schema_versions WHERE version < 1 OR version > $1", connection, transaction))
        {
            command.Parameters.AddWithValue(resources.Length);
            if ((long)(await command.ExecuteScalarAsync(cancellationToken))! != 0)
            {
                throw new StorageConfigurationException("Database schema is newer than this application.");
            }
        }
        for (var index = 0; index < resources.Length; index++)
        {
            using var stream = assembly.GetManifestResourceStream(resources[index])!;
            using var reader = new StreamReader(stream, Encoding.UTF8);
            // Normalize checkout line endings so the checksum is portable between Windows and Linux.
            var sql = (await reader.ReadToEndAsync(cancellationToken)).Replace("\r\n", "\n", StringComparison.Ordinal);
            var checksum = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(sql)));
            await using var query = new NpgsqlCommand(
                "SELECT checksum FROM control_schema_versions WHERE version = $1", connection, transaction);
            query.Parameters.AddWithValue(index + 1);
            var existing = (string?)await query.ExecuteScalarAsync(cancellationToken);
            if (existing is not null)
            {
                if (existing != checksum)
                {
                    throw new StorageConfigurationException("Database migration checksum mismatch.");
                }
                continue;
            }
            await using var migration = new NpgsqlCommand(sql, connection, transaction);
            await migration.ExecuteNonQueryAsync(cancellationToken);
            await using var record = new NpgsqlCommand(
                "INSERT INTO control_schema_versions (version, checksum) VALUES ($1, $2)", connection, transaction);
            record.Parameters.AddWithValue(index + 1);
            record.Parameters.AddWithValue(checksum);
            await record.ExecuteNonQueryAsync(cancellationToken);
        }
        await transaction.CommitAsync(cancellationToken);
    }
}
