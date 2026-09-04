using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Persistence;
using ControlApi.Semantic;

namespace ControlApi.Tests;

public sealed class RepositoryTests
{
    [Theory]
    [InlineData("run_0123456789abcdef0123456789abcdef", true)]
    [InlineData("run_0123456789ABCDEF0123456789ABCDEF", false)]
    [InlineData("0123456789abcdef0123456789abcdef", false)]
    [InlineData("run_not-hex", false)]
    public void RunIdUsesCanonicalValidatedFormat(string value, bool valid)
    {
        Assert.Equal(valid, RunId.TryParse(value, null, out _));
    }

    [Fact]
    public async Task CreateIdempotencyCoversQuestionAndRunOptions()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var request = new CreateRunRequest("create-idempotency", "synthetic-workload");
        var first = await repository.CreateAsync(request, "synthetic-user", default);
        var duplicate = await repository.CreateAsync(request, "synthetic-user", default);

        Assert.True(first.Created);
        Assert.False(duplicate.Created);
        Assert.Equal(first.Run.Id, duplicate.Run.Id);
        await Assert.ThrowsAsync<IdempotencyConflictException>(() =>
            repository.CreateAsync(
                request with { Question = "A different synthetic question" },
                "synthetic-user",
                default));
        await Assert.ThrowsAsync<IdempotencyConflictException>(() =>
            repository.CreateAsync(
                request with { OutputMode = OutputMode.Stream },
                "synthetic-user",
                default));
    }

    [Fact]
    public async Task CancellationIsIdempotentAndRejectsTerminalRuns()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("cancel-request", "synthetic-workload"),
            "synthetic-user",
            default);
        var first = await repository.RequestCancellationAsync(
            created.Run.Id,
            created.Run.Version,
            default);
        var delivered = await repository.MarkCancellationDeliveredAsync(
            created.Run.Id,
            first.CancellationGeneration,
            default);
        var second = await repository.RequestCancellationAsync(
            created.Run.Id,
            created.Run.Version,
            default);

        Assert.True(first.Changed);
        Assert.False(first.RequiresDispatch);
        Assert.Equal(RunState.Cancelled, first.Run.State);
        Assert.False(second.Changed);
        Assert.False(second.RequiresDispatch);
        Assert.Equal(delivered.Version, second.Run.Version);

        var terminalRepository = new InMemoryRunRepository(TimeProvider.System);
        var terminalCreated = await terminalRepository.CreateAsync(
            new CreateRunRequest("terminal-request", "synthetic-workload"),
            "synthetic-user",
            default);
        await terminalRepository.ApplySemanticStatusAsync(
            terminalCreated.Run.Id,
            terminalCreated.Run.Version,
            StubSemanticBackendClient.Status(terminalCreated.Run.Id, RunState.Failed),
            default);

        await Assert.ThrowsAsync<InvalidRunTransitionException>(() =>
            terminalRepository.RequestCancellationAsync(
                terminalCreated.Run.Id,
                null,
                default));
    }

    [Fact]
    public async Task FeedbackUsesOptimisticConcurrencyAndIdempotency()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("feedback-request", "synthetic-workload"),
            "synthetic-user",
            default);
        var request = new SubmitFeedbackRequest(
            "feedback-001",
            4,
            FeedbackOutcome.Helpful,
            ["clear"],
            created.Run.Version);

        var first = await repository.SubmitFeedbackAsync(
            created.Run.Id,
            request,
            "synthetic-user",
            default);
        var second = await repository.SubmitFeedbackAsync(
            created.Run.Id,
            request,
            "synthetic-user",
            default);
        Assert.Equal(first, second);

        await Assert.ThrowsAsync<IdempotencyConflictException>(() =>
            repository.SubmitFeedbackAsync(
                created.Run.Id,
                request with { Rating = 1 },
                "synthetic-user",
                default));

        await Assert.ThrowsAsync<OptimisticConcurrencyException>(() =>
            repository.SubmitFeedbackAsync(
                created.Run.Id,
                request with
                {
                    SubmissionId = "feedback-002",
                    ExpectedRunVersion = created.Run.Version
                },
                "synthetic-user",
                default));
    }

    [Fact]
    public async Task StatisticsProjectRunAndFeedbackTotals()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("statistics-request", "synthetic-workload"),
            "synthetic-user",
            default);
        var starting = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            created.Run.Version,
            StubSemanticBackendClient.Status(created.Run.Id, RunState.Starting),
            default);
        var running = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            starting.Version,
            StubSemanticBackendClient.Status(created.Run.Id, RunState.Running),
            default);
        await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            running.Version,
            new SemanticRunStatus(
                created.Run.Id,
                RunState.Succeeded,
                DateTimeOffset.UtcNow.AddSeconds(-1),
                DateTimeOffset.UtcNow,
                [],
                new TokenUsage(8, 13),
                []),
            default);

        var statistics = await repository.GetStatisticsAsync(default);

        Assert.Equal(1, statistics.TotalRuns);
        Assert.Equal(1, statistics.RunsByState[RunState.Succeeded]);
        Assert.Equal(8, statistics.TotalInputTokens);
        Assert.Equal(13, statistics.TotalOutputTokens);
        Assert.NotNull(statistics.AverageDurationMilliseconds);
    }

    [Fact]
    public async Task SemanticStatusCanSkipUnobservedTransientStates()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("skipped-state", "synthetic-workload"),
            "synthetic-user",
            default);
        var starting = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            created.Run.Version,
            StubSemanticBackendClient.Status(created.Run.Id, RunState.Starting),
            default);

        var succeeded = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            starting.Version,
            StubSemanticBackendClient.Status(created.Run.Id, RunState.Succeeded),
            default);

        Assert.Equal(RunState.Succeeded, succeeded.State);
    }

    [Fact]
    public async Task InvalidBackendStatusIsRejectedBeforeMutation()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("invalid-backend", "synthetic-workload"),
            "synthetic-user",
            default);
        var invalid = StubSemanticBackendClient.Status(RunId.New(), RunState.Running);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            repository.ApplySemanticStatusAsync(
                created.Run.Id,
                created.Run.Version,
                invalid,
                default));
        var unchanged = await repository.GetAsync(created.Run.Id, default);

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
        Assert.Equal(RunState.StartPending, unchanged!.State);
        Assert.Equal(created.Run.Version, unchanged.Version);
    }

    [Fact]
    public async Task StatisticsAverageOnlyInvariantTerminalRuns()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var now = DateTimeOffset.UtcNow;
        var active = await repository.CreateAsync(
            new CreateRunRequest("active-duration", "synthetic-workload"),
            "synthetic-user",
            default);
        await repository.ApplySemanticStatusAsync(
            active.Run.Id,
            active.Run.Version,
            new SemanticRunStatus(
                active.Run.Id,
                RunState.Running,
                now.AddMinutes(-30),
                null,
                [],
                new TokenUsage(0, 0),
                []),
            default);

        var terminal = await repository.CreateAsync(
            new CreateRunRequest("terminal-duration", "synthetic-workload"),
            "synthetic-user",
            default);
        await repository.ApplySemanticStatusAsync(
            terminal.Run.Id,
            terminal.Run.Version,
            new SemanticRunStatus(
                terminal.Run.Id,
                RunState.Succeeded,
                now.AddSeconds(-10),
                now,
                [],
                new TokenUsage(0, 0),
                []),
            default);

        var statistics = await repository.GetStatisticsAsync(default);

        Assert.Equal(10_000, statistics.AverageDurationMilliseconds);
    }

    [Fact]
    public async Task BackendCannotRegressPersistedStartTimestamp()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("timestamp-regression", "synthetic-workload"),
            "synthetic-user",
            default);
        var firstStart = DateTimeOffset.UtcNow;
        var running = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            created.Run.Version,
            new SemanticRunStatus(
                created.Run.Id,
                RunState.Running,
                firstStart,
                null,
                [],
                new TokenUsage(0, 0),
                []),
            default);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            repository.ApplySemanticStatusAsync(
                created.Run.Id,
                running.Version,
                new SemanticRunStatus(
                    created.Run.Id,
                    RunState.Succeeded,
                    firstStart.AddMinutes(-10),
                    firstStart.AddMinutes(-5),
                    [],
                    new TokenUsage(0, 0),
                    []),
                default));
        var unchanged = await repository.GetAsync(created.Run.Id, default);

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
        Assert.Equal(RunState.Running, unchanged!.State);
        Assert.Null(unchanged.CompletedAt);
    }

    [Fact]
    public async Task CancellationPreservesIntentAcrossActiveAndTerminalBackendStatuses()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("cancel-projection", "synthetic-workload"),
            "synthetic-user",
            default);
        var startedAt = DateTimeOffset.UtcNow.AddSeconds(-5);
        var running = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            created.Run.Version,
            new SemanticRunStatus(
                created.Run.Id,
                RunState.Running,
                startedAt,
                null,
                [],
                new TokenUsage(1, 2),
                []),
            default);
        var cancellation = await repository.RequestCancellationAsync(
            created.Run.Id,
            running.Version,
            default);

        var activeProjection = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            cancellation.Run.Version,
            new SemanticRunStatus(
                created.Run.Id,
                RunState.Running,
                startedAt,
                null,
                [],
                new TokenUsage(3, 5),
                []),
            default);
        Assert.Equal(RunState.CancelRequested, activeProjection.State);
        Assert.Equal(CancellationDeliveryState.Pending, activeProjection.CancellationDelivery);
        Assert.Equal(8, activeProjection.TokenUsage.TotalTokens);

        var terminalProjection = await repository.ApplySemanticStatusAsync(
            created.Run.Id,
            activeProjection.Version,
            new SemanticRunStatus(
                created.Run.Id,
                RunState.Cancelled,
                startedAt,
                DateTimeOffset.UtcNow,
                [],
                new TokenUsage(3, 5),
                []),
            default);
        var acknowledged = await repository.MarkCancellationDeliveredAsync(
            created.Run.Id,
            cancellation.CancellationGeneration,
            default);
        var replayedAcknowledgment = await repository.MarkCancellationDeliveredAsync(
            created.Run.Id,
            cancellation.CancellationGeneration,
            default);
        var replayedCancellation = await repository.RequestCancellationAsync(
            created.Run.Id,
            cancellation.Run.Version,
            default);

        Assert.Equal(RunState.Cancelled, terminalProjection.State);
        Assert.Equal(CancellationDeliveryState.Delivered, terminalProjection.CancellationDelivery);
        Assert.Equal(terminalProjection.Version, acknowledged.Version);
        Assert.Equal(acknowledged.Version, replayedAcknowledgment.Version);
        Assert.False(replayedCancellation.RequiresDispatch);
        Assert.Equal(RunState.Cancelled, replayedCancellation.Run.State);
    }

    [Fact]
    public async Task UnknownDispatchCanFinalizeCancellationWithoutBackend()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("cancel-unknown-dispatch", "synthetic-workload"),
            "synthetic-user",
            default);
        var unknown = await repository.MarkStartDispatchUnknownAsync(
            created.Run.Id,
            "semantic_backend_timeout",
            default);

        var cancellation = await repository.RequestCancellationAsync(
            created.Run.Id,
            unknown.Version,
            default);
        var cancelled = await repository.FinalizeCancellationWithoutBackendAsync(
            created.Run.Id,
            expectedVersion: null,
            expectedGeneration: cancellation.CancellationGeneration,
            default);
        var replay = await repository.FinalizeCancellationWithoutBackendAsync(
            created.Run.Id,
            expectedVersion: null,
            expectedGeneration: cancellation.CancellationGeneration,
            default);

        Assert.Equal(RunState.Cancelled, cancelled.State);
        Assert.Equal(CancellationDeliveryState.Delivered, cancelled.CancellationDelivery);
        Assert.Equal(1, cancelled.CancellationGeneration);
        Assert.Equal(cancelled.Version, replay.Version);
    }

    [Fact]
    public async Task StaleCancellationGenerationCannotAcknowledgeDelivery()
    {
        var repository = new InMemoryRunRepository(TimeProvider.System);
        var created = await repository.CreateAsync(
            new CreateRunRequest("cancel-generation", "synthetic-workload"),
            "synthetic-user",
            default);
        var cancellation = await repository.RequestCancellationAsync(
            created.Run.Id,
            created.Run.Version,
            default);

        await Assert.ThrowsAsync<OptimisticConcurrencyException>(() =>
            repository.MarkCancellationDeliveredAsync(
                created.Run.Id,
                cancellation.CancellationGeneration + 1,
                default));
    }
}
