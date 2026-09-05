using System.Data;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Semantic;
using Npgsql;
using NpgsqlTypes;

namespace ControlApi.Persistence;

public sealed record StartDispatchClaim(RunMetadata Run, bool Acquired);

public interface IDurableStartDispatch
{
    Task<StartDispatchClaim> ClaimStartAsync(RunId id, CancellationToken cancellationToken);
}

public sealed class PostgresRunRepository(NpgsqlDataSource dataSource, TimeProvider timeProvider)
    : IRunRepository, IDurableStartDispatch
{
    public async Task<CreateRunResult> CreateAsync(
        CreateRunRequest request, string subject, CancellationToken cancellationToken)
    {
        var run = RunTransitions.Create(request, subject, timeProvider.GetUtcNow());
        var json = StoredRunCodec.Encode(run);
        _ = StoredRunCodec.Run(json);
        await using var connection = await dataSource.OpenConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        await using var insert = new NpgsqlCommand("""
            INSERT INTO control_runs (run_id, subject, client_request_id, version, created_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (subject, client_request_id) DO NOTHING
            """, connection, transaction);
        insert.Parameters.AddWithValue(run.Id.Value);
        insert.Parameters.AddWithValue(subject);
        insert.Parameters.AddWithValue(request.ClientRequestId);
        insert.Parameters.AddWithValue(run.Version);
        insert.Parameters.AddWithValue(run.CreatedAt.ToUniversalTime());
        insert.Parameters.AddWithValue(NpgsqlDbType.Jsonb, json);
        var created = await insert.ExecuteNonQueryAsync(cancellationToken) == 1;
        CreateRunResult result;
        if (created)
        {
            result = new(run, true);
        }
        else
        {
            await using var query = new NpgsqlCommand("""
                SELECT metadata::text FROM control_runs
                WHERE subject = $1 AND client_request_id = $2 FOR UPDATE
                """, connection, transaction);
            query.Parameters.AddWithValue(subject);
            query.Parameters.AddWithValue(request.ClientRequestId);
            result = RunTransitions.Duplicate(
                StoredRunCodec.Run((string)(await query.ExecuteScalarAsync(cancellationToken))!), request);
        }
        await transaction.CommitAsync(cancellationToken);
        return result;
    }

    public async Task<RunMetadata?> GetAsync(RunId id, CancellationToken cancellationToken)
    {
        await using var command = dataSource.CreateCommand(
            "SELECT metadata::text FROM control_runs WHERE run_id = $1");
        command.Parameters.AddWithValue(id.Value);
        var json = (string?)await command.ExecuteScalarAsync(cancellationToken);
        return json is null ? null : ReadRun(json, id);
    }

    private static RunMetadata ReadRun(string json, RunId id)
    {
        var run = StoredRunCodec.Run(json);
        return run.Id == id ? run : throw new StorageCorruptionException();
    }

    public async Task<IReadOnlyList<RunMetadata>> ListAsync(int limit, CancellationToken cancellationToken)
    {
        ArgumentOutOfRangeException.ThrowIfLessThan(limit, 1);
        ArgumentOutOfRangeException.ThrowIfGreaterThan(limit, 100);
        await using var command = dataSource.CreateCommand(
            "SELECT metadata::text FROM control_runs ORDER BY created_at DESC, run_id LIMIT $1");
        command.Parameters.AddWithValue(limit);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        var runs = new List<RunMetadata>();
        while (await reader.ReadAsync(cancellationToken))
        {
            runs.Add(StoredRunCodec.Run(reader.GetString(0)));
        }
        return runs;
    }

    private async Task<T> WithRun<T>(
        RunId id,
        Func<NpgsqlConnection, NpgsqlTransaction, RunMetadata, Task<(RunMetadata Run, T Result)>> action,
        CancellationToken cancellationToken)
    {
        await using var connection = await dataSource.OpenConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(cancellationToken);
        await using var query = new NpgsqlCommand(
            "SELECT metadata::text FROM control_runs WHERE run_id = $1 FOR UPDATE", connection, transaction);
        query.Parameters.AddWithValue(id.Value);
        var json = (string?)await query.ExecuteScalarAsync(cancellationToken);
        var current = json is null ? throw new RunNotFoundException(id) : ReadRun(json, id);
        var result = await action(connection, transaction, current);
        if (result.Run != current)
        {
            var updated = StoredRunCodec.Encode(result.Run);
            _ = StoredRunCodec.Run(updated);
            await using var update = new NpgsqlCommand("""
                UPDATE control_runs SET metadata = $1, version = $2 WHERE run_id = $3 AND version = $4
                """, connection, transaction);
            update.Parameters.AddWithValue(NpgsqlDbType.Jsonb, updated);
            update.Parameters.AddWithValue(result.Run.Version);
            update.Parameters.AddWithValue(id.Value);
            update.Parameters.AddWithValue(current.Version);
            if (await update.ExecuteNonQueryAsync(cancellationToken) != 1)
            {
                throw new OptimisticConcurrencyException("Run changed during its transaction.");
            }
        }
        await transaction.CommitAsync(cancellationToken);
        return result.Result;
    }

    private Task<RunMetadata> Mutate(
        RunId id, Func<RunMetadata, RunMetadata> transition, CancellationToken cancellationToken) =>
        WithRun(id, (_, _, current) =>
        {
            var updated = transition(current);
            return Task.FromResult((updated, updated));
        }, cancellationToken);

    public Task<RunMetadata> ApplySemanticStatusAsync(
        RunId id, long expectedVersion, SemanticRunStatus status, CancellationToken cancellationToken) =>
        Mutate(id, current => RunTransitions.ApplyStatus(
            current, expectedVersion, status, timeProvider.GetUtcNow()), cancellationToken);

    public Task<RunMetadata> MarkFailedAsync(
        RunId id, string code, string message, CancellationToken cancellationToken) =>
        Mutate(id, current => RunTransitions.Fail(
            current, code, message, timeProvider.GetUtcNow()), cancellationToken);

    public Task<RunMetadata> MarkStartDispatchUnknownAsync(
        RunId id, string code, CancellationToken cancellationToken) =>
        Mutate(id, current => RunTransitions.DispatchUnknown(
            current, code, timeProvider.GetUtcNow()), cancellationToken);

    public Task<MutationResult> RequestCancellationAsync(
        RunId id, long? expectedVersion, CancellationToken cancellationToken) =>
        WithRun(id, (_, _, current) =>
        {
            var result = RunTransitions.Cancel(current, expectedVersion, timeProvider.GetUtcNow());
            return Task.FromResult((result.Run, result));
        }, cancellationToken);

    public Task<RunMetadata> MarkCancellationDeliveredAsync(
        RunId id, long expectedGeneration, CancellationToken cancellationToken) =>
        Mutate(id, current => RunTransitions.Deliver(
            current, expectedGeneration, timeProvider.GetUtcNow()), cancellationToken);

    public Task<RunMetadata> FinalizeCancellationWithoutBackendAsync(
        RunId id, long? expectedVersion, long? expectedGeneration, CancellationToken cancellationToken) =>
        WithRun(id, async (connection, transaction, current) =>
        {
            // A missing backend run does not fence a paused sender or prove durable deletion.
            await using var query = new NpgsqlCommand(
                "SELECT count(*) FROM control_start_dispatch WHERE run_id = $1", connection, transaction);
            query.Parameters.AddWithValue(id.Value);
            if ((long)(await query.ExecuteScalarAsync(cancellationToken))! != 0 &&
                !current.State.IsTerminal())
            {
                throw new DispatchRecoveryRequiredException();
            }
            var updated = RunTransitions.FinalizeCancellation(
                current, expectedVersion, expectedGeneration, timeProvider.GetUtcNow());
            return (updated, updated);
        }, cancellationToken);

    public Task<StartDispatchClaim> ClaimStartAsync(RunId id, CancellationToken cancellationToken) =>
        WithRun(id, async (connection, transaction, current) =>
        {
            if (!current.State.RequiresStartReconciliation())
            {
                return (current, new StartDispatchClaim(current, false));
            }
            await using var insert = new NpgsqlCommand("""
                INSERT INTO control_start_dispatch (run_id, generation) VALUES ($1, 1)
                ON CONFLICT (run_id) DO NOTHING
                """, connection, transaction);
            insert.Parameters.AddWithValue(id.Value);
            var acquired = await insert.ExecuteNonQueryAsync(cancellationToken) == 1;
            var updated = RunTransitions.DispatchUnknown(
                current, "durable_start_dispatch_claimed", timeProvider.GetUtcNow());
            return (updated, new StartDispatchClaim(updated, acquired));
        }, cancellationToken);

    public Task<RunFeedback> SubmitFeedbackAsync(
        RunId id, SubmitFeedbackRequest request, string subject, CancellationToken cancellationToken) =>
        WithRun(id, async (connection, transaction, current) =>
        {
            await using var query = new NpgsqlCommand("""
                SELECT feedback::text FROM control_feedback WHERE run_id = $1 AND submission_id = $2
                """, connection, transaction);
            query.Parameters.AddWithValue(id.Value);
            query.Parameters.AddWithValue(request.SubmissionId);
            var json = (string?)await query.ExecuteScalarAsync(cancellationToken);
            if (json is not null)
            {
                return (current, RunTransitions.DuplicateFeedback(StoredRunCodec.Feedback(json), request));
            }
            var result = RunTransitions.Feedback(current, request, subject, timeProvider.GetUtcNow());
            json = StoredRunCodec.Encode(result.Feedback);
            _ = StoredRunCodec.Feedback(json);
            await using var insert = new NpgsqlCommand("""
                INSERT INTO control_feedback (run_id, submission_id, feedback) VALUES ($1, $2, $3)
                """, connection, transaction);
            insert.Parameters.AddWithValue(id.Value);
            insert.Parameters.AddWithValue(request.SubmissionId);
            insert.Parameters.AddWithValue(NpgsqlDbType.Jsonb, json);
            await insert.ExecuteNonQueryAsync(cancellationToken);
            return result;
        }, cancellationToken);

    public async Task<IReadOnlyList<RunFeedback>> GetFeedbackAsync(RunId id, CancellationToken cancellationToken)
    {
        _ = await GetAsync(id, cancellationToken) ?? throw new RunNotFoundException(id);
        await using var command = dataSource.CreateCommand(
            "SELECT feedback::text FROM control_feedback WHERE run_id = $1");
        command.Parameters.AddWithValue(id.Value);
        await using var reader = await command.ExecuteReaderAsync(cancellationToken);
        var items = new List<RunFeedback>();
        while (await reader.ReadAsync(cancellationToken))
        {
            var item = StoredRunCodec.Feedback(reader.GetString(0));
            if (item.RunId != id)
            {
                throw new StorageCorruptionException();
            }
            items.Add(item);
        }
        return items.OrderBy(item => item.SubmittedAt).ToArray();
    }

    public async Task<RunStatistics> GetStatisticsAsync(CancellationToken cancellationToken)
    {
        await using var connection = await dataSource.OpenConnectionAsync(cancellationToken);
        await using var transaction = await connection.BeginTransactionAsync(IsolationLevel.RepeatableRead, cancellationToken);
        await using var command = new NpgsqlCommand("SELECT metadata::text FROM control_runs", connection, transaction);
        var runs = new List<RunMetadata>();
        await using (var reader = await command.ExecuteReaderAsync(cancellationToken))
        {
            while (await reader.ReadAsync(cancellationToken))
            {
                runs.Add(StoredRunCodec.Run(reader.GetString(0)));
            }
        }
        await using var feedback = new NpgsqlCommand("SELECT feedback::text FROM control_feedback", connection, transaction);
        long count = 0;
        await using (var reader = await feedback.ExecuteReaderAsync(cancellationToken))
        {
            while (await reader.ReadAsync(cancellationToken))
            {
                _ = StoredRunCodec.Feedback(reader.GetString(0));
                count++;
            }
        }
        await transaction.CommitAsync(cancellationToken);
        return RunTransitions.Statistics(runs, count);
    }
}
