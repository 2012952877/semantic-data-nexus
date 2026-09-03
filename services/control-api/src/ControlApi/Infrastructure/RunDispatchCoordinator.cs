using System.Collections.Concurrent;
using ControlApi.Domain;

namespace ControlApi;

public interface IRunDispatchCoordinator
{
    ValueTask<IAsyncDisposable> AcquireAsync(RunId runId, CancellationToken cancellationToken);
}

public sealed class RunDispatchCoordinator : IRunDispatchCoordinator
{
    private readonly ConcurrentDictionary<RunId, SemaphoreSlim> gates = new();

    public async ValueTask<IAsyncDisposable> AcquireAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        var gate = gates.GetOrAdd(runId, static _ => new SemaphoreSlim(1, 1));
        await gate.WaitAsync(cancellationToken);
        return new Lease(gate);
    }

    private sealed class Lease(SemaphoreSlim gate) : IAsyncDisposable
    {
        private int released;

        public ValueTask DisposeAsync()
        {
            if (Interlocked.Exchange(ref released, 1) == 0)
            {
                gate.Release();
            }

            return ValueTask.CompletedTask;
        }
    }
}
