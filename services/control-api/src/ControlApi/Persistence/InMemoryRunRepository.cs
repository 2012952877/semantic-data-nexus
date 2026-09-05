using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Semantic;

namespace ControlApi.Persistence;

public sealed class InMemoryRunRepository(TimeProvider timeProvider) : IRunRepository
{
    private readonly object gate = new();
    private readonly Dictionary<RunId, RunMetadata> runs = [];
    private readonly Dictionary<(string Subject, string ClientRequestId), RunId> createKeys = [];
    private readonly Dictionary<RunId, Dictionary<string, RunFeedback>> feedback = [];

    private Task<T> Locked<T>(Func<T> action, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            return Task.FromResult(action());
        }
    }

    public Task<CreateRunResult> CreateAsync(
        CreateRunRequest request, string subject, CancellationToken cancellationToken) =>
        Locked(() =>
        {
            var key = (subject, request.ClientRequestId);
            if (createKeys.TryGetValue(key, out var id))
            {
                return RunTransitions.Duplicate(runs[id], request);
            }
            var run = RunTransitions.Create(request, subject, timeProvider.GetUtcNow());
            runs.Add(run.Id, run);
            createKeys.Add(key, run.Id);
            return new CreateRunResult(run, true);
        }, cancellationToken);

    public Task<IReadOnlyList<RunMetadata>> ListAsync(int limit, CancellationToken cancellationToken) =>
        Locked<IReadOnlyList<RunMetadata>>(() =>
            runs.Values.OrderByDescending(run => run.CreatedAt).Take(limit).ToArray(), cancellationToken);

    public Task<RunMetadata?> GetAsync(RunId id, CancellationToken cancellationToken) =>
        Locked(() => runs.GetValueOrDefault(id), cancellationToken);

    private Task<RunMetadata> Mutate(
        RunId id, Func<RunMetadata, RunMetadata> transition, CancellationToken cancellationToken) =>
        Locked(() => runs[id] = transition(GetRequired(id)), cancellationToken);

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
        Locked(() =>
        {
            var result = RunTransitions.Cancel(GetRequired(id), expectedVersion, timeProvider.GetUtcNow());
            runs[id] = result.Run;
            return result;
        }, cancellationToken);

    public Task<RunMetadata> MarkCancellationDeliveredAsync(
        RunId id, long expectedGeneration, CancellationToken cancellationToken) =>
        Mutate(id, current => RunTransitions.Deliver(
            current, expectedGeneration, timeProvider.GetUtcNow()), cancellationToken);

    public Task<RunMetadata> FinalizeCancellationWithoutBackendAsync(
        RunId id, long? expectedVersion, long? expectedGeneration, CancellationToken cancellationToken) =>
        Mutate(id, current => RunTransitions.FinalizeCancellation(
            current, expectedVersion, expectedGeneration, timeProvider.GetUtcNow()), cancellationToken);

    public Task<RunFeedback> SubmitFeedbackAsync(
        RunId id, SubmitFeedbackRequest request, string subject, CancellationToken cancellationToken) =>
        Locked(() =>
        {
            var current = GetRequired(id);
            if (!feedback.TryGetValue(id, out var submissions))
            {
                submissions = [];
                feedback.Add(id, submissions);
            }
            if (submissions.TryGetValue(request.SubmissionId, out var existing))
            {
                return RunTransitions.DuplicateFeedback(existing, request);
            }
            var result = RunTransitions.Feedback(current, request, subject, timeProvider.GetUtcNow());
            runs[id] = result.Run;
            submissions.Add(result.Feedback.SubmissionId, result.Feedback);
            return result.Feedback;
        }, cancellationToken);

    public Task<IReadOnlyList<RunFeedback>> GetFeedbackAsync(RunId id, CancellationToken cancellationToken) =>
        Locked<IReadOnlyList<RunFeedback>>(() =>
        {
            _ = GetRequired(id);
            return feedback.GetValueOrDefault(id)?.Values.OrderBy(item => item.SubmittedAt).ToArray() ?? [];
        }, cancellationToken);

    public Task<RunStatistics> GetStatisticsAsync(CancellationToken cancellationToken) =>
        Locked(() => RunTransitions.Statistics(
            runs.Values.ToArray(), feedback.Values.Sum(items => (long)items.Count)), cancellationToken);

    private RunMetadata GetRequired(RunId id) =>
        runs.GetValueOrDefault(id) ?? throw new RunNotFoundException(id);
}
