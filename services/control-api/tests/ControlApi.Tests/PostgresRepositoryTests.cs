using System.Net;
using System.Net.Http.Json;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Persistence;
using ControlApi.Semantic;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Npgsql;

namespace ControlApi.Tests;

// Intentionally never skipped: CI must supply a real PostgreSQL service.
public sealed class PostgresRepositoryTests : IAsyncLifetime
{
    private readonly string schema = $"test_{Guid.NewGuid():N}";
    private NpgsqlDataSource admin = null!;
    private NpgsqlDataSource first = null!;
    private NpgsqlDataSource second = null!;
    private string databaseSettings = null!;
    private PostgresRunRepository A => new(first, TimeProvider.System);
    private PostgresRunRepository B => new(second, TimeProvider.System);

    public async Task InitializeAsync()
    {
        var configured = Environment.GetEnvironmentVariable("CONTROL_API_TEST_POSTGRES");
        Assert.False(string.IsNullOrWhiteSpace(configured),
            "CONTROL_API_TEST_POSTGRES is required; PostgreSQL integration tests cannot silently skip.");
        admin = NpgsqlDataSource.Create(configured!);
        await using var command = admin.CreateCommand($"CREATE SCHEMA {schema}");
        await command.ExecuteNonQueryAsync();
        databaseSettings = new NpgsqlConnectionStringBuilder(configured!) { SearchPath = schema }.ConnectionString;
        first = NpgsqlDataSource.Create(databaseSettings);
        second = NpgsqlDataSource.Create(databaseSettings);
        await new PostgresMigrations(first).ApplyAsync(default);
    }

    public async Task DisposeAsync()
    {
        if (first is not null)
        {
            await first.DisposeAsync();
            await second.DisposeAsync();
        }
        if (admin is not null)
        {
            await using var command = admin.CreateCommand($"DROP SCHEMA {schema} CASCADE");
            await command.ExecuteNonQueryAsync();
            await admin.DisposeAsync();
        }
    }

    private static CreateRunRequest Request(string key = "create") => new(key, "synthetic-workload");
    private async Task<RunMetadata> Create() => (await A.CreateAsync(Request(), "subject", default)).Run;

    [Fact]
    public async Task RecreatedClientRetainsMetadataFeedbackAndStatistics()
    {
        var run = await Create();
        var now = DateTimeOffset.UtcNow;
        var status = new SemanticRunStatus(run.Id, RunState.Succeeded, now.AddSeconds(-10), now,
            [new StageSummary("stage", "Stage", RunState.Succeeded, now.AddSeconds(-10), now,
                [new NodeSummary("node", "scan", RunState.Succeeded, now.AddSeconds(-10), now)])],
            new TokenUsage(7, 11), [new DiagnosticSummary("done", "Completed", null, now)]);
        var updated = await A.ApplySemanticStatusAsync(run.Id, run.Version, status, default);
        await A.SubmitFeedbackAsync(run.Id,
            new SubmitFeedbackRequest("feedback", 5, FeedbackOutcome.Helpful, ["clear"], updated.Version),
            "subject", default);
        await first.DisposeAsync();
        first = NpgsqlDataSource.Create(databaseSettings);
        var loaded = await A.GetAsync(run.Id, default);
        Assert.Equal(RunState.Succeeded, loaded!.State);
        Assert.Equal("node", loaded.Stages.Single().Nodes.Single().NodeId);
        Assert.Equal(3, loaded.Version);
        Assert.Equal(run.EvaluationClock, loaded.EvaluationClock);
        Assert.Equal(run.Question, loaded.Question);
        Assert.Single(await A.GetFeedbackAsync(run.Id, default));
        var stats = await A.GetStatisticsAsync(default);
        Assert.Equal(1, stats.TotalRuns);
        Assert.Equal(1, stats.FeedbackCount);
        Assert.Equal(7, stats.TotalInputTokens);
        Assert.Equal(11, stats.TotalOutputTokens);
        Assert.Equal(10000, stats.AverageDurationMilliseconds);
    }

    [Fact]
    public async Task TwoClientsRaceDuplicateCreation()
    {
        var results = await Task.WhenAll(
            A.CreateAsync(Request(), "subject", default), B.CreateAsync(Request(), "subject", default));
        Assert.Single(results.Where(result => result.Created));
        Assert.Equal(results[0].Run.Id, results[1].Run.Id);
        var otherSubject = await B.CreateAsync(Request(), "other-subject", default);
        Assert.True(otherSubject.Created);
        Assert.NotEqual(results[0].Run.Id, otherSubject.Run.Id);
    }

