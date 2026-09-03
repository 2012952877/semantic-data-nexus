using System.Net;
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
    IReadOnlyDictionary<string, string?>? settings = null)
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

public sealed class StubSemanticBackendClient : ISemanticBackendClient
{
    public bool Ready { get; set; } = true;
    public Exception? StartException { get; set; }
    public Exception? StatusException { get; set; }
    public Exception? CancelException { get; set; }
    public int StartCalls { get; private set; }
    public int CancelCalls { get; private set; }
    public Dictionary<RunId, SemanticRunStatus> Runs { get; } = [];

    public Task<SemanticRunStatus> StartAsync(
        SemanticRunStart request,
        CancellationToken cancellationToken)
    {
        StartCalls++;
        if (StartException is not null)
        {
            throw StartException;
        }

        var status = Status(request.RunId, RunState.Starting);
        Runs[request.RunId] = status;
        return Task.FromResult(status);
    }

    public Task<SemanticRunStatus> GetStatusAsync(
        RunId runId,
        CancellationToken cancellationToken)
    {
        if (StatusException is not null)
        {
            throw StatusException;
        }

        return Task.FromResult(Runs[runId]);
    }

    public Task RequestCancellationAsync(RunId runId, CancellationToken cancellationToken)
    {
        CancelCalls++;
        if (CancelException is not null)
        {
            throw CancelException;
        }

        Runs[runId] = Status(runId, RunState.Cancelled);
        return Task.CompletedTask;
    }

    public Task<bool> IsReadyAsync(CancellationToken cancellationToken) =>
        Task.FromResult(Ready);

    public static SemanticRunStatus Status(RunId id, RunState state) =>
        new(id, state, [], new TokenUsage(0, 0), [], state.IsTerminal() ? DateTimeOffset.UtcNow : null);
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
