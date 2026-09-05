using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using ControlApi.Semantic;

namespace ControlApi.Tests;

public sealed class CatalogBackendClientTests
{
    private static readonly CatalogPin Pin = new("resource-version/v1",
        new("synthetic-tenant", "synthetic-workspace"), "ontology", "synthetic-catalog", 1, new string('a', 64));
    private static readonly CatalogQueryRequest Query = new("catalog-compile/v1", "request-a", Pin, "Synthetic question");

    private static object Result(CatalogPin? pin = null, string requestId = "request-a") => new
    {
        contract_version = "catalog-query-result/v1",
        request_id = requestId,
        status = "succeeded",
        compilation = new
        {
            contract_version = "catalog-compilation/v1",
            catalog = pin ?? Pin,
            status = "compiled",
            input_tokens = (long?)null
        },
        run_id = "run_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        result = new
        {
            columns = new[] { new { key = "value", label = "Value", dataType = "integer", format = "number", nullable = false } },
            rows = new[] { new[] { 4L } },
            rowCount = 1,
            truncated = false
        },
        provenance = new Dictionary<string, string> { ["runtime_contract"] = "query-runtime/v1" }
    };

    [Fact]
    public async Task QueryForwardsVersionedRequestAndPreservesUnknownUsage()
    {
        using var handler = new DelegateHandler(async (request, token) =>
        {
            Assert.Equal("/v1/catalog/queries", request.RequestUri!.AbsolutePath);
            var document = JsonDocument.Parse(await request.Content!.ReadAsByteArrayAsync(token));
            Assert.Equal("catalog-compile/v1", document.RootElement.GetProperty("contract_version").GetString());
            Assert.Equal("request-a", document.RootElement.GetProperty("request_id").GetString());
            Assert.False(document.RootElement.TryGetProperty("trusted_context", out _));
            return new(HttpStatusCode.OK) { Content = JsonContent.Create(Result()) };
        });
        using var http = new HttpClient(handler) { BaseAddress = new("https://backend.example.test/") };
        var result = await new HttpCatalogBackendClient(http).QueryAsync(Query, CancellationToken.None);
        Assert.Equal(4, result.Result!.Rows[0][0].IntegerValue);
        Assert.Equal(JsonValueKind.Null, result.Compilation.GetProperty("input_tokens").ValueKind);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task BothCatalogOperationsPropagateCallerCancellation(bool resume)
    {
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var cancelled = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        using var handler = new DelegateHandler(async (_, token) =>
        {
            entered.SetResult();
            try
            {
                await Task.Delay(Timeout.InfiniteTimeSpan, token);
                throw new InvalidOperationException("Cancellation did not propagate");
            }
            catch (OperationCanceledException)
            {
                cancelled.SetResult();
                throw;
            }
        });
        using var http = new HttpClient(handler) { BaseAddress = new("https://backend.example.test/") };
        using var cts = new CancellationTokenSource();
        var client = new HttpCatalogBackendClient(http);
        var operation = resume
            ? client.AnswerAsync("clarification-a", new("catalog-answer/v1", Pin, 1, "choice-a"), cts.Token)
            : client.QueryAsync(Query, cts.Token);
        await entered.Task.WaitAsync(TimeSpan.FromSeconds(2));
        cts.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
        await cancelled.Task.WaitAsync(TimeSpan.FromSeconds(2));
    }

    [Theory]
    [InlineData("scope")]
    [InlineData("request")]
    [InlineData("scalar")]
    public async Task InvalidBackendResponseIsNotPassedThrough(string defect)
    {
        var payload = defect switch
        {
            "scope" => JsonSerializer.Serialize(Result(Pin with { Scope = new("other", "workspace") })),
            "request" => JsonSerializer.Serialize(Result(requestId: "other")),
            _ => JsonSerializer.Serialize(Result()).Replace("[[4]]", "[[9007199254740992]]", StringComparison.Ordinal)
        };
        using var handler = new DelegateHandler((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = new StringContent(payload, System.Text.Encoding.UTF8, "application/json")
        }));
        using var http = new HttpClient(handler) { BaseAddress = new("https://backend.example.test/") };
        var error = await Assert.ThrowsAsync<CatalogBackendException>(() =>
            new HttpCatalogBackendClient(http).QueryAsync(Query, CancellationToken.None));
        Assert.Equal(502, error.Status);
        Assert.Equal("CATALOG_INVALID_RESPONSE", error.Code);
    }

    [Fact]
    public async Task TypedTerminalFailureKeepsStatusAndValueFreeCode()
    {
        using var handler = new DelegateHandler((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.UnprocessableEntity)
        {
            Content = JsonContent.Create(new { detail = new { code = "RESULT_INTEGER_OUT_OF_RANGE" } })
        }));
        using var http = new HttpClient(handler) { BaseAddress = new("https://backend.example.test/") };
        var error = await Assert.ThrowsAsync<CatalogBackendException>(() =>
            new HttpCatalogBackendClient(http).QueryAsync(Query, CancellationToken.None));
        Assert.Equal(422, error.Status);
        Assert.Equal("RESULT_INTEGER_OUT_OF_RANGE", error.Code);
    }

    private sealed class DelegateHandler(
        Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> action) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            action(request, cancellationToken);
    }
}
