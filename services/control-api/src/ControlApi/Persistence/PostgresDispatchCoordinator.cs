using System.Buffers.Binary;
using System.Security.Cryptography;
using System.Text;
using ControlApi.Domain;
using Npgsql;

namespace ControlApi.Persistence;

public sealed class PostgresDispatchCoordinator(NpgsqlDataSource dataSource, bool ownsDataSource = false)
    : IRunDispatchCoordinator, IAsyncDisposable
{
    public ValueTask DisposeAsync() => ownsDataSource ? dataSource.DisposeAsync() : ValueTask.CompletedTask;

    public async ValueTask<IAsyncDisposable> AcquireAsync(RunId runId, CancellationToken cancellationToken)
    {
        var connection = await dataSource.OpenConnectionAsync(cancellationToken);
        try
        {
            var transaction = await connection.BeginTransactionAsync(cancellationToken);
            var hash = SHA256.HashData(Encoding.UTF8.GetBytes(runId.Value));
            var key = BinaryPrimitives.ReadInt64BigEndian(hash);
            await using var command = new NpgsqlCommand(
                "SELECT pg_advisory_xact_lock($1)", connection, transaction);
            command.Parameters.AddWithValue(key);
            await command.ExecuteNonQueryAsync(cancellationToken);
            return new Lease(connection, transaction);
        }
        catch
        {
            await connection.DisposeAsync();
            throw;
        }
    }

    private sealed class Lease(NpgsqlConnection connection, NpgsqlTransaction transaction) : IAsyncDisposable
    {
        public async ValueTask DisposeAsync()
        {
            try
            {
                await transaction.DisposeAsync();
            }
            finally
            {
                await connection.DisposeAsync();
            }
        }
    }
}
