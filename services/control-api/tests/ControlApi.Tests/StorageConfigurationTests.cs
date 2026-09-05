using ControlApi.Persistence;
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
    public void ApiConfigurationFailsClosedWithoutLeakingValues(string storage, string? connection)
    {
        using var factory = new ControlApiFactory(settings: new Dictionary<string, string?>
        {
            ["RunStorage:Provider"] = storage,
            ["RunStorage:ConnectionString"] = connection
        });
        var exception = Assert.Throws<StorageConfigurationException>(() => factory.CreateClient());
        Assert.DoesNotContain("invalid secret content", exception.ToString());
    }

    [Fact]
    public void ApiStartupFailsWhenPostgresIsUnavailable()
    {
        using var factory = new ControlApiFactory(settings: new Dictionary<string, string?>
        {
            ["RunStorage:Provider"] = "Postgres",
            ["RunStorage:ConnectionString"] = "Host=127.0.0.1;Port=1;Database=unavailable;Username=test;Password=synthetic-never-log-this"
        });
        var exception = Assert.Throws<StorageConfigurationException>(() => factory.CreateClient());
        Assert.DoesNotContain("synthetic-never-log-this", exception.ToString());
        Assert.Contains("initialization failed", exception.Message);
    }
}
