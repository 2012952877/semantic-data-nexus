using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Text.RegularExpressions;
using ControlApi.Authentication;
using ControlApi.Domain;

namespace ControlApi.Semantic;

public sealed record CatalogScope(
    [property: JsonRequired, JsonPropertyName("tenant_id")] string TenantId,
    [property: JsonRequired, JsonPropertyName("workspace_id")] string WorkspaceId);

public sealed record CatalogPin(
    [property: JsonRequired, JsonPropertyName("contract_version")] string ContractVersion,
    [property: JsonRequired, JsonPropertyName("scope")] CatalogScope Scope,
    [property: JsonRequired, JsonPropertyName("resource_kind")] string ResourceKind,
    [property: JsonRequired, JsonPropertyName("resource_id")] string ResourceId,
    [property: JsonRequired, JsonPropertyName("revision")] long Revision,
    [property: JsonRequired, JsonPropertyName("content_sha256")] string ContentSha256);

public sealed record CatalogQueryRequest(
    [property: JsonRequired, JsonPropertyName("contract_version")] string ContractVersion,
    [property: JsonRequired, JsonPropertyName("request_id")] string RequestId,
    [property: JsonRequired, JsonPropertyName("catalog")] CatalogPin Catalog,
    [property: JsonRequired, JsonPropertyName("question")] string Question);

public sealed record CatalogAnswerRequest(
    [property: JsonRequired, JsonPropertyName("contract_version")] string ContractVersion,
    [property: JsonRequired, JsonPropertyName("catalog")] CatalogPin Catalog,
    [property: JsonRequired, JsonPropertyName("revision")] int Revision,
    [property: JsonRequired, JsonPropertyName("choice_id")] string ChoiceId);

public sealed record CatalogQueryResponse(
    [property: JsonRequired, JsonPropertyName("contract_version")] string ContractVersion,
    [property: JsonRequired, JsonPropertyName("request_id")] string RequestId,
    [property: JsonRequired, JsonPropertyName("status")] string Status,
    [property: JsonRequired, JsonPropertyName("compilation")] JsonElement Compilation,
    [property: JsonRequired, JsonPropertyName("run_id")] RunId? RunId,
    [property: JsonRequired, JsonPropertyName("result")] SemanticResultSet? Result,
    [property: JsonRequired, JsonPropertyName("provenance")] IReadOnlyDictionary<string, string> Provenance);

public interface ICatalogBackendClient
{
    Task<CatalogQueryResponse> QueryAsync(CatalogQueryRequest request, CancellationToken cancellationToken);
    Task<CatalogQueryResponse> AnswerAsync(string identifier, CatalogAnswerRequest request, CancellationToken cancellationToken);
}

public sealed class CatalogBackendException(int status, string code)
    : Exception("The catalog request could not be completed.")
{
    public int Status { get; } = status;
    public string Code { get; } = code;
}

public sealed class DisabledCatalogBackendClient : ICatalogBackendClient
{
    public Task<CatalogQueryResponse> QueryAsync(CatalogQueryRequest request, CancellationToken cancellationToken) =>
        throw new CatalogBackendException(503, "CATALOG_NOT_CONFIGURED");
    public Task<CatalogQueryResponse> AnswerAsync(string identifier, CatalogAnswerRequest request, CancellationToken cancellationToken) =>
        throw new CatalogBackendException(503, "CATALOG_NOT_CONFIGURED");
}

public sealed class HttpCatalogBackendClient(HttpClient client) : ICatalogBackendClient
{
    private static readonly JsonSerializerOptions Json = CreateJson();

    private static JsonSerializerOptions CreateJson()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web)
        {
            PropertyNameCaseInsensitive = false,
            UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
        };
        SemanticJsonContractOptions.Configure(options);
        return options;
    }

    public Task<CatalogQueryResponse> QueryAsync(CatalogQueryRequest request, CancellationToken cancellationToken) =>
        SendAsync("v1/catalog/queries", request, request.Catalog, request.RequestId, cancellationToken);

    public Task<CatalogQueryResponse> AnswerAsync(
        string identifier, CatalogAnswerRequest request, CancellationToken cancellationToken) =>
        SendAsync($"v1/catalog/clarifications/{Uri.EscapeDataString(identifier)}/answers",
            request, request.Catalog, null, cancellationToken);

    private async Task<CatalogQueryResponse> SendAsync<T>(
        string path, T request, CatalogPin expectedPin, string? requestId, CancellationToken cancellationToken)
    {
        try
        {
            using var message = new HttpRequestMessage(HttpMethod.Post, path)
            {
                Content = JsonContent.Create(request, options: Json)
            };
            using var response = await client.SendAsync(message, HttpCompletionOption.ResponseContentRead, cancellationToken);
            if (!response.IsSuccessStatusCode)
            {
                var status = (int)response.StatusCode;
                var code = status switch
                {
                    401 => "AUTHENTICATION_REQUIRED",
                    403 => "AUTHORIZATION_DENIED",
                    _ => "CATALOG_BACKEND_FAILED"
                };
                if (response.StatusCode is not (HttpStatusCode.Unauthorized or HttpStatusCode.Forbidden))
                {
                    using var error = JsonDocument.Parse(await response.Content.ReadAsByteArrayAsync(cancellationToken));
                    if (error.RootElement.TryGetProperty("detail", out var detail) &&
                        detail.ValueKind == JsonValueKind.Object &&
                        detail.TryGetProperty("code", out var value) &&
                        value.ValueKind == JsonValueKind.String &&
                        Regex.IsMatch(value.GetString()!, "^[A-Z0-9_]{1,64}\\z"))
                    {
                        code = value.GetString()!;
                    }
                }
                throw new CatalogBackendException(
                    status is 400 or 401 or 403 or 404 or 409 or 410 or 413 or 422 or 429 or 503 or 504 ? status : 502, code);
            }
            var result = await response.Content.ReadFromJsonAsync<CatalogQueryResponse>(Json, cancellationToken)
                ?? throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
            if (result.ContractVersion != "catalog-query-result/v1" ||
                result.RequestId is null ||
                !Regex.IsMatch(result.RequestId, "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\\z") ||
                (requestId is not null && result.RequestId != requestId) ||
                result.Status is not ("succeeded" or "clarification_required" or "blocked") ||
                result.Compilation.ValueKind != JsonValueKind.Object ||
                result.Compilation.GetProperty("contract_version").GetString() != "catalog-compilation/v1" ||
                result.Compilation.GetProperty("catalog").Deserialize<CatalogPin>(Json) != expectedPin ||
                result.Provenance is null || result.Provenance.Count > 16 ||
                result.Provenance.Any(pair => pair.Key.Length > 128 || pair.Value.Length > 2048))
            {
                throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
            }
            var compiled = result.Compilation.GetProperty("status").GetString();
            if (result.Status == "succeeded")
            {
                if (compiled != "compiled" || result.Result is null || result.RunId is null)
                {
                    throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
                }
                SemanticRunDetailValidator.ValidateResult(result.Result);
            }
            else if (result.Result is not null || result.RunId is not null ||
                compiled != (result.Status == "blocked" ? "blocked" : "clarification"))
            {
                throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
            }
            return result;
        }
        catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
        {
            throw new CatalogBackendException(504, "CATALOG_TIMEOUT");
        }
        catch (HttpRequestException)
        {
            throw new CatalogBackendException(503, "CATALOG_UNAVAILABLE");
        }
        catch (SemanticBackendException)
        {
            throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
        }
        catch (Exception exception) when (exception is JsonException or KeyNotFoundException or InvalidOperationException)
        {
            throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
        }
    }
}
