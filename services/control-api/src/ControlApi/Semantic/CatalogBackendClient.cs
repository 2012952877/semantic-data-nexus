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
    [property: JsonRequired, JsonPropertyName("request_id")] string RequestId,
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

public sealed record CatalogAnswerResponse(
    [property: JsonRequired, JsonPropertyName("contract_version")] string ContractVersion,
    [property: JsonRequired, JsonPropertyName("request_id")] string RequestId,
    [property: JsonRequired, JsonPropertyName("clarification_id")] string ClarificationId,
    [property: JsonRequired, JsonPropertyName("revision")] int Revision,
    [property: JsonRequired, JsonPropertyName("choice_id")] string ChoiceId,
    [property: JsonRequired, JsonPropertyName("outcome")] CatalogQueryResponse Outcome);

public interface ICatalogBackendClient
{
    Task<CatalogQueryResponse> QueryAsync(CatalogQueryRequest request, CancellationToken cancellationToken);
    Task<CatalogAnswerResponse> AnswerAsync(string identifier, CatalogAnswerRequest request, CancellationToken cancellationToken);
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
    public Task<CatalogAnswerResponse> AnswerAsync(string identifier, CatalogAnswerRequest request, CancellationToken cancellationToken) =>
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

    public async Task<CatalogQueryResponse> QueryAsync(CatalogQueryRequest request, CancellationToken cancellationToken)
    {
        var result = await SendAsync<CatalogQueryRequest, CatalogQueryResponse>(
            "v1/catalog/queries", request, cancellationToken);
        CatalogResponseValidation.Validate(result, request.Catalog, request.RequestId, null);
        return result;
    }

    public async Task<CatalogAnswerResponse> AnswerAsync(
        string identifier, CatalogAnswerRequest request, CancellationToken cancellationToken)
    {
        var result = await SendAsync<CatalogAnswerRequest, CatalogAnswerResponse>(
            $"v1/catalog/clarifications/{Uri.EscapeDataString(identifier)}/answers",
            request, cancellationToken);
        if (result.ContractVersion != "catalog-answer-result/v1" ||
            result.RequestId != request.RequestId || result.ClarificationId != identifier ||
            result.Revision != request.Revision || result.ChoiceId != request.ChoiceId)
        {
            throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
        }
        CatalogResponseValidation.Validate(result.Outcome, request.Catalog, request.RequestId,
            (identifier, request.Revision + 1));
        return result;
    }

    private async Task<TResponse> SendAsync<TRequest, TResponse>(
        string path, TRequest request, CancellationToken cancellationToken)
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
            var result = await response.Content.ReadFromJsonAsync<TResponse>(Json, cancellationToken)
                ?? throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
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
