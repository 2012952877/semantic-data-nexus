using System.Net;
using System.Collections.Concurrent;
using ControlApi.Domain;
using ControlApi.Semantic;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;

namespace ControlApi.Tests;

public sealed class ControlApiFactory(
    ISemanticBackendClient? semanticBackend = null,
    string environment = "Development",
    IReadOnlyDictionary<string, string?>? settings = null,
    IRunDispatchCoordinator? dispatchCoordinator = null)
    : WebApplicationFactory<Program>
{
    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseEnvironment(environment);
        builder.ConfigureAppConfiguration((_, configuration) =>
        {
            var defaults = new Dictionary<string, string?>
            {
                ["LocalDevelopmentAuth:Enabled"] = "true",
                ["SemanticBackend:UseFake"] = "true",
                ["OpenApi:Enabled"] = "false"
            };
            if (settings is not null)
            {
                foreach (var setting in settings)
                {
                    defaults[setting.Key] = setting.Value;
                }
            }

            configuration.AddInMemoryCollection(defaults);
        });

        if (semanticBackend is not null)
        {
            builder.ConfigureTestServices(services =>
            {
                services.RemoveAll<ISemanticBackendClient>();
                services.AddSingleton(semanticBackend);
            });
        }

        if (dispatchCoordinator is not null)
        {
            builder.ConfigureTestServices(services =>
            {
                services.RemoveAll<IRunDispatchCoordinator>();
                services.AddSingleton(dispatchCoordinator);
            });
        }
    }

    public HttpClient CreateAuthenticatedClient(string roles = "reader")
    {
        var client = CreateClient();
        client.DefaultRequestHeaders.Add("X-Dev-Subject", "synthetic-user");
        client.DefaultRequestHeaders.Add("X-Dev-Name", "Synthetic User");
        client.DefaultRequestHeaders.Add("X-Dev-Roles", roles);
        return client;
    }
}

public sealed class ObservableRunDispatchCoordinator : IRunDispatchCoordinator
{
    private readonly object gate = new();
    private readonly RunDispatchCoordinator inner = new();
    private readonly Dictionary<int, TaskCompletionSource<bool>> waiters = [];
    private int attempts;

    public int AttemptCount => Volatile.Read(ref attempts);

    public ValueTask<IAsyncDisposable> AcquireAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        var attempt = Interlocked.Increment(ref attempts);
        lock (gate)
        {
            if (waiters.Remove(attempt, out var waiter))
            {
                waiter.TrySetResult(true);
            }
        }

        return inner.AcquireAsync(runId, cancellationToken);
    }

    public Task WaitForAttemptAsync(int attempt, CancellationToken cancellationToken = default)
    {
        if (Volatile.Read(ref attempts) >= attempt)
        {
            return Task.CompletedTask;
        }

        TaskCompletionSource<bool> waiter;
        lock (gate)
        {
            if (attempts >= attempt)
            {
                return Task.CompletedTask;
            }

            waiter = new TaskCompletionSource<bool>(
                TaskCreationOptions.RunContinuationsAsynchronously);
            waiters.Add(attempt, waiter);
        }

        return waiter.Task.WaitAsync(cancellationToken);
    }
}

public sealed class DelayedFirstRunDispatchCoordinator : IRunDispatchCoordinator
{
    private readonly RunDispatchCoordinator inner = new();
    private int attempts;

    public TaskCompletionSource<bool> FirstAttempted { get; } =
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    public TaskCompletionSource<bool> ReleaseFirst { get; } =
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    public async ValueTask<IAsyncDisposable> AcquireAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        if (Interlocked.Increment(ref attempts) == 1)
        {
            FirstAttempted.TrySetResult(true);
            await ReleaseFirst.Task.WaitAsync(cancellationToken);
        }

        return await inner.AcquireAsync(runId, cancellationToken);
    }
}

public sealed class StubSemanticBackendClient : ISemanticBackendClient
{
    public bool Ready { get; set; } = true;
    public Exception? StartException { get; set; }
    public SemanticRunStatus? StartResult { get; set; }
    public Exception? StatusException { get; set; }
    public Exception? CancelException { get; set; }
    public TaskCompletionSource<bool>? StartEntered { get; set; }
    public TaskCompletionSource<bool>? ReleaseStart { get; set; }
    public TaskCompletionSource<bool>? CancelEntered { get; set; }
    public TaskCompletionSource<bool>? ReleaseCancel { get; set; }
    public int StartCalls { get; private set; }
    public int StatusCalls { get; private set; }
    public int CancelCalls { get; private set; }
    public Dictionary<RunId, SemanticRunStatus> Runs { get; } = [];
    public ConcurrentQueue<string> Operations { get; } = new();

    public async Task<SemanticRunStatus> StartAsync(
        SemanticRunStart request,
        CancellationToken cancellationToken)
    {
        StartCalls++;
        Operations.Enqueue("start");
        StartEntered?.TrySetResult(true);
        if (ReleaseStart is not null)
        {
            await ReleaseStart.Task.WaitAsync(cancellationToken);
        }

        if (StartException is not null)
        {
            throw StartException;
        }

        var status = StartResult ?? Status(request.RunId, RunState.Starting);
        Runs[request.RunId] = status;
        return status;
    }

    public Task<SemanticRunStatus> GetStatusAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        StatusCalls++;
        if (StatusException is not null)
        {
            throw StatusException;
        }

        return Task.FromResult(
            Runs.GetValueOrDefault(runId) ??
            throw new SemanticBackendException(
                "semantic_backend_status_failed",
                "Synthetic run was not found.",
                HttpStatusCode.NotFound,
                SemanticFailureKind.NotFound));
    }

    public async Task RequestCancellationAsync(RunId runId, CancellationToken cancellationToken)
    {
        CancelCalls++;
        Operations.Enqueue("cancel");
        CancelEntered?.TrySetResult(true);
        if (ReleaseCancel is not null)
        {
            await ReleaseCancel.Task.WaitAsync(cancellationToken);
        }

        if (CancelException is not null)
        {
            throw CancelException;
        }
        Runs[runId] = Status(runId, RunState.Cancelled);
        Runs[runId] = Status(runId, RunState.Cancelled);
    }

    public Task<bool> IsReadyAsync(CancellationToken cancellationToken) =>
        Task.FromResult(Ready);

    public static SemanticRunStatus Status(RunId id, RunState state)
    {
        var now = DateTimeOffset.UtcNow;
        DateTimeOffset? startedAt = state == RunState.Queued ? null : now.AddSeconds(-1);
        DateTimeOffset? finalizedAt = state.IsTerminal() ? now : null;
        return new SemanticRunStatus(
            id,
            state,
            startedAt,
            finalizedAt,
            [],
            new TokenUsage(0, 0),
            []);
    }
}

public sealed class DelegateHandler(
    Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> handler)
    : HttpMessageHandler
{
    protected override Task<HttpResponseMessage> SendAsync(
        HttpRequestMessage request,
        CancellationToken cancellationToken) =>
        handler(request, cancellationToken);
}
