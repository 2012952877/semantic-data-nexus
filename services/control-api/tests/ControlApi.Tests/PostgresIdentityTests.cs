using System.Net;
using System.Net.Http.Json;
using System.Security.Cryptography;
using ControlApi.Authentication;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Persistence;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.DependencyInjection.Extensions;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;
using Npgsql;

namespace ControlApi.Tests;

public sealed class PostgresIdentityTests : IAsyncLifetime
{
    private const string Issuer = "https://identity.example.test/realms/one";
    private const string OtherIssuer = "https://identity.example.test/realms/two";
    private readonly string schema = $"identity_test_{Guid.NewGuid():N}";
    private readonly RSA key = RSA.Create(2048);
    private NpgsqlDataSource admin = null!;
    private NpgsqlDataSource database = null!;
    private string databaseSettings = null!;

    public async Task InitializeAsync()
    {
        var configured = Environment.GetEnvironmentVariable("CONTROL_API_TEST_POSTGRES");
        Assert.False(string.IsNullOrWhiteSpace(configured), "Real PostgreSQL is required; identity tests never skip.");
        admin = NpgsqlDataSource.Create(configured!);
        await using (var setup = admin.CreateCommand($"CREATE SCHEMA {schema}"))
        {
            await setup.ExecuteNonQueryAsync();
        }
        databaseSettings = new NpgsqlConnectionStringBuilder(configured!) { SearchPath = schema }.ConnectionString;
        database = NpgsqlDataSource.Create(databaseSettings);
        await new PostgresMigrations(database).ApplyAsync(default);
        await Execute("""
            INSERT INTO identity_principals VALUES
              ('p-one','https://identity.example.test/realms/one','same-sub','',true),
              ('p-two','https://identity.example.test/realms/two','same-sub','',true);
            INSERT INTO identity_workspaces (workspace_id,tenant_id,name) VALUES
              ('workspace-a','tenant-a','Workspace A'),('workspace-b','tenant-b','Workspace B');
            INSERT INTO identity_memberships VALUES
              ('m-a','workspace-a','p-one','admin',true),
              ('m-b','workspace-b','p-one','admin',true),
              ('m-other','workspace-a','p-two','reader',true);
            """);
    }

    public async Task DisposeAsync()
    {
        key.Dispose();
        if (database is not null) await database.DisposeAsync();
        if (admin is not null)
        {
            await using var command = admin.CreateCommand($"DROP SCHEMA {schema} CASCADE");
            await command.ExecuteNonQueryAsync();
            await admin.DisposeAsync();
        }
    }

    private async Task Execute(string sql)
    {
        await using var command = database.CreateCommand(sql);
        await command.ExecuteNonQueryAsync();
    }

    private Task<TrustedContext> Context(string workspace = "workspace-a", string issuer = Issuer) =>
        new IdentityStore(database).ResolveAsync(new(issuer, "same-sub", "",
            new("oidc", "nexus-api", DateTimeOffset.UtcNow.AddMinutes(-1), DateTimeOffset.UtcNow.AddMinutes(10))),
            workspace, default);

    private PostgresRunRepository Repository(TrustedContext context) =>
        new(database, TimeProvider.System, RunAccess.Verified(context));

    [Fact]
    public async Task IdenticalRequestIdsAreScopedAndSurviveRecreation()
    {
        var a = Repository(await Context());
        var b = Repository(await Context("workspace-b"));
        var request = new CreateRunRequest("same-request", "synthetic-workload");
        var pair = await Task.WhenAll(a.CreateAsync(request, "forged-owner", default),
            a.CreateAsync(request, "forged-owner", default));
        Assert.Single(pair.Where(r => r.Created));
        var other = await b.CreateAsync(request, "forged-owner", default);
        Assert.NotEqual(pair[0].Run.Id, other.Run.Id);
        Assert.Equal("p-one", other.Run.CreatedBy);
        await database.DisposeAsync();
        database = NpgsqlDataSource.Create(databaseSettings);
        Assert.Single(await Repository(await Context()).ListAsync(100, default));
        Assert.Null(await Repository(await Context()).GetAsync(other.Run.Id, default));
    }

    [Fact]
    public async Task SameSubjectFromOtherIssuerIsNotSamePrincipal()
    {
        var one = await Context();
        var two = await Context(issuer: OtherIssuer);
        Assert.NotEqual(one.Principal.PrincipalId, two.Principal.PrincipalId);
        Assert.DoesNotContain(Policies.Contributor, two.Membership.Permissions);
        await Assert.ThrowsAsync<IdentityAccessException>(() =>
            Repository(two).CreateAsync(new("same-request", "synthetic"), "same-sub", default));
        await Execute("UPDATE identity_memberships SET role='contributor' WHERE membership_id='m-other'");
        var first = await Repository(one).CreateAsync(new("same-request", "synthetic"), "same-sub", default);
        var second = await Repository(await Context(issuer: OtherIssuer)).CreateAsync(new("same-request", "synthetic"), "same-sub", default);
        Assert.NotEqual(first.Run.Id, second.Run.Id);
    }

