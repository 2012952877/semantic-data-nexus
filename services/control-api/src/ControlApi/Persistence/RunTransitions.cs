using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Semantic;

namespace ControlApi.Persistence;

// Storage adapters provide the clock and serialize access; transitions have no I/O.
internal static class RunTransitions
{
    public static RunMetadata Create(CreateRunRequest request, string subject, DateTimeOffset now) =>
        new(RunId.New(), request.ClientRequestId, request.Workload, request.Question,
            request.EvaluationClock, request.EvaluationTimezone, request.CompilationMode,
            request.ExecutionMode, request.OutputMode, subject, RunState.StartPending,
            CancellationDeliveryState.NotRequested, 0, now, now, null, null, 1, [],
            new TokenUsage(0, 0), []);

    public static CreateRunResult Duplicate(RunMetadata run, CreateRunRequest request)
    {
        if (run.Workload != request.Workload || run.Question != request.Question ||
            run.EvaluationClock != request.EvaluationClock ||
            run.EvaluationTimezone != request.EvaluationTimezone ||
            run.CompilationMode != request.CompilationMode ||
            run.ExecutionMode != request.ExecutionMode || run.OutputMode != request.OutputMode)
        {
            throw new IdempotencyConflictException(
                "The client request ID was already used with different run metadata.");
        }
        return new(run, false);
    }

    public static void CheckVersion(RunMetadata current, long? expected)
    {
        if (expected is not null && current.Version != expected)
        {
            throw new OptimisticConcurrencyException(
                $"Run version {expected} is stale; current version is {current.Version}.");
        }
    }

    private static void CheckGeneration(RunMetadata current, long? expected)
    {
        if (expected is not null && current.CancellationGeneration != expected)
        {
            throw new OptimisticConcurrencyException(
                $"Cancellation generation {expected} is stale; current generation is {current.CancellationGeneration}.");
        }
    }

    public static RunMetadata ApplyStatus(
        RunMetadata current, long expected, SemanticRunStatus status, DateTimeOffset now)
    {
        SemanticRunStatusValidator.Validate(status, current.Id);
        CheckVersion(current, expected);
        var next = current.State == RunState.CancelRequested && !status.State.IsTerminal()
            ? RunState.CancelRequested : status.State;
        if (!current.State.CanTransitionTo(next))
        {
            throw new InvalidRunTransitionException(current.State, next);
        }
        // A repeated terminal observation cannot replace committed usage/timestamps.
        if (current.State.IsTerminal())
        {
            return current;
        }
        var started = current.StartedAt ?? status.StartedAt;
        if (status.FinalizedAt is not null && status.FinalizedAt < started)
        {
            throw new SemanticBackendException(
                "semantic_backend_invalid_response",
                "The semantic backend finalized the run before its persisted start timestamp.",
                failureKind: SemanticFailureKind.InvalidResponse);
        }
        return current with
        {
            State = next,
            CancellationDelivery = status.State.IsTerminal() &&
                current.CancellationDelivery == CancellationDeliveryState.Pending
                ? CancellationDeliveryState.Delivered : current.CancellationDelivery,
            UpdatedAt = now,
            StartedAt = started,
            CompletedAt = status.State.IsTerminal() ? status.FinalizedAt : null,
            Version = checked(current.Version + 1),
            Stages = status.Stages,
            TokenUsage = status.TokenUsage,
            Diagnostics = status.Diagnostics
        };
    }

    public static RunMetadata Fail(RunMetadata current, string code, string message, DateTimeOffset now) =>
        current.State.IsTerminal() ? current : current with
        {
            State = RunState.Failed,
            CancellationDelivery = current.CancellationDelivery == CancellationDeliveryState.Pending
                ? CancellationDeliveryState.Delivered : current.CancellationDelivery,
            UpdatedAt = now,
            StartedAt = current.StartedAt ?? current.CreatedAt,
            CompletedAt = now,
            Version = checked(current.Version + 1),
            Diagnostics = [.. current.Diagnostics, new DiagnosticSummary(code, message, null, now)]
        };

    public static RunMetadata DispatchUnknown(RunMetadata current, string code, DateTimeOffset now) =>
        !current.State.RequiresStartReconciliation() ||
        (current.State == RunState.DispatchUnknown && current.Diagnostics.Count > 0 &&
            current.Diagnostics[^1].Code == code)
        ? current : current with
        {
            State = RunState.DispatchUnknown,
            UpdatedAt = now,
            Version = checked(current.Version + 1),
            Diagnostics = [.. current.Diagnostics, new DiagnosticSummary(code,
                "Semantic start dispatch outcome is unknown and requires reconciliation.", null, now)]
        };

