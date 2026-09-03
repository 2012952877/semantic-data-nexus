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

    public Task<CreateRunResult> CreateAsync(
        CreateRunRequest request,
        string subject,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();

        lock (gate)
        {
            var key = (subject, request.ClientRequestId);
            if (createKeys.TryGetValue(key, out var existingId))
            {
                var existing = runs[existingId];
                if (!string.Equals(existing.Workload, request.Workload, StringComparison.Ordinal))
                {
                    throw new IdempotencyConflictException(
                        "The client request ID was already used with different run metadata.");
                }

                return Task.FromResult(new CreateRunResult(existing, false));
            }

            var now = timeProvider.GetUtcNow();
            var run = new RunMetadata(
                RunId.New(),
                request.ClientRequestId,
                request.Workload,
                subject,
                RunState.StartPending,
                CancellationDeliveryState.NotRequested,
                0,
                now,
                now,
                null,
                null,
                1,
                [],
                new TokenUsage(0, 0),
                []);

            runs.Add(run.Id, run);
            createKeys.Add(key, run.Id);
            return Task.FromResult(new CreateRunResult(run, true));
        }
    }

    public Task<IReadOnlyList<RunMetadata>> ListAsync(int limit, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            IReadOnlyList<RunMetadata> result = runs.Values
                .OrderByDescending(run => run.CreatedAt)
                .Take(limit)
                .ToArray();
            return Task.FromResult(result);
        }
    }

    public Task<RunMetadata?> GetAsync(RunId id, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            return Task.FromResult(runs.GetValueOrDefault(id));
        }
    }

    public Task<RunMetadata> ApplySemanticStatusAsync(
        RunId id,
        long expectedVersion,
        SemanticRunStatus status,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var current = GetRequired(id);
            SemanticRunStatusValidator.Validate(status, id);
            if (current.Version != expectedVersion)
            {
                throw new OptimisticConcurrencyException(
                    $"Run version {expectedVersion} is stale; current version is {current.Version}.");
            }

            var nextState =
                current.State == RunState.CancelRequested && !status.State.IsTerminal()
                    ? RunState.CancelRequested
                    : status.State;
            if (!current.State.CanTransitionTo(nextState))
            {
                throw new InvalidRunTransitionException(current.State, nextState);
            }

            var now = timeProvider.GetUtcNow();
            var startedAt = current.StartedAt ?? status.StartedAt;
            if (status.FinalizedAt is not null && status.FinalizedAt < startedAt)
            {
                throw new SemanticBackendException(
                    "semantic_backend_invalid_response",
                    "The semantic backend finalized the run before its persisted start timestamp.",
                    failureKind: SemanticFailureKind.InvalidResponse);
            }

            var completedAt = status.State.IsTerminal() ? status.FinalizedAt : null;
            var updated = current with
            {
                State = nextState,
                CancellationDelivery =
                    status.State.IsTerminal() &&
                    current.CancellationDelivery == CancellationDeliveryState.Pending
                        ? CancellationDeliveryState.Delivered
                        : current.CancellationDelivery,
                UpdatedAt = now,
                StartedAt = startedAt,
                CompletedAt = completedAt,
                Version = current.Version + 1,
                Stages = status.Stages,
                TokenUsage = status.TokenUsage,
                Diagnostics = status.Diagnostics
            };

            runs[id] = updated;
            return Task.FromResult(updated);
        }
    }

    public Task<RunMetadata> MarkFailedAsync(
        RunId id,
        string code,
        string message,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var current = GetRequired(id);
            if (current.State.IsTerminal())
            {
                return Task.FromResult(current);
            }

            var now = timeProvider.GetUtcNow();
            var updated = current with
            {
                State = RunState.Failed,
                UpdatedAt = now,
                StartedAt = current.StartedAt ?? current.CreatedAt,
                CompletedAt = now,
                Version = current.Version + 1,
                Diagnostics =
                [
                    .. current.Diagnostics,
                    new DiagnosticSummary(code, message, null, now)
                ]
            };
            runs[id] = updated;
            return Task.FromResult(updated);
        }
    }

    public Task<RunMetadata> MarkStartDispatchUnknownAsync(
        RunId id,
        string code,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var current = GetRequired(id);
            if (!current.State.RequiresStartReconciliation())
            {
                return Task.FromResult(current);
            }

            if (current.State == RunState.DispatchUnknown &&
                current.Diagnostics.Count > 0 &&
                current.Diagnostics[^1].Code == code)
            {
                return Task.FromResult(current);
            }

            var now = timeProvider.GetUtcNow();
            var updated = current with
            {
                State = RunState.DispatchUnknown,
                UpdatedAt = now,
                Version = current.Version + 1,
                Diagnostics =
                [
                    .. current.Diagnostics,
                    new DiagnosticSummary(
                        code,
                        "Semantic start dispatch outcome is unknown and requires reconciliation.",
                        null,
                        now)
                ]
            };
            runs[id] = updated;
            return Task.FromResult(updated);
        }
    }

    public Task<MutationResult> RequestCancellationAsync(
        RunId id,
        long? expectedVersion,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var current = GetRequired(id);
            if (current.State == RunState.CancelRequested)
            {
                return Task.FromResult(new MutationResult(
                    current,
                    false,
                    current.CancellationDelivery == CancellationDeliveryState.Pending,
                    current.CancellationGeneration));
            }

            if (current.State == RunState.Cancelled)
            {
                return Task.FromResult(new MutationResult(
                    current,
                    false,
                    false,
                    current.CancellationGeneration));
            }

            if (current.State.IsTerminal())
            {
                throw new InvalidRunTransitionException(current.State, RunState.CancelRequested);
            }

            if (expectedVersion is not null && current.Version != expectedVersion)
            {
                throw new OptimisticConcurrencyException(
                    $"Run version {expectedVersion} is stale; current version is {current.Version}.");
            }

            if (current.State == RunState.StartPending)
            {
                var now = timeProvider.GetUtcNow();
                var cancelled = current with
                {
                    State = RunState.Cancelled,
                    CancellationDelivery = CancellationDeliveryState.Delivered,
                    CancellationGeneration = current.CancellationGeneration + 1,
                    UpdatedAt = now,
                    StartedAt = current.CreatedAt,
                    CompletedAt = now,
                    Version = current.Version + 1
                };
                runs[id] = cancelled;
                return Task.FromResult(new MutationResult(
                    cancelled,
                    true,
                    false,
                    cancelled.CancellationGeneration));
            }

            var updated = current with
            {
                State = RunState.CancelRequested,
                CancellationDelivery = CancellationDeliveryState.Pending,
                CancellationGeneration = current.CancellationGeneration + 1,
                UpdatedAt = timeProvider.GetUtcNow(),
                Version = current.Version + 1
            };
            runs[id] = updated;
            return Task.FromResult(new MutationResult(
                updated,
                true,
                true,
                updated.CancellationGeneration));
        }
    }

    public Task<RunMetadata> MarkCancellationDeliveredAsync(
        RunId id,
        long expectedGeneration,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var current = GetRequired(id);
            if (current.CancellationGeneration != expectedGeneration)
            {
                throw new OptimisticConcurrencyException(
                    $"Cancellation generation {expectedGeneration} is stale; current generation is " +
                    $"{current.CancellationGeneration}.");
            }

            if (current.CancellationDelivery == CancellationDeliveryState.Delivered)
            {
                return Task.FromResult(current);
            }

            if (current.CancellationDelivery != CancellationDeliveryState.Pending)
            {
                throw new InvalidOperationException(
                    "Cancellation delivery can only be acknowledged for a pending generation.");
            }

            var updated = current with
            {
                CancellationDelivery = CancellationDeliveryState.Delivered,
                UpdatedAt = timeProvider.GetUtcNow(),
                Version = current.Version + 1
            };
            runs[id] = updated;
            return Task.FromResult(updated);
        }
    }

    public Task<RunFeedback> SubmitFeedbackAsync(
        RunId id,
        SubmitFeedbackRequest request,
        string subject,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var current = GetRequired(id);
            if (!feedback.TryGetValue(id, out var submissions))
            {
                submissions = [];
                feedback.Add(id, submissions);
            }

            if (submissions.TryGetValue(request.SubmissionId, out var existing))
            {
                if (existing.Rating == request.Rating &&
                    existing.Outcome == request.Outcome &&
                    existing.ReasonCodes.SequenceEqual(request.ReasonCodes, StringComparer.Ordinal))
                {
                    return Task.FromResult(existing);
                }

                throw new IdempotencyConflictException(
                    "The feedback submission ID was already used with different values.");
            }

            if (current.Version != request.ExpectedRunVersion)
            {
                throw new OptimisticConcurrencyException(
                    $"Run version {request.ExpectedRunVersion} is stale; current version is {current.Version}.");
            }

            var updated = current with
            {
                UpdatedAt = timeProvider.GetUtcNow(),
                Version = current.Version + 1
            };
            runs[id] = updated;

            var created = new RunFeedback(
                request.SubmissionId,
                id,
                request.Rating,
                request.Outcome,
                request.ReasonCodes.ToArray(),
                subject,
                updated.UpdatedAt,
                updated.Version);
            submissions.Add(created.SubmissionId, created);
            return Task.FromResult(created);
        }
    }

    public Task<IReadOnlyList<RunFeedback>> GetFeedbackAsync(
        RunId id,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            _ = GetRequired(id);
            IReadOnlyList<RunFeedback> result = feedback.GetValueOrDefault(id)?.Values
                .OrderBy(item => item.SubmittedAt)
                .ToArray() ?? [];
            return Task.FromResult(result);
        }
    }

    public Task<RunStatistics> GetStatisticsAsync(CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        lock (gate)
        {
            var completedDurations = runs.Values
                .Where(run => run.State.IsTerminal())
                .Select(run => run.Duration)
                .Where(duration => duration is not null && duration >= TimeSpan.Zero)
                .Select(duration => duration!.Value.TotalMilliseconds)
                .ToArray();
            var stats = new RunStatistics(
                runs.Count,
                Enum.GetValues<RunState>().ToDictionary(
                    state => state,
                    state => runs.Values.LongCount(run => run.State == state)),
                completedDurations.Length == 0 ? null : completedDurations.Average(),
                runs.Values.Sum(run => run.TokenUsage.InputTokens),
                runs.Values.Sum(run => run.TokenUsage.OutputTokens),
                feedback.Values.Sum(items => (long)items.Count));
            return Task.FromResult(stats);
        }
    }

    private RunMetadata GetRequired(RunId id) =>
        runs.GetValueOrDefault(id) ?? throw new RunNotFoundException(id);
}