    [Fact]
    public async Task LegacyRowsStayQuarantinedAfterIdentityMigration()
    {
        var legacy = new PostgresRunRepository(database, TimeProvider.System, RunAccess.LegacyDevelopment());
        var run = await legacy.CreateAsync(new("legacy-request", "synthetic"), "same-sub", default);
        await new PostgresMigrations(database).ApplyAsync(default);
        var enterprise = Repository(await Context());
        Assert.Null(await enterprise.GetAsync(run.Run.Id, default));
        Assert.Empty(await enterprise.ListAsync(100, default));
        Assert.Equal(0, (await enterprise.GetStatisticsAsync(default)).TotalRuns);
        Assert.Single(await legacy.ListAsync(100, default));
    }

    [Theory]
    [InlineData("UPDATE identity_principals SET active=false WHERE principal_id='p-one'")]
    [InlineData("UPDATE identity_memberships SET active=false WHERE membership_id='m-a'")]
    [InlineData("UPDATE identity_workspaces SET active=false WHERE workspace_id='workspace-a'")]
    public async Task RevocationInvalidatesAlreadyResolvedContext(string revoke)
    {
        var repository = Repository(await Context());
        var run = await repository.CreateAsync(new("request", "synthetic"), "same-sub", default);
        await Execute(revoke);
        await Assert.ThrowsAsync<IdentityAccessException>(() => repository.GetAsync(run.Run.Id, default));
        await Assert.ThrowsAsync<IdentityAccessException>(() => repository.ListAsync(100, default));
        await Assert.ThrowsAsync<IdentityAccessException>(() => repository.GetFeedbackAsync(run.Run.Id, default));
        await Assert.ThrowsAsync<IdentityAccessException>(() => repository.GetStatisticsAsync(default));
        await Assert.ThrowsAsync<IdentityAccessException>(() => repository.RequestCancellationAsync(run.Run.Id, null, default));
    }

    [Fact]
    public async Task GroupRevocationAndIdempotentAdministrationAreTransactional()
    {
        var administrator = new IdentityAdministration(database, [Provider("one", Issuer), Provider("two", OtherIssuer)]);
        var change = new GroupChange("group-request", "group-a", "Analysts", "contributor", true, ["m-other"]);
        await administrator.GroupAsync(await Context(), change, default);
        var updated = await Context();
        await administrator.GroupAsync(updated, change, default);
        Assert.Equal(updated.Membership.Revision, (await Context()).Membership.Revision);
        var member = await Context(issuer: OtherIssuer);
        Assert.Contains(Policies.Contributor, member.Membership.Permissions);
        await administrator.GroupAsync(await Context(), change with { RequestId = "revoke-group", Active = false }, default);
        await Assert.ThrowsAsync<IdentityAccessException>(() =>
            Repository(member).CreateAsync(new("request", "synthetic"), "same-sub", default));
        Assert.DoesNotContain(Policies.Contributor, (await Context(issuer: OtherIssuer)).Membership.Permissions);
    }

    [Fact]
    public async Task CrossScopeGroupMembersRollBackWithoutRevisionOrAuditChange()
    {
        var context = await Context();
        var administration = new IdentityAdministration(database, [Provider("one", Issuer)]);
        await Assert.ThrowsAsync<Microsoft.AspNetCore.Http.BadHttpRequestException>(() =>
            administration.GroupAsync(context, new("bad-group", "group-a", "Analysts", "admin", true, ["m-b"]), default));
        Assert.Equal(context.Membership.Revision, (await Context()).Membership.Revision);
        await using var query = database.CreateCommand("SELECT count(*) FROM identity_groups");
        Assert.Equal(0L, await query.ExecuteScalarAsync());
    }

