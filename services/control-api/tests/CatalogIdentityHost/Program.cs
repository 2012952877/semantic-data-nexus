using System.Net;
using System.Net.Security;
using System.Net.Sockets;
using System.Reflection;
using System.Security.Cryptography.X509Certificates;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.AspNetCore.Authentication.OpenIdConnect;
using Microsoft.AspNetCore.Hosting;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

[assembly: HostingStartup(typeof(CatalogIdentityHost.LocalIdentityStartup))]

namespace CatalogIdentityHost;

public static class EntryPoint
{
    public static async Task Main(string[] args)
    {
        Environment.SetEnvironmentVariable("ASPNETCORE_HOSTINGSTARTUPASSEMBLIES",
            typeof(LocalIdentityStartup).Assembly.GetName().Name);
        Environment.SetEnvironmentVariable("ASPNETCORE_APPLICATIONNAME", "ControlApi");
        var entry = Assembly.Load("ControlApi").EntryPoint ??
            throw new InvalidOperationException("ControlApi entry point is missing.");
        if (entry.Invoke(null, [args]) is Task task)
        {
            await task;
        }
    }
}

public sealed class LocalIdentityStartup : IHostingStartup
{
    public void Configure(IWebHostBuilder builder)
    {
        builder.ConfigureServices((context, services) =>
        {
            if (!context.HostingEnvironment.IsDevelopment())
            {
                throw new InvalidOperationException("The local identity test host is development-only.");
            }
            var ca = Environment.GetEnvironmentVariable("NEXUS_IDENTITY_FIXTURE_CA") ??
                throw new InvalidOperationException("An explicit disposable test CA is required.");
            services.ConfigureAll<OpenIdConnectOptions>(options =>
                options.BackchannelHttpHandler = new FixtureTlsHandler(ca));
            services.ConfigureAll<JwtBearerOptions>(options =>
                options.BackchannelHttpHandler = new FixtureTlsHandler(ca));
        });
    }
}

internal sealed class FixtureTlsHandler : DelegatingHandler
{
    private readonly X509Certificate2 root;

    public FixtureTlsHandler(string path)
    {
        root = X509Certificate2.CreateFromPem(File.ReadAllText(path));
        InnerHandler = new SocketsHttpHandler
        {
            AllowAutoRedirect = false,
            SslOptions = new SslClientAuthenticationOptions
            {
                CertificateChainPolicy = new X509ChainPolicy
                {
                    TrustMode = X509ChainTrustMode.CustomRootTrust,
                    CustomTrustStore = { root },
                    RevocationMode = X509RevocationMode.NoCheck
                }
            },
            ConnectCallback = async (context, cancellationToken) =>
            {
                if (context.DnsEndPoint.Host != "identity.localhost" || context.DnsEndPoint.Port != 8443)
                {
                    throw new InvalidOperationException("The test transport only resolves its local IdP.");
                }
                Socket? socket = new(SocketType.Stream, ProtocolType.Tcp);
                try
                {
                    await socket.ConnectAsync(new IPEndPoint(IPAddress.Loopback, 8443), cancellationToken);
                    var stream = new NetworkStream(socket, ownsSocket: true);
                    socket = null;
                    return stream;
                }
                finally
                {
                    socket?.Dispose();
                }
            }
        };
    }

    protected override void Dispose(bool disposing)
    {
        base.Dispose(disposing);
        if (disposing)
        {
            root.Dispose();
        }
    }
}
