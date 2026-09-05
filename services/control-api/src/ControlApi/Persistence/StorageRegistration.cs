using Microsoft.Extensions.Diagnostics.HealthChecks;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Npgsql;

namespace ControlApi.Persistence;

public static class StorageRegistration
{
    public static IServiceCollection AddRunStorage(this IServiceCollection services, IConfiguration configuration)
    {
        // Resolve selection after the host has applied all configuration providers.
        services.TryAddSingleton(configuration);
        services.AddSingleton(provider => new StorageSettings(provider.GetRequiredService<IConfiguration>()));
        services.AddSingleton(provider => NpgsqlDataSource.Create(
            provider.GetRequiredService<StorageSettings>().ConnectionString ??
                throw new StorageConfigurationException("Postgres is not selected.")));
        services.AddSingleton<PostgresMigrations>();
        services.AddSingleton<IRunRepository>(provider =>
            provider.GetRequiredService<StorageSettings>().IsPostgres
                ? new PostgresRunRepository(provider.GetRequiredService<NpgsqlDataSource>(),
                    provider.GetRequiredService<TimeProvider>())
                : new InMemoryRunRepository(provider.GetRequiredService<TimeProvider>()));
        // Lock waiters must not exhaust the pool used by repository operations holding a lock.
        services.AddSingleton<IRunDispatchCoordinator>(provider =>
            provider.GetRequiredService<StorageSettings>().IsPostgres
                ? new PostgresDispatchCoordinator(
                    NpgsqlDataSource.Create(provider.GetRequiredService<StorageSettings>().ConnectionString!),
                    ownsDataSource: true)
                : new RunDispatchCoordinator());
        services.AddHostedService<StorageStartup>();
        services.AddHealthChecks().AddCheck<StorageReadiness>("control-storage", tags: ["ready"]);
        return services;
    }
}

internal sealed class StorageSettings
{
    public bool IsPostgres { get; }
    public string? ConnectionString { get; }

    public StorageSettings(IConfiguration configuration)
    {
        var provider = configuration["RunStorage:Provider"] ?? "Memory";
        if (provider == "Memory")
        {
            return;
        }
        if (provider != "Postgres")
        {
            throw new StorageConfigurationException("RunStorage:Provider must be Memory or Postgres.");
        }
        var connectionString = configuration["RunStorage:ConnectionString"];
        if (string.IsNullOrWhiteSpace(connectionString))
        {
            throw new StorageConfigurationException("Postgres requires RunStorage:ConnectionString via a secret reference.");
        }
        NpgsqlConnectionStringBuilder options;
        try
        {
            options = new NpgsqlConnectionStringBuilder(connectionString)
            {
                IncludeErrorDetail = false,
                LogParameters = false,
                PersistSecurityInfo = false,
                Pooling = true,
                Multiplexing = false,
                NoResetOnClose = false,
                MaxPoolSize = 32,
                MinPoolSize = 0,
                Timeout = 5,
                CommandTimeout = 30
            };
        }
        catch (ArgumentException)
        {
            throw new StorageConfigurationException("RunStorage:ConnectionString is invalid.");
        }
        IsPostgres = true;
        ConnectionString = options.ConnectionString;
    }
}

internal sealed class StorageStartup(StorageSettings settings, IServiceProvider services) : IHostedService
{
    public async Task StartAsync(CancellationToken cancellationToken)
    {
        try
        {
            if (settings.IsPostgres)
            {
                await services.GetRequiredService<PostgresMigrations>().ApplyAsync(cancellationToken);
            }
        }
        catch (NpgsqlException)
        {
            throw new StorageConfigurationException("Postgres initialization failed. Check database availability and migration permissions.");
        }
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}

internal sealed class StorageReadiness(StorageSettings settings, IServiceProvider services) : IHealthCheck
{
    public async Task<HealthCheckResult> CheckHealthAsync(
        HealthCheckContext context, CancellationToken cancellationToken = default)
    {
        if (!settings.IsPostgres)
        {
            return HealthCheckResult.Healthy();
        }
        try
        {
            await using var command = services.GetRequiredService<NpgsqlDataSource>().CreateCommand(
                "SELECT 1 FROM control_schema_versions WHERE version = 1");
            return await command.ExecuteScalarAsync(cancellationToken) is not null
                ? HealthCheckResult.Healthy()
                : HealthCheckResult.Unhealthy("Control storage schema is unavailable.");
        }
        catch (NpgsqlException)
        {
            return HealthCheckResult.Unhealthy("Control storage is unavailable.");
        }
    }
}