    public static MutationResult Cancel(RunMetadata current, long? expected, DateTimeOffset now)
    {
        if (current.State == RunState.CancelRequested || current.State == RunState.Cancelled ||
            (current.State.IsTerminal() && current.CancellationGeneration > 0 &&
                current.CancellationDelivery == CancellationDeliveryState.Delivered))
        {
            return new(current, false,
                current.CancellationDelivery == CancellationDeliveryState.Pending,
                current.CancellationGeneration);
        }
        if (current.State.IsTerminal())
        {
            throw new InvalidRunTransitionException(current.State, RunState.CancelRequested);
        }
        CheckVersion(current, expected);
        var local = current.State == RunState.StartPending;
        var updated = current with
        {
            State = local ? RunState.Cancelled : RunState.CancelRequested,
            CancellationDelivery = local ? CancellationDeliveryState.Delivered : CancellationDeliveryState.Pending,
            CancellationGeneration = checked(current.CancellationGeneration + 1),
            UpdatedAt = now,
            StartedAt = local ? current.CreatedAt : current.StartedAt,
            CompletedAt = local ? now : current.CompletedAt,
            Version = checked(current.Version + 1)
        };
        return new(updated, true, !local, updated.CancellationGeneration);
    }

    public static RunMetadata Deliver(RunMetadata current, long generation, DateTimeOffset now)
    {
        CheckGeneration(current, generation);
        if (current.CancellationDelivery == CancellationDeliveryState.Delivered)
        {
            return current;
        }
        if (current.CancellationDelivery != CancellationDeliveryState.Pending)
        {
            throw new InvalidOperationException(
                "Cancellation delivery can only be acknowledged for a pending generation.");
        }
        return current with
        {
            CancellationDelivery = CancellationDeliveryState.Delivered,
            UpdatedAt = now,
            Version = checked(current.Version + 1)
        };
    }

    public static RunMetadata FinalizeCancellation(
        RunMetadata current, long? version, long? generation, DateTimeOffset now)
    {
        if (current.State == RunState.Cancelled &&
            current.CancellationDelivery == CancellationDeliveryState.Delivered)
        {
            return current;
        }
        CheckVersion(current, version);
        CheckGeneration(current, generation);
        if (current.State != RunState.CancelRequested)
        {
            throw new InvalidRunTransitionException(current.State, RunState.Cancelled);
        }
        return current with
        {
            State = RunState.Cancelled,
            CancellationDelivery = CancellationDeliveryState.Delivered,
            UpdatedAt = now,
            StartedAt = current.StartedAt ?? current.CreatedAt,
            CompletedAt = now,
            Version = checked(current.Version + 1)
        };
    }

    public static RunFeedback DuplicateFeedback(RunFeedback existing, SubmitFeedbackRequest request)
    {
        if (existing.Rating != request.Rating || existing.Outcome != request.Outcome ||
            !existing.ReasonCodes.SequenceEqual(request.ReasonCodes, StringComparer.Ordinal))
        {
            throw new IdempotencyConflictException(
                "The feedback submission ID was already used with different values.");
        }
        return existing;
    }

    public static (RunMetadata Run, RunFeedback Feedback) Feedback(
        RunMetadata current, SubmitFeedbackRequest request, string subject, DateTimeOffset now)
    {
        CheckVersion(current, request.ExpectedRunVersion);
        var updated = current with { UpdatedAt = now, Version = checked(current.Version + 1) };
        return (updated, new RunFeedback(request.SubmissionId, current.Id, request.Rating,
            request.Outcome, request.ReasonCodes.ToArray(), subject, now, updated.Version));
    }

    public static RunStatistics Statistics(IReadOnlyList<RunMetadata> runs, long feedbackCount)
    {
        var durations = runs.Where(run => run.State.IsTerminal()).Select(run => run.Duration)
            .Where(duration => duration is not null && duration >= TimeSpan.Zero)
            .Select(duration => duration!.Value.TotalMilliseconds).ToArray();
        return new(runs.Count, Enum.GetValues<RunState>().ToDictionary(
                state => state, state => runs.LongCount(run => run.State == state)),
            durations.Length == 0 ? null : durations.Average(),
            runs.Sum(run => run.TokenUsage.InputTokens), runs.Sum(run => run.TokenUsage.OutputTokens),
            feedbackCount);
    }
}
