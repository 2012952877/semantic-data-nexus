using System.Security.Claims;
using ControlApi.Authentication;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Authorization;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.FileProviders;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Options;

namespace ControlApi.Tests;

public sealed class AuthenticationTests
{
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
    public async Task EntraBearerKeepsRawScopeClaimsAndPoliciesAuthorizeThem()
    {
        var configuration = new ConfigurationBuilder()
            .AddInMemoryCollection(new Dictionary<string, string?>
            {
                ["LocalDevelopmentAuth:Enabled"] = "false",
                ["AzureAd:Instance"] = "https://login.microsoftonline.com/",
                ["AzureAd:TenantId"] = Guid.NewGuid().ToString(),
                ["AzureAd:ClientId"] = Guid.NewGuid().ToString()
            })
            .Build();
        var services = new ServiceCollection();
        services.AddLogging();
        services.AddSingleton<IConfiguration>(configuration);
        services.AddControlApiAuthentication(
            configuration,
            new TestHostEnvironment(Environments.Staging));
        await using var provider = services.BuildServiceProvider();

        var bearer = provider
            .GetRequiredService<IOptionsMonitor<JwtBearerOptions>>()
            .Get(JwtBearerDefaults.AuthenticationScheme);
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

        Assert.True(result.Succeeded);
    }

    private sealed class TestHostEnvironment(string environmentName) : IHostEnvironment
    {
        public string EnvironmentName { get; set; } = environmentName;
        public string ApplicationName { get; set; } = "ControlApi.Tests";
        public string ContentRootPath { get; set; } = AppContext.BaseDirectory;
        public IFileProvider ContentRootFileProvider { get; set; } = new NullFileProvider();
    }
}