    [Fact]
    public async Task HttpSurfacesHideCrossWorkspaceIdsAndIgnoreSpoofedScopeAndRoles()
    {
        using var factory = new EnterpriseFactory(databaseSettings, key, Issuer, OtherIssuer);
        using var client = factory.CreateClient();
        client.DefaultRequestHeaders.Authorization = new("Bearer", Token(Issuer));
        client.DefaultRequestHeaders.Add("X-Workspace-Id", "workspace-a");
        var created = await client.PostAsJsonAsync("/api/v1/runs", new CreateRunRequest("http-request", "synthetic"));
        Assert.True(created.IsSuccessStatusCode, await created.Content.ReadAsStringAsync());
        var run = (await created.Content.ReadFromJsonAsync<Domain.RunMetadata>())!;
        client.DefaultRequestHeaders.Remove("X-Workspace-Id");
        client.DefaultRequestHeaders.Add("X-Workspace-Id", "workspace-b");
        client.DefaultRequestHeaders.Add("X-Dev-Subject", "p-one");
        client.DefaultRequestHeaders.Add("X-Tenant-Id", "tenant-a");
        client.DefaultRequestHeaders.Add("X-Dev-Roles", "admin");
        foreach (var suffix in new[] { "", "/detail", "/feedback", "/semantic-status" })
        {
            Assert.Equal(HttpStatusCode.NotFound, (await client.GetAsync($"/api/v1/runs/{run.Id}{suffix}")).StatusCode);
        }
        Assert.Equal(HttpStatusCode.NotFound, (await client.PostAsJsonAsync($"/api/v1/runs/{run.Id}/cancel", new { })).StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, (await client.PostAsJsonAsync($"/api/v1/runs/{run.Id}/feedback",
            new SubmitFeedbackRequest("feedback", 5, FeedbackOutcome.Helpful, [], 1))).StatusCode);
        Assert.Empty((await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs?limit=1&workspaceId=workspace-a"))!.Items);
        Assert.Equal(0, (await client.GetFromJsonAsync<Domain.RunStatistics>("/api/v1/statistics/summary"))!.TotalRuns);
        client.DefaultRequestHeaders.Authorization = new("Bearer", Token(OtherIssuer));
        Assert.Equal(HttpStatusCode.Forbidden, (await client.GetAsync("/api/v1/runs")).StatusCode);
        client.DefaultRequestHeaders.Remove("X-Workspace-Id");
        client.DefaultRequestHeaders.Add("X-Workspace-Id", "workspace-a");
        Assert.Equal(HttpStatusCode.Forbidden,
            (await client.PostAsJsonAsync("/api/v1/runs", new CreateRunRequest("unauthorized", "synthetic"))).StatusCode);
    }

    [Theory]
    [InlineData("wrong-audience")]
    [InlineData("expired")]
    [InlineData("wrong-issuer")]
    [InlineData("bad-signature")]
    public async Task InvalidTokensNeverReachWorkspaceData(string invalid)
    {
        using var factory = new EnterpriseFactory(databaseSettings, key, Issuer, OtherIssuer);
        using var client = factory.CreateClient();
        client.DefaultRequestHeaders.Authorization = new("Bearer", Token(Issuer, invalid));
        client.DefaultRequestHeaders.Add("X-Workspace-Id", "workspace-a");
        Assert.Equal(HttpStatusCode.Unauthorized, (await client.GetAsync("/api/v1/runs")).StatusCode);
    }

    private string Token(string issuer, string? invalid = null)
    {
        using var wrongKey = RSA.Create(2048);
        return new JsonWebTokenHandler().CreateToken(new SecurityTokenDescriptor
        {
            Issuer = invalid == "wrong-issuer" ? "https://unregistered.example.test" : issuer,
            Audience = invalid == "wrong-audience" ? "nexus-browser" : "nexus-api",
            IssuedAt = DateTime.UtcNow.AddMinutes(-2),
            NotBefore = DateTime.UtcNow.AddMinutes(-2),
            Expires = invalid == "expired" ? DateTime.UtcNow.AddMinutes(-1) : DateTime.UtcNow.AddMinutes(5),
            SigningCredentials = new(new RsaSecurityKey(invalid == "bad-signature" ? wrongKey : key), SecurityAlgorithms.RsaSha256),
            Claims = new Dictionary<string, object> { ["sub"] = "same-sub", ["roles"] = new[] { "admin" } }
        });
    }

    private static IdentityProvider Provider(string name, string issuer) => new()
    {
        Name = name,
        Authority = issuer,
        ClientId = "nexus-browser",
        ClientSecret = "synthetic-test-only",
        Audience = "nexus-api"
    };
}

internal sealed class EnterpriseFactory(string connection, RSA key, params string[] issuers) : WebApplicationFactory<Program>
{
    protected override void ConfigureWebHost(IWebHostBuilder builder)
    {
        builder.UseEnvironment("Development");
        builder.ConfigureAppConfiguration((_, configuration) =>
        {
            var settings = new Dictionary<string, string?>
            {
                ["LocalDevelopmentAuth:Enabled"] = "false",
                ["SemanticBackend:UseFake"] = "true",
                ["RunStorage:Provider"] = "Postgres",
                ["RunStorage:ConnectionString"] = connection,
                ["Identity:ServiceSigningKey"] = key.ExportPkcs8PrivateKeyPem()
            };
            for (var i = 0; i < issuers.Length; i++)
            {
                settings[$"Identity:Providers:{i}:Name"] = $"provider{i}";
                settings[$"Identity:Providers:{i}:Authority"] = issuers[i];
                settings[$"Identity:Providers:{i}:ClientId"] = "nexus-browser";
                settings[$"Identity:Providers:{i}:ClientSecret"] = "synthetic-test-only";
                settings[$"Identity:Providers:{i}:Audience"] = "nexus-api";
            }
            configuration.AddInMemoryCollection(settings);
        });
        builder.ConfigureTestServices(services =>
        {
            services.RemoveAll<Semantic.ISemanticBackendClient>();
            services.AddSingleton<Semantic.ISemanticBackendClient, StubSemanticBackendClient>();
            for (var i = 0; i < issuers.Length; i++)
            {
                var issuer = issuers[i];
                services.PostConfigure<JwtBearerOptions>($"access-provider{i}", options =>
                {
                    var metadata = new OpenIdConnectConfiguration { Issuer = issuer };
                    metadata.SigningKeys.Add(new RsaSecurityKey(key));
                    options.ConfigurationManager = new StaticConfigurationManager<OpenIdConnectConfiguration>(metadata);
                });
            }
        });
    }
}