    [Fact]
    public async Task TwoClientsRaceDifferentPayload()
    {
        async Task<bool> Attempt(PostgresRunRepository repository, string question)
        {
            try
            {
                await repository.CreateAsync(Request() with { Question = question }, "subject", default);
                return true;
            }
            catch (IdempotencyConflictException)
            {
                return false;
            }
        }
        var results = await Task.WhenAll(Attempt(A, "Question A"), Attempt(B, "Question B"));
        Assert.Single(results.Where(value => value));
        Assert.Single(await A.ListAsync(100, default));
    }

    [Fact]
    public async Task ConcurrentFeedbackDeduplicatesBeforeStaleVersionCheck()
    {
        var run = await Create();
        var request = new SubmitFeedbackRequest("feedback", 4, FeedbackOutcome.Helpful, ["clear"], run.Version);
        var results = await Task.WhenAll(
            A.SubmitFeedbackAsync(run.Id, request, "subject", default),
            B.SubmitFeedbackAsync(run.Id, request, "subject", default));
        Assert.Equal(results[0].RunVersion, results[1].RunVersion);
        Assert.Single(await B.GetFeedbackAsync(run.Id, default));
        Assert.Equal(2, (await B.GetAsync(run.Id, default))!.Version);
        await Assert.ThrowsAsync<IdempotencyConflictException>(() =>
            B.SubmitFeedbackAsync(run.Id, request with { Rating = 1 }, "subject", default));
        await Assert.ThrowsAsync<OptimisticConcurrencyException>(() =>
            A.SubmitFeedbackAsync(run.Id, request with { SubmissionId = "other" }, "subject", default));
    }

    [Fact]
    public async Task ConcurrentStatusUsesTransactionalCas()
    {
        var run = await Create();
        async Task<bool> Apply(PostgresRunRepository repository)
        {
            try
            {
                await repository.ApplySemanticStatusAsync(run.Id, run.Version,
                    StubSemanticBackendClient.Status(run.Id, RunState.Running), default);
                return true;
            }
            catch (OptimisticConcurrencyException)
            {
                return false;
            }
        }
        Assert.Single((await Task.WhenAll(Apply(A), Apply(B))).Where(result => result));
        Assert.Equal(2, (await B.GetAsync(run.Id, default))!.Version);
    }

    [Fact]
    public async Task CancellationGenerationAndTerminalAbsorptionSurviveRestart()
    {
        var run = await Create();
        var claim = await A.ClaimStartAsync(run.Id, default);
        var cancel = await B.RequestCancellationAsync(run.Id, claim.Run.Version, default);
        Assert.True(cancel.RequiresDispatch);
        var running = await A.ApplySemanticStatusAsync(run.Id, cancel.Run.Version,
            StubSemanticBackendClient.Status(run.Id, RunState.Running), default);
        Assert.Equal(RunState.CancelRequested, running.State);
        await Assert.ThrowsAsync<OptimisticConcurrencyException>(() =>
            B.MarkCancellationDeliveredAsync(run.Id, cancel.CancellationGeneration + 1, default));
        var terminal = await B.ApplySemanticStatusAsync(run.Id, running.Version,
            StubSemanticBackendClient.Status(run.Id, RunState.Succeeded), default);
        var lateCancel = await A.RequestCancellationAsync(run.Id, 1, default);
        Assert.False(lateCancel.Changed);
        Assert.False(lateCancel.RequiresDispatch);
        Assert.Equal(CancellationDeliveryState.Delivered, terminal.CancellationDelivery);
        var repeated = await A.ApplySemanticStatusAsync(run.Id, terminal.Version,
            StubSemanticBackendClient.Status(run.Id, RunState.Succeeded), default);
        Assert.Equal(terminal.Version, repeated.Version);
        Assert.Equal(terminal.CompletedAt, repeated.CompletedAt);
        await Assert.ThrowsAsync<InvalidRunTransitionException>(() =>
            B.ApplySemanticStatusAsync(run.Id, terminal.Version,
                StubSemanticBackendClient.Status(run.Id, RunState.Running), default));
    }

    [Fact]
    public async Task CancelBeforeDispatchDoesNotAcquireStartClaim()
    {
        var run = await Create();
        var result = await A.RequestCancellationAsync(run.Id, run.Version, default);
        Assert.Equal(RunState.Cancelled, result.Run.State);
        Assert.False((await B.ClaimStartAsync(run.Id, default)).Acquired);
    }

