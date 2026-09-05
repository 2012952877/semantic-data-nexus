using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Nodes;
using ControlApi.Endpoints;
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
            input_tokens = (long?)null,
            graph = new
            {
                contract_version = "sqg/v1",
                catalog = pin ?? Pin,
                result_schema = new[] { new { name = "value", data_type = "integer" } }
            },
            clarification_id = (string?)null,
            clarification_revision = (int?)null,
            ambiguity = (object?)null,
            expires_at = (string?)null
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
        Task operation = resume
            ? client.AnswerAsync("clarification-a", new("catalog-answer/v2", "request-a", Pin, 1, "choice-a"), cts.Token)
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

    private static JsonObject Continuation()
    {
        var result = JsonSerializer.SerializeToNode(Result())!.AsObject();
        result["status"] = "clarification_required";
        result["result"] = null;
        result["run_id"] = null;
        var compilation = result["compilation"]!.AsObject();
        compilation["status"] = "clarification";
        compilation["graph"] = null;
        compilation["clarification_id"] = "clarification-a";
        compilation["clarification_revision"] = 2;
        compilation["expires_at"] = DateTimeOffset.UtcNow.AddMinutes(5).ToString("O");
        compilation["ambiguity"] = JsonSerializer.SerializeToNode(new
        {
            id = "ambiguity-a",
            term = "ambiguous",
            choices = new[]
            {
                new { id = "choice-a", label = "A", kind = "metric", target_id = "metric.a", field_id = (string?)null },
                new { id = "choice-b", label = "B", kind = "metric", target_id = "metric.b", field_id = (string?)null }
            }
        });
        return result;
    }

    [Theory]
    [InlineData("graph-pin")]
    [InlineData("schema-name")]
    [InlineData("schema-type")]
    [InlineData("schema-length")]
    [InlineData("missing-id")]
    [InlineData("missing-revision")]
    [InlineData("missing-ambiguity")]
    [InlineData("empty-choices")]
    public async Task NestedCompilationFailuresAreRejected(string defect)
    {
        var root = defect.StartsWith("schema", StringComparison.Ordinal) || defect == "graph-pin"
            ? JsonSerializer.SerializeToNode(Result())!.AsObject() : Continuation();
        var compilation = root["compilation"]!.AsObject();
        switch (defect)
        {
            case "graph-pin": compilation["graph"]!["catalog"]!["revision"] = 2; break;
            case "schema-name": compilation["graph"]!["result_schema"]![0]!["name"] = "other"; break;
            case "schema-type": compilation["graph"]!["result_schema"]![0]!["data_type"] = "number"; break;
            case "schema-length": compilation["graph"]!["result_schema"] = new JsonArray(); break;
            case "missing-id": compilation.Remove("clarification_id"); break;
            case "missing-revision": compilation.Remove("clarification_revision"); break;
            case "missing-ambiguity": compilation.Remove("ambiguity"); break;
            case "empty-choices": compilation["ambiguity"]!["choices"] = new JsonArray(); break;
        }
        using var handler = new DelegateHandler((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = JsonContent.Create(root)
        }));
        using var http = new HttpClient(handler) { BaseAddress = new("https://backend.example.test/") };
        var error = await Assert.ThrowsAsync<CatalogBackendException>(() =>
            new HttpCatalogBackendClient(http).QueryAsync(Query, CancellationToken.None));
        Assert.Equal("CATALOG_INVALID_RESPONSE", error.Code);
    }

    [Theory]
    [InlineData("success", "request")]
    [InlineData("success", "identifier")]
    [InlineData("success", "revision")]
    [InlineData("success", "choice")]
    [InlineData("success", "outcome-request")]
    [InlineData("blocked", "request")]
    [InlineData("clarification", "continuation-step")]
    [InlineData("success", "none")]
    [InlineData("clarification", "none")]
    [InlineData("blocked", "none")]
    public async Task AnswerCorrelationIsCheckedForEveryOutcomeAndReplay(string status, string defect)
    {
        var outcome = status == "clarification" ? Continuation() : JsonSerializer.SerializeToNode(Result())!.AsObject();
        if (status == "blocked")
        {
            outcome["status"] = "blocked";
            outcome["result"] = null;
            outcome["run_id"] = null;
            outcome["compilation"]!["status"] = "blocked";
            outcome["compilation"]!["graph"] = null;
        }
        var response = JsonSerializer.SerializeToNode(new
        {
            contract_version = "catalog-answer-result/v1",
            request_id = "request-a",
            clarification_id = "clarification-a",
            revision = 1,
            choice_id = "choice-a",
            outcome
        })!.AsObject();
        switch (defect)
        {
            case "request": response["request_id"] = "unrelated"; break;
            case "identifier": response["clarification_id"] = "clarification-other"; break;
            case "revision": response["revision"] = 0; break;
            case "choice": response["choice_id"] = "choice-b"; break;
            case "outcome-request": response["outcome"]!["request_id"] = "unrelated"; break;
            case "continuation-step": response["outcome"]!["compilation"]!["clarification_revision"] = 1; break;
        }
        using var handler = new DelegateHandler((_, _) => Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
        {
            Content = JsonContent.Create(response)
        }));
        using var http = new HttpClient(handler) { BaseAddress = new("https://backend.example.test/") };
        var client = new HttpCatalogBackendClient(http);
        var request = new CatalogAnswerRequest("catalog-answer/v2", "request-a", Pin, 1, "choice-a");
        if (defect == "none")
        {
            var first = await client.AnswerAsync("clarification-a", request, CancellationToken.None);
            var replay = await client.AnswerAsync("clarification-a", request, CancellationToken.None);
            Assert.Equal(first.Revision, replay.Revision);
            Assert.Equal(first.RequestId, replay.RequestId);
        }
        else
        {
            var error = await Assert.ThrowsAsync<CatalogBackendException>(() =>
                client.AnswerAsync("clarification-a", request, CancellationToken.None));
            Assert.Equal("CATALOG_INVALID_RESPONSE", error.Code);
        }
    }

    [Theory]
    [InlineData("line one\nline two\tvalue\r\n", true)]
    [InlineData(" \n\t ", false)]
    public void QueryWhitespaceMatchesTheVersionedContract(string question, bool accepted) =>
        Assert.Equal(accepted, CatalogEndpoints.ValidQuestion(question));

    [Fact]
    public void QueryCountsScalarsAndRejectsMalformedUtf16()
    {
        var twoSupplementary = char.ConvertFromUtf32(0x1F642) + char.ConvertFromUtf32(0x1F643);
        Assert.True(CatalogEndpoints.ValidQuestion(new string('a', 3998) + twoSupplementary));
        Assert.False(CatalogEndpoints.ValidQuestion(new string('a', 3999) + twoSupplementary));
        Assert.False(CatalogEndpoints.ValidQuestion("a" + '\uD800'));
        Assert.False(CatalogEndpoints.ValidQuestion("a" + '\uDC00'));
        Assert.True(CatalogEndpoints.ValidQuestion(new string('a', 4000)));
        Assert.False(CatalogEndpoints.ValidQuestion(new string('a', 4001)));
    }

    private sealed class DelegateHandler(
        Func<HttpRequestMessage, CancellationToken, Task<HttpResponseMessage>> action) : HttpMessageHandler
    {
        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken) =>
            action(request, cancellationToken);
    }
}
