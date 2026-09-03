using ControlApi.Domain;

namespace ControlApi.Tests;

public sealed class RunDispatchCoordinatorTests
{
    [Fact]
    public async Task GateIsEvictedAfterQueuedLeasesComplete()
    {
        var coordinator = new RunDispatchCoordinator();
        var runId = RunId.New();
        var first = await coordinator.AcquireAsync(runId, default);
        var secondTask = coordinator.AcquireAsync(runId, default).AsTask();

        Assert.Equal(1, coordinator.ActiveGateCount);
        await first.DisposeAsync();
        var second = await secondTask;
        Assert.Equal(1, coordinator.ActiveGateCount);
        await second.DisposeAsync();

        Assert.Equal(0, coordinator.ActiveGateCount);
    }

    [Fact]
    public async Task CancelledWaiterReleasesItsGateReference()
    {
        var coordinator = new RunDispatchCoordinator();
        var runId = RunId.New();
        var first = await coordinator.AcquireAsync(runId, default);
        using var cancellation = new CancellationTokenSource();
        var waiting = coordinator.AcquireAsync(runId, cancellation.Token).AsTask();
        cancellation.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => waiting);
        Assert.Equal(1, coordinator.ActiveGateCount);
        await first.DisposeAsync();

        Assert.Equal(0, coordinator.ActiveGateCount);
    }
}
