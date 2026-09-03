using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Semantic;

namespace ControlApi.Persistence;

public sealed record CreateRunResult(RunMetadata Run, bool Created);
public sealed record MutationResult(RunMetadata Run, bool Changed);

public interface IRunRepository
{
    Task<CreateRunResult> CreateAsync(
        CreateRunRequest request,
        string subject,
        CancellationToken cancellationToken);

    Task<IReadOnlyList<RunMetadata>> ListAsync(int limit, CancellationToken cancellationToken);
    Task<RunMetadata?> GetAsync(RunId id, CancellationToken cancellationToken);

    Task<RunMetadata> ApplySemanticStatusAsync(
        RunId id,
        long expectedVersion,
        SemanticRunStatus status,
        CancellationToken cancellationToken);

    Task<RunMetadata> MarkFailedAsync(
        RunId id,
        string code,
        string message,
        CancellationToken cancellationToken);

    Task<MutationResult> RequestCancellationAsync(
        RunId id,
        long? expectedVersion,
        CancellationToken cancellationToken);

    Task<RunFeedback> SubmitFeedbackAsync(
        RunId id,
        SubmitFeedbackRequest request,
        string subject,
        CancellationToken cancellationToken);

    Task<IReadOnlyList<RunFeedback>> GetFeedbackAsync(RunId id, CancellationToken cancellationToken);
    Task<RunStatistics> GetStatisticsAsync(CancellationToken cancellationToken);
}

public sealed class RunNotFoundException(RunId id) : Exception($"Run '{id}' was not found.")
{
    public RunId RunId { get; } = id;
}

public sealed class OptimisticConcurrencyException(string message) : Exception(message);
public sealed class IdempotencyConflictException(string message) : Exception(message);
public sealed class InvalidRunTransitionException(RunState current, RunState requested)
    : Exception($"Run cannot transition from {current} to {requested}.");