    [Fact]
    public async Task ClaimCommittedBeforeSenderCrashIsNeverReclaimed()
    {
        var run = await Create();
        var claim = await A.ClaimStartAsync(run.Id, default);
        Assert.True(claim.Acquired);
        await first.DisposeAsync();
        first = NpgsqlDataSource.Create(databaseSettings);
        var retry = await B.ClaimStartAsync(run.Id, default);
        Assert.False(retry.Acquired);
        Assert.Equal(RunState.DispatchUnknown, retry.Run.State);
        Assert.Contains(retry.Run.Diagnostics, item => item.Code == "durable_start_dispatch_claimed");
        var cancel = await B.RequestCancellationAsync(run.Id, retry.Run.Version, default);
        await Assert.ThrowsAsync<DispatchRecoveryRequiredException>(() =>
            A.FinalizeCancellationWithoutBackendAsync(run.Id, null, cancel.CancellationGeneration, default));
    }

    [Fact]
    public async Task LostLockConnectionDoesNotPermitSecondStartClaim()
    {
        var run = await Create();
        var options = new NpgsqlConnectionStringBuilder(databaseSettings) { ApplicationName = schema + "_lock" };
        await using var lockSource = NpgsqlDataSource.Create(options.ConnectionString);
        var coordinator = new PostgresDispatchCoordinator(lockSource);
        var lease = await coordinator.AcquireAsync(run.Id, default);
        try
        {
            Assert.True((await A.ClaimStartAsync(run.Id, default)).Acquired);
            await using var kill = admin.CreateCommand("""
                SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE application_name = $1
                """);
            kill.Parameters.AddWithValue(options.ApplicationName);
            Assert.True((bool)(await kill.ExecuteScalarAsync())!);
            await using var nextLease = await new PostgresDispatchCoordinator(second).AcquireAsync(run.Id, default);
            Assert.False((await B.ClaimStartAsync(run.Id, default)).Acquired);
        }
        finally
        {
            await lease.DisposeAsync();
        }
    }

