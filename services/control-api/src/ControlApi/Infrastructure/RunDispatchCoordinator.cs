using ControlApi.Domain;

namespace ControlApi;

public interface IRunDispatchCoordinator
{
    ValueTask<IAsyncDisposable> AcquireAsync(RunId runId, CancellationToken cancellationToken);
}

public sealed class RunDispatchCoordinator : IRunDispatchCoordinator
{
    private readonly object sync = new();
    private readonly Dictionary<RunId, GateEntry> gates = [];

    internal int ActiveGateCount
    {
        get
        {
            lock (sync)
            {
                return gates.Count;
            }
        }
    }

    public async ValueTask<IAsyncDisposable> AcquireAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        GateEntry entry;
        lock (sync)
        {
            if (!gates.TryGetValue(runId, out entry!))
            {
                entry = new GateEntry();
                gates.Add(runId, entry);
            }

            entry.ReferenceCount++;
        }

        try
        {
            await entry.Semaphore.WaitAsync(cancellationToken);
            return new Lease(this, runId, entry);
        }
        catch
        {
            Release(runId, entry, acquired: false);
            throw;
        }
    }

    private void Release(RunId runId, GateEntry entry, bool acquired)
    {
        if (acquired)
        {
            entry.Semaphore.Release();
        }

        var dispose = false;
        lock (sync)
        {
            entry.ReferenceCount--;
            if (entry.ReferenceCount == 0 &&
                gates.TryGetValue(runId, out var current) &&
                ReferenceEquals(current, entry))
            {
                gates.Remove(runId);
                dispose = true;
            }
        }

        if (dispose)
        {
            entry.Semaphore.Dispose();
        }
    }

    private sealed class GateEntry
    {
        public SemaphoreSlim Semaphore { get; } = new(1, 1);
        public int ReferenceCount { get; set; }
    }

    private sealed class Lease(
        RunDispatchCoordinator owner,
        RunId runId,
        GateEntry entry) : IAsyncDisposable
    {
        private int released;

        public ValueTask DisposeAsync()
        {
            if (Interlocked.Exchange(ref released, 1) == 0)
            {
                owner.Release(runId, entry, acquired: true);
            }

            return ValueTask.CompletedTask;
        }
    }
}
