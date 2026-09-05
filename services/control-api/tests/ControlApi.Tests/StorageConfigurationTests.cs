using ControlApi.Persistence;
using System.Diagnostics;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;

namespace ControlApi.Tests;

public sealed class StorageConfigurationTests
{
    [Fact]
    public void DefaultSelectionIsMemory()
    {
        var services = new ServiceCollection();
        services.AddSingleton(TimeProvider.System);
        services.AddRunStorage(new ConfigurationBuilder().Build());
        using var provider = services.BuildServiceProvider();
        Assert.IsType<InMemoryRunRepository>(provider.GetRequiredService<IRunRepository>());
        Assert.IsType<RunDispatchCoordinator>(provider.GetRequiredService<IRunDispatchCoordinator>());
    }

    [Theory]
    [InlineData("typo", null)]
    [InlineData("Postgres", null)]
    [InlineData("Postgres", "invalid secret content")]
    public async Task ApiConfigurationFailsClosedWithoutLeakingValues(string storage, string? connection)
    {
        var output = await FailedStartup(storage, connection);
        Assert.Contains(nameof(StorageConfigurationException), output);
        Assert.DoesNotContain("invalid secret content", output);
    }

    [Fact]
    public async Task ApiStartupFailsWhenPostgresIsUnavailable()
    {
        var output = await FailedStartup("Postgres",
            "Host=127.0.0.1;Port=1;Database=unavailable;Username=test;Password=synthetic-never-log-this");
        Assert.Contains(nameof(StorageConfigurationException), output);
        Assert.DoesNotContain("synthetic-never-log-this", output);
        Assert.Contains("initialization failed", output);
    }

    private static async Task<string> FailedStartup(string storage, string? connection)
    {
        // WebApplicationFactory races host disposal when startup fails. Exercise the real entry point
        // in an isolated process so exit status and the original startup diagnostic are both observable.
        var testAssembly = typeof(StorageConfigurationTests).Assembly.Location;
        var start = new ProcessStartInfo("dotnet")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false
        };
        start.ArgumentList.Add("exec");
        start.ArgumentList.Add("--runtimeconfig");
        start.ArgumentList.Add(Path.ChangeExtension(testAssembly, "runtimeconfig.json"));
        start.ArgumentList.Add("--depsfile");
        start.ArgumentList.Add(Path.ChangeExtension(testAssembly, "deps.json"));
        start.ArgumentList.Add(typeof(Program).Assembly.Location);
        start.Environment["ASPNETCORE_ENVIRONMENT"] = "Development";
        start.Environment["DOTNET_ENVIRONMENT"] = "Development";
        start.Environment["LocalDevelopmentAuth__Enabled"] = "true";
        start.Environment["SemanticBackend__UseFake"] = "true";
        start.Environment["RunStorage__Provider"] = storage;
        start.Environment["RunStorage__ConnectionString"] = connection ?? "";
        using var process = Process.Start(start)!;
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        try
        {
            await process.WaitForExitAsync(timeout.Token);
        }
        finally
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
            }
        }
        Assert.NotEqual(0, process.ExitCode);
        return await stdout + await stderr;
    }
}