    [Fact]
    public async Task AdvisoryLockWaitCanBeCancelledAndRetried()
    {
        var run = await Create();
        var a = new PostgresDispatchCoordinator(first);
        var b = new PostgresDispatchCoordinator(second);
        var lease = await a.AcquireAsync(run.Id, default);
        using var cancel = new CancellationTokenSource(TimeSpan.FromMilliseconds(200));
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () =>
            await b.AcquireAsync(run.Id, cancel.Token));
        await lease.DisposeAsync();
        await using var retry = await b.AcquireAsync(run.Id, default);
    }

    [Fact]
    public async Task RepeatAndConcurrentMigrationsAreSafe()
    {
        await Task.WhenAll(new PostgresMigrations(first).ApplyAsync(default),
            new PostgresMigrations(second).ApplyAsync(default));
        await using var count = first.CreateCommand("SELECT count(*) FROM control_schema_versions");
        Assert.Equal(1L, await count.ExecuteScalarAsync());
        await using var tamper = first.CreateCommand("UPDATE control_schema_versions SET checksum = 'tampered'");
        await tamper.ExecuteNonQueryAsync();
        await Assert.ThrowsAsync<StorageConfigurationException>(() => new PostgresMigrations(second).ApplyAsync(default));
    }

    [Fact]
    public async Task ConcurrentFirstStartupAppliesOneSchema()
    {
        await using (var drop = first.CreateCommand("""
            DROP TABLE control_start_dispatch, control_feedback, control_runs, control_schema_versions
            """))
        {
            await drop.ExecuteNonQueryAsync();
        }
        await Task.WhenAll(new PostgresMigrations(first).ApplyAsync(default),
            new PostgresMigrations(second).ApplyAsync(default));
        await Create();
        Assert.Single(await B.ListAsync(100, default));
    }

    [Fact]
    public async Task InvalidBackendStateRollsBackAndCorruptStoredStateFailsLoudly()
    {
        var run = await Create();
        await Assert.ThrowsAsync<SemanticBackendException>(() =>
            A.ApplySemanticStatusAsync(run.Id, run.Version,
                StubSemanticBackendClient.Status(run.Id, (RunState)999), default));
        Assert.Equal(run.Version, (await B.GetAsync(run.Id, default))!.Version);
        await using var corrupt = first.CreateCommand("""
            UPDATE control_runs SET metadata = jsonb_set(metadata, '{State}', '999') WHERE run_id = $1
            """);
        corrupt.Parameters.AddWithValue(run.Id.Value);
        await corrupt.ExecuteNonQueryAsync();
        await Assert.ThrowsAsync<StorageCorruptionException>(() => B.GetAsync(run.Id, default));
        await Assert.ThrowsAsync<StorageCorruptionException>(() => A.GetStatisticsAsync(default));
    }

    [Fact]
    public async Task CancelledDatabaseOperationDoesNotWrite()
    {
        using var cancelled = new CancellationTokenSource();
        cancelled.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            A.CreateAsync(Request(), "subject", cancelled.Token));
        Assert.Empty(await B.ListAsync(100, default));
    }

    [Fact]
    public async Task ConcurrentCancelAndStatusNeverLoseCancellationIntent()
    {
        var run = await Create();
        var running = await A.ApplySemanticStatusAsync(run.Id, run.Version,
            StubSemanticBackendClient.Status(run.Id, RunState.Running), default);
        async Task Observe()
        {
            try
            {
                await B.ApplySemanticStatusAsync(run.Id, running.Version,
                    StubSemanticBackendClient.Status(run.Id, RunState.Running), default);
            }
            catch (OptimisticConcurrencyException)
            {
                // Cancellation won the version race.
            }
        }
        await Task.WhenAll(A.RequestCancellationAsync(run.Id, null, default), Observe());
        var current = (await B.GetAsync(run.Id, default))!;
        Assert.Equal(RunState.CancelRequested, current.State);
        Assert.Equal(CancellationDeliveryState.Pending, current.CancellationDelivery);
        Assert.Equal(1, current.CancellationGeneration);
    }

    [Fact]
    public async Task FailedMigrationRollsBackDdlAndVersionRecord()
    {
        await using (var setup = first.CreateCommand("""
            DROP TABLE control_start_dispatch, control_feedback, control_runs, control_schema_versions;
            CREATE TABLE control_feedback (conflicting_column integer);
            """))
        {
            await setup.ExecuteNonQueryAsync();
        }
        await Assert.ThrowsAsync<PostgresException>(() => new PostgresMigrations(first).ApplyAsync(default));
        await using var query = second.CreateCommand(
            "SELECT to_regclass('control_runs') IS NULL AND to_regclass('control_schema_versions') IS NULL");
        Assert.True((bool)(await query.ExecuteScalarAsync())!);
        await using (var repair = first.CreateCommand("DROP TABLE control_feedback"))
        {
            await repair.ExecuteNonQueryAsync();
        }
        await new PostgresMigrations(second).ApplyAsync(default);
        await Create();
    }

    [Theory]
    [InlineData("metadata - 'State'")]
    [InlineData("jsonb_set(metadata, '{TokenUsage}', '{\"InputTokens\": 0}')")]
    [InlineData("jsonb_set(metadata, '{CancellationDelivery}', '999')")]
    [InlineData("jsonb_set(metadata, '{TokenUsage}', '{\"InputTokens\": -1, \"OutputTokens\": 0}')")]
    public async Task CorruptRequiredFieldsAndUsageNeverBecomeDefaults(string expression)
    {
        var run = await Create();
        // Expressions come exclusively from static test cases, never user input.
        await using var corrupt = first.CreateCommand($"UPDATE control_runs SET metadata = {expression} WHERE run_id = $1");
        corrupt.Parameters.AddWithValue(run.Id.Value);
        await corrupt.ExecuteNonQueryAsync();
        await Assert.ThrowsAsync<StorageCorruptionException>(() => B.GetAsync(run.Id, default));
    }

    [Fact]
    public async Task DatabaseUnavailableNeverFallsBack()
    {
        await using var unavailable = NpgsqlDataSource.Create(
            "Host=127.0.0.1;Port=1;Database=unavailable;Username=test;Timeout=1");
        var repository = new PostgresRunRepository(unavailable, TimeProvider.System);
        await Assert.ThrowsAnyAsync<NpgsqlException>(() => repository.CreateAsync(Request(), "subject", default));
        await Assert.ThrowsAnyAsync<NpgsqlException>(() => new PostgresMigrations(unavailable).ApplyAsync(default));
    }

    private Dictionary<string, string?> Settings() => new()
    {
        ["RunStorage:Provider"] = "Postgres",
        ["RunStorage:ConnectionString"] = databaseSettings
    };

    [Fact]
    public async Task ApiTwoHostsStartOnceAndReconcilePersistedRun()
    {
        var backend = new StubSemanticBackendClient();
        using var factoryA = new ControlApiFactory(backend, settings: Settings());
        using var factoryB = new ControlApiFactory(backend, settings: Settings());
        using var clientA = factoryA.CreateAuthenticatedClient("contributor");
        using var clientB = factoryB.CreateAuthenticatedClient("contributor");
        var responses = await Task.WhenAll(clientA.PostAsJsonAsync("/api/v1/runs/", Request()),
            clientB.PostAsJsonAsync("/api/v1/runs/", Request()));
        Assert.All(responses, response => Assert.True(response.IsSuccessStatusCode));
        Assert.Equal(1, backend.StartCalls);
        Assert.IsType<PostgresRunRepository>(factoryA.Services.GetRequiredService<IRunRepository>());
        Assert.Equal(HttpStatusCode.OK, (await clientB.GetAsync("/health/ready")).StatusCode);
        using var reader = factoryB.CreateAuthenticatedClient("reader");
        Assert.Equal(HttpStatusCode.Forbidden,
            (await reader.PostAsJsonAsync("/api/v1/runs/", Request("forbidden"))).StatusCode);
    }

    [Fact]
    public async Task ApiCrashWindowReturnsActionableProblemWithoutBlindRedispatch()
    {
        var run = (await A.CreateAsync(Request(), "synthetic-user", default)).Run;
        await A.ClaimStartAsync(run.Id, default);
        var backend = new StubSemanticBackendClient();
        using var factory = new ControlApiFactory(backend, settings: Settings());
        using var client = factory.CreateAuthenticatedClient("contributor");
        var response = await client.PostAsJsonAsync("/api/v1/runs/", Request());
        Assert.Equal(HttpStatusCode.ServiceUnavailable, response.StatusCode);
        Assert.Contains("dispatch_recovery_required", await response.Content.ReadAsStringAsync());
        Assert.Equal(0, backend.StartCalls);
        backend.Runs[run.Id] = StubSemanticBackendClient.Status(run.Id, RunState.Running);
        var reconciled = await client.PostAsJsonAsync("/api/v1/runs/", Request());
        Assert.Equal(HttpStatusCode.OK, reconciled.StatusCode);
        Assert.Equal(0, backend.StartCalls);
        Assert.Equal(RunState.Running, (await B.GetAsync(run.Id, default))!.State);
    }

    [Fact]
    public async Task ApiCorruptionIsStableAndReadinessReflectsDatabaseFailure()
    {
        var run = await Create();
        using var factory = new ControlApiFactory(settings: Settings());
        using var client = factory.CreateAuthenticatedClient();
        await using (var corrupt = first.CreateCommand(
            "UPDATE control_runs SET metadata = jsonb_set(metadata, '{State}', '999')"))
        {
            await corrupt.ExecuteNonQueryAsync();
        }
        var response = await client.GetAsync($"/api/v1/runs/{run.Id}");
        Assert.Equal(HttpStatusCode.InternalServerError, response.StatusCode);
        Assert.Contains("control_storage_corrupt", await response.Content.ReadAsStringAsync());
        await using (var drop = first.CreateCommand("DROP TABLE control_schema_versions"))
        {
            await drop.ExecuteNonQueryAsync();
        }
        Assert.Equal(HttpStatusCode.ServiceUnavailable, (await client.GetAsync("/health/ready")).StatusCode);
        Assert.Equal(HttpStatusCode.OK, (await client.GetAsync("/health/live")).StatusCode);
        await using (var drop = first.CreateCommand("DROP TABLE control_start_dispatch, control_feedback, control_runs"))
        {
            await drop.ExecuteNonQueryAsync();
        }
        var unavailable = await client.GetAsync($"/api/v1/runs/{run.Id}");
        Assert.Equal(HttpStatusCode.ServiceUnavailable, unavailable.StatusCode);
        var body = await unavailable.Content.ReadAsStringAsync();
        Assert.Contains("control_storage_unavailable", body);
        Assert.DoesNotContain("Npgsql", body);
        Assert.DoesNotContain(schema, body);
    }

    [Theory]
    [InlineData("Unknown", null)]
    [InlineData("Postgres", null)]
    [InlineData("Postgres", "not-a-connection-string")]
    public void InvalidConfigurationFailsAtResolution(string provider, string? connection)
    {
        var configuration = new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
        {
            ["RunStorage:Provider"] = provider,
            ["RunStorage:ConnectionString"] = connection
        }).Build();
        using var services = new ServiceCollection().AddRunStorage(configuration).BuildServiceProvider();
        Assert.Throws<StorageConfigurationException>(() => services.GetRequiredService<IRunRepository>());
    }
}
