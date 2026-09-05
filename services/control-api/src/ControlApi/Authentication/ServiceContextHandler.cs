using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Tokens;

namespace ControlApi.Authentication;

public sealed class ServiceContextSigner : IDisposable
{
    public const string Issuer = "https://control.example.test";
    public const string Audience = "semantic-backend";
    private readonly RSA rsa;
    private readonly SigningCredentials credentials;

    public ServiceContextSigner(IConfiguration configuration)
    {
        rsa = RSA.Create();
        var material = configuration["Identity:ServiceSigningKey"];
        if (string.IsNullOrWhiteSpace(material))
        {
            throw new InvalidOperationException("Identity:ServiceSigningKey must be supplied by a secret reference.");
        }
        rsa.ImportFromPem(material);
        if (rsa.KeySize < 2048)
        {
            throw new InvalidOperationException("Service signing requires an RSA key of at least 2048 bits.");
        }
        credentials = new SigningCredentials(new RsaSecurityKey(rsa), SecurityAlgorithms.RsaSha256);
    }

    public string Sign(TrustedContext context, string method, string path, byte[] body)
    {
        var now = DateTime.UtcNow;
        var expiry = context.Authentication.ExpiresAt.UtcDateTime < now.AddSeconds(30)
            ? context.Authentication.ExpiresAt.UtcDateTime : now.AddSeconds(30);
        if (expiry <= now)
        {
            throw new IdentityAccessException();
        }
        return new JsonWebTokenHandler().CreateToken(new SecurityTokenDescriptor
        {
            Issuer = Issuer,
            Audience = Audience,
            IssuedAt = now,
            NotBefore = now,
            Expires = expiry,
            TokenType = "nexus-service+jwt",
            SigningCredentials = credentials,
            Claims = new Dictionary<string, object>
            {
                ["jti"] = Guid.NewGuid().ToString("N"),
                ["htm"] = method,
                ["htu"] = path,
                ["bh"] = Convert.ToHexString(SHA256.HashData(body)).ToLowerInvariant(),
                ["ctx"] = JsonSerializer.SerializeToElement(context, TrustedContext.Json)
            }
        });
    }

    public void Dispose() => rsa.Dispose();
}

public sealed class ServiceContextHandler(RunAccess access, ServiceContextSigner signer) : DelegatingHandler
{
    protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
    {
        if (!request.RequestUri!.AbsolutePath.StartsWith("/health/", StringComparison.Ordinal))
        {
            var body = request.Content is null ? [] : await request.Content.ReadAsByteArrayAsync(cancellationToken);
            request.Headers.Authorization = new("Bearer",
                signer.Sign(access.Context!, request.Method.Method, request.RequestUri.PathAndQuery, body));
        }
        return await base.SendAsync(request, cancellationToken);
    }
}
