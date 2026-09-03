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
        var second = await repository.RequestCancellationAsync(
            created.Run.Id,
            created.Run.Version,
            default);

        Assert.True(first.Changed);
        Assert.False(second.Changed);
        Assert.Equal(first.Run.Version, second.Run.Version);

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
                [],
                new TokenUsage(8, 13),
                [],
                DateTimeOffset.UtcNow),
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
}
