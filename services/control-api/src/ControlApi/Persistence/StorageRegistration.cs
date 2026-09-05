using Microsoft.Extensions.Diagnostics.HealthChecks;
using Npgsql;

namespace ControlApi.Persistence;

public static class StorageRegistration
{
    public static IServiceCollection AddRunStorage(this IServiceCollection services, IConfiguration configuration)
    {
        var provider = configuration["RunStorage:Provider"] ?? "Memory";
        if (provider == "Memory")
        {
            services.AddSingleton<IRunRepository, InMemoryRunRepository>();
            services.AddSingleton<IRunDispatchCoordinator, RunDispatchCoordinator>();
            return services;
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
        services.AddSingleton(_ => NpgsqlDataSource.Create(options.ConnectionString));
        services.AddSingleton<PostgresMigrations>();
        services.AddSingleton<IRunRepository, PostgresRunRepository>();
        services.AddSingleton<IRunDispatchCoordinator, PostgresDispatchCoordinator>();
        services.AddHostedService<PostgresStartup>();
        services.AddHealthChecks().AddCheck<PostgresReadiness>("control-storage", tags: ["ready"]);
        return services;
    }
}

internal sealed class PostgresStartup(PostgresMigrations migrations) : IHostedService
{
    public async Task StartAsync(CancellationToken cancellationToken)
    {
        try
        {
            await migrations.ApplyAsync(cancellationToken);
        }
        catch (NpgsqlException)
        {
            throw new StorageConfigurationException("Postgres initialization failed. Check database availability and migration permissions.");
        }
    }

    public Task StopAsync(CancellationToken cancellationToken) => Task.CompletedTask;
}

internal sealed class PostgresReadiness(NpgsqlDataSource dataSource) : IHealthCheck
{
    public async Task<HealthCheckResult> CheckHealthAsync(
        HealthCheckContext context, CancellationToken cancellationToken = default)
    {
        try
        {
            await using var command = dataSource.CreateCommand(
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
