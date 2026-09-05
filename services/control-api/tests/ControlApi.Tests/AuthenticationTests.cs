using System.Security.Claims;
using System.Security.Cryptography;
using ControlApi.Authentication;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Authorization;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;
using Microsoft.AspNetCore.Builder;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.TestHost;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Protocols;
using Microsoft.IdentityModel.Protocols.OpenIdConnect;
using Microsoft.IdentityModel.Tokens;

namespace ControlApi.Tests;

public sealed class AuthenticationTests
{
    [Fact]
    public async Task RegisteredAccessTokenAuthenticatesThroughActualHandler()
    {
        using var key = RSA.Create(2048);
        const string issuer = "https://identity.example.test/realms/nexus";
        var configuration = new ConfigurationBuilder().AddInMemoryCollection(new Dictionary<string, string?>
        {
            ["Identity:Providers:0:Name"] = "local",
            ["Identity:Providers:0:Authority"] = issuer,
            ["Identity:Providers:0:ClientId"] = "browser",
            ["Identity:Providers:0:Audience"] = "api",
            ["Identity:Providers:0:ClientSecret"] = "synthetic-test-only",
            ["Identity:ServiceSigningKey"] = key.ExportPkcs8PrivateKeyPem(),
            ["RunStorage:Provider"] = "Postgres"
        }).Build();
        Exception? authenticationFailure = null;
        using var server = new TestServer(new WebHostBuilder().ConfigureServices(services =>
        {
            services.AddRouting();
            services.AddControlApiAuthentication(configuration, new TestHostEnvironment("Development"));
            services.PostConfigure<JwtBearerOptions>("access-local", options =>
            {
                var metadata = new OpenIdConnectConfiguration { Issuer = issuer };
                metadata.SigningKeys.Add(new RsaSecurityKey(key));
                options.ConfigurationManager = new StaticConfigurationManager<OpenIdConnectConfiguration>(metadata);
                options.Events.OnAuthenticationFailed = context =>
                {
                    authenticationFailure = context.Exception;
                    return Task.CompletedTask;
                };
            });
        }).Configure(app =>
        {
            app.UseRouting();
            app.UseAuthentication();
            app.UseAuthorization();
            app.UseEndpoints(endpoints => endpoints.MapGet("/claims", () => "ok").RequireAuthorization());
        }));
        using var client = server.CreateClient();
        var token = new JsonWebTokenHandler().CreateToken(new SecurityTokenDescriptor
        {
            Issuer = issuer,
            Audience = "api",
            IssuedAt = DateTime.UtcNow.AddMinutes(-1),
            NotBefore = DateTime.UtcNow.AddMinutes(-1),
            Expires = DateTime.UtcNow.AddMinutes(5),
            SigningCredentials = new(new RsaSecurityKey(key), SecurityAlgorithms.RsaSha256),
            Claims = new Dictionary<string, object> { ["sub"] = "synthetic-subject" }
        });
        client.DefaultRequestHeaders.Authorization = new("Bearer", token);
        var response = await client.GetAsync("/claims");
        Assert.True(response.IsSuccessStatusCode, authenticationFailure?.ToString() ?? response.StatusCode.ToString());
    }

    [Theory]
    [InlineData("Staging")]
    [InlineData("QA")]
    public void HeaderAuthenticationRegistrationFailsOutsideDevelopment(string environmentName)
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalDevelopmentAuth:Enabled"] = "true"
            })
            .Build();
        var services = new ServiceCollection();

        var exception = Assert.Throws<InvalidOperationException>(() =>
            services.AddControlApiAuthentication(
                configuration,
                new TestHostEnvironment(environmentName)));

        Assert.Contains("only be enabled in Development", exception.Message, StringComparison.Ordinal);
    }

    [Fact]
    public async Task TokenRolesNeverAuthorizeWorkspacePoliciesWithoutServerMembership()
    {
        using var key = RSA.Create(2048);
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalDevelopmentAuth:Enabled"] = "false",
                ["Identity:Providers:0:Name"] = "local",
                ["Identity:Providers:0:Authority"] = "https://identity.example.test/realms/nexus",
                ["Identity:Providers:0:ClientId"] = "nexus-browser",
                ["Identity:Providers:0:ClientSecret"] = "synthetic-test-only",
                ["Identity:Providers:0:Audience"] = "nexus-api",
                ["Identity:ServiceSigningKey"] = key.ExportPkcs8PrivateKeyPem(),
                ["RunStorage:Provider"] = "Postgres"
            })
            .Build();
        var services = new ServiceCollection();
        services.AddLogging();
        services.AddSingleton<IConfiguration>(configuration);
        services.AddControlApiAuthentication(
            configuration,
            new TestHostEnvironment(Environments.Development));
        await using var provider = services.BuildServiceProvider();

        var bearer = provider
            .GetRequiredService<IOptionsMonitor<JwtBearerOptions>>()
            .Get("access-local");
        Assert.False(bearer.MapInboundClaims);

        var principal = new ClaimsPrincipal(new ClaimsIdentity(
        [
            new Claim("sub", "synthetic-subject"),
            new Claim("scp", "reader contributor")
        ], JwtBearerDefaults.AuthenticationScheme));
        var authorization = provider.GetRequiredService<IAuthorizationService>();

        var result = await authorization.AuthorizeAsync(
            principal,
            resource: null,
            Policies.Contributor);

        Assert.False(result.Succeeded);
        Assert.True(bearer.TokenValidationParameters.ValidateIssuer);
        Assert.True(bearer.TokenValidationParameters.ValidateAudience);
        Assert.True(bearer.TokenValidationParameters.ValidateLifetime);
        Assert.True(bearer.TokenValidationParameters.RequireSignedTokens);
    }

    internal sealed class TestHostEnvironment(string environmentName) : IHostEnvironment
    {
        public string EnvironmentName { get; set; } = environmentName;
        public string ApplicationName { get; set; } = "ControlApi.Tests";
        public string ContentRootPath { get; set; } = AppContext.BaseDirectory;
        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
    }
}
