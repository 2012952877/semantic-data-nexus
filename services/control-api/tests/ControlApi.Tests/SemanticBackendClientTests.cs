using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Semantic;
using Microsoft.Extensions.Logging.Abstractions;

namespace ControlApi.Tests;

public sealed class SemanticBackendClientTests
{
    [Fact]
    public async Task StartUsesTypedBoundaryAndDoesNotRetry()
    {
        var runId = RunId.Parse(
            "run_0123456789abcdef0123456789abcdef",
            provider: null);
        var start = new SemanticRunStart(
            runId,
            "request-typed",
            "synthetic-workload",
            "Compare synthetic regional revenue",
            DateTimeOffset.Parse("2026-08-15T09:00:00+08:00"),
            "Asia/Shanghai",
            CompilationMode.MonthlyRegionalComparison,
            ExecutionMode.Thread,
            OutputMode.Stream,
            "synthetic-user",
            "trace");
        var calls = 0;
        var handler = new DelegateHandler(async (request, cancellationToken) =>
        {
            calls++;
            Assert.Equal(HttpMethod.Post, request.Method);
            Assert.Equal("/v1/runs", request.RequestUri!.AbsolutePath);
            var body = await request.Content!.ReadAsStringAsync(cancellationToken);
            using var document = JsonDocument.Parse(body);
            using var fixture = JsonDocument.Parse(
                File.ReadAllText(Path.Combine(
                    AppContext.BaseDirectory,
                    "Fixtures",
                    "bff-start-request.json")));
            Assert.True(JsonElement.DeepEquals(fixture.RootElement, document.RootElement));
            var root = document.RootElement;
            Assert.Equal(runId.Value, root.GetProperty("runId").GetString());
            Assert.Equal("request-typed", root.GetProperty("clientRequestId").GetString());
            Assert.Equal("synthetic-workload", root.GetProperty("workload").GetString());
            Assert.Equal(
                "Compare synthetic regional revenue",
                root.GetProperty("question").GetString());
            Assert.Equal(
                "2026-08-15T09:00:00+08:00",
                root.GetProperty("evaluationClock").GetString());
            Assert.Equal("Asia/Shanghai", root.GetProperty("evaluationTimezone").GetString());
            Assert.Equal(
                "monthly_regional_comparison",
                root.GetProperty("compilationMode").GetString());
            Assert.Equal("thread", root.GetProperty("executionMode").GetString());
            Assert.Equal("stream", root.GetProperty("outputMode").GetString());
            Assert.Equal("synthetic-user", root.GetProperty("requestedBy").GetString());
            Assert.Equal("trace", root.GetProperty("traceId").GetString());
            Assert.Equal(11, root.EnumerateObject().Count());
            return new HttpResponseMessage(HttpStatusCode.ServiceUnavailable);
        });
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.StartAsync(
                start,
                default));

        Assert.Equal("semantic_backend_start_failed", exception.DiagnosticCode);
        Assert.Equal(1, calls);
    }

    [Fact]
    public async Task DetailUsesTypedBoundaryAndRejectsMismatchedRunId()
    {
        var requested = RunId.New();
        var options = JsonOptions();
        var handler = new DelegateHandler((request, _) =>
        {
            Assert.Equal(
                $"/v1/runs/{requested.Value}/detail",
                request.RequestUri!.AbsolutePath);
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = JsonContent.Create(
                    StubSemanticBackendClient.Detail(RunId.New()),
                    options: options)
            });
        });
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetDetailAsync(requested, default));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public async Task BackendProducedDetailFixtureDeserializesAndValidates()
    {
        var runId = RunId.Parse(
            "run_0123456789abcdef0123456789abcdef",
            provider: null);
        var fixture = File.ReadAllText(Path.Combine(
            AppContext.BaseDirectory,
            "Fixtures",
            "backend-run-detail.json"));
        var handler = new DelegateHandler((request, _) =>
        {
            Assert.Equal($"/v1/runs/{runId.Value}/detail", request.RequestUri!.AbsolutePath);
            return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(fixture, Encoding.UTF8, "application/json")
            });
        });
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var detail = await client.GetDetailAsync(runId, default);

        Assert.Equal(runId, detail.RunId);
        Assert.Equal("physical-aggregate_profit", Assert.Single(detail.PhysicalNodes).Id);
        Assert.Equal(2, detail.Result!.Columns.Count);
        Assert.Equal(SemanticScalarKind.String, detail.Result.Rows[0][0].Kind);
        Assert.Equal(SemanticScalarKind.String, detail.Result.Rows[0][1].Kind);
        Assert.Equal("2334.00", detail.Result.Rows[0][1].StringValue);
        Assert.Equal(runId, detail.Manifest!.RunId);
        Assert.Equal(4, detail.Lineage.Nodes.Count);
    }

    [Theory]
    [InlineData("runId")]
    [InlineData("question")]
    [InlineData("sqg")]
    [InlineData("physicalNodes")]
    [InlineData("lineage")]
    [InlineData("diagnostics")]
    [InlineData("sqg/version")]
    [InlineData("sqg/intent")]
    [InlineData("sqg/ontology")]
    [InlineData("sqg/resolvedMembers")]
    [InlineData("sqg/metrics")]
    [InlineData("sqg/dimensions")]
    [InlineData("sqg/filters")]
    [InlineData("sqg/filters/0/field")]
    [InlineData("sqg/filters/0/operator")]
    [InlineData("sqg/filters/0/value")]
    [InlineData("sqg/policyChecks")]
    [InlineData("physicalNodes/0/id")]
    [InlineData("physicalNodes/0/kind")]
    [InlineData("physicalNodes/0/label")]
    [InlineData("physicalNodes/0/plainLanguage")]
    [InlineData("physicalNodes/0/inputs")]
    [InlineData("physicalNodes/0/outputFields")]
    [InlineData("result/columns")]
    [InlineData("result/rows")]
    [InlineData("result/columns/0/key")]
    [InlineData("result/columns/0/label")]
    [InlineData("result/columns/0/dataType")]
    [InlineData("result/columns/0/format")]
    [InlineData("result/columns/0/nullable")]
    [InlineData("result/rowCount")]
    [InlineData("result/truncated")]
    [InlineData("manifest/resultId")]
    [InlineData("manifest/runId")]
    [InlineData("manifest/nodeId")]
    [InlineData("manifest/storage")]
    [InlineData("manifest/uri")]
    [InlineData("manifest/rowCount")]
    [InlineData("manifest/byteCount")]
    [InlineData("manifest/checksum")]
    [InlineData("manifest/committedAt")]
    [InlineData("lineage/version")]
    [InlineData("lineage/runId")]
    [InlineData("lineage/nodes")]
    [InlineData("lineage/edges")]
    [InlineData("lineage/nodes/0/id")]
    [InlineData("lineage/nodes/0/kind")]
    [InlineData("lineage/nodes/0/operation")]
    [InlineData("lineage/nodes/0/sourceAlias")]
    [InlineData("lineage/nodes/0/sourceType")]
    [InlineData("lineage/nodes/0/resultId")]
    [InlineData("lineage/nodes/0/parameters")]
    [InlineData("lineage/nodes/0/parameters/0/name")]
    [InlineData("lineage/nodes/0/parameters/0/dataType")]
    [InlineData("lineage/edges/0/source")]
    [InlineData("lineage/edges/0/target")]
    [InlineData("lineage/edges/0/relation")]
    [InlineData("diagnostics/0/sequence")]
    [InlineData("diagnostics/0/runId")]
    [InlineData("diagnostics/0/scope")]
    [InlineData("diagnostics/0/scopeId")]
    [InlineData("diagnostics/0/code")]
    [InlineData("diagnostics/0/title")]
    [InlineData("diagnostics/0/message")]
    [InlineData("diagnostics/0/recovery")]
    [InlineData("diagnostics/0/severity")]
    [InlineData("diagnostics/0/occurredAt")]
    public async Task BackendFixtureRejectsMissingRequiredMembers(string path)
    {
        var runId = RunId.Parse(
            "run_0123456789abcdef0123456789abcdef",
            provider: null);
        var root = BackendDetailFixture();
        root["lineage"]!["nodes"]![0]!["parameters"] = JsonNode.Parse(
            """[{"name":"synthetic_limit","dataType":"integer"}]""");
        root["diagnostics"] = JsonNode.Parse(
            """
            [{
              "sequence": 1,
              "runId": "run_0123456789abcdef0123456789abcdef",
              "scope": "run",
              "scopeId": "run",
              "code": "synthetic_notice",
              "title": "Synthetic notice",
              "message": "Synthetic diagnostic.",
              "recovery": "No action is required.",
              "severity": "info",
              "occurredAt": "2026-08-15T01:00:01Z"
            }]
            """);
        RemoveJsonPath(root, path);

        var exception = await GetDetailFailureAsync(root.ToJsonString(), runId);

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Theory]
    [InlineData("9007199254740992")]
    [InlineData("-9007199254740992")]
    [InlineData("9223372036854775808")]
    [InlineData("9007199254740992.0")]
    [InlineData("1e29")]
    [InlineData("-1e29")]
    public async Task BackendFixtureRejectsNumericValuesOutsideSharedRange(string value)
    {
        var runId = RunId.Parse(
            "run_0123456789abcdef0123456789abcdef",
            provider: null);
        var root = BackendDetailFixture();
        root["result"]!["columns"]![1]!["dataType"] = "float";
        root["result"]!["rows"]![0]![1] = JsonNode.Parse(value);

        var exception = await GetDetailFailureAsync(root.ToJsonString(), runId);

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public void ScalarConverterAcceptsSharedNumericBoundaries()
    {
        var positiveInteger = JsonSerializer.Deserialize<SemanticScalarValue>(
            SemanticScalarLimits.MaximumIntegerMagnitude.ToString(CultureInfo.InvariantCulture));
        var negativeInteger = JsonSerializer.Deserialize<SemanticScalarValue>(
            (-SemanticScalarLimits.MaximumIntegerMagnitude).ToString(CultureInfo.InvariantCulture));
        var smallNumber = JsonSerializer.Deserialize<SemanticScalarValue>(
            "1e-29");
        var negativeSmallNumber = JsonSerializer.Deserialize<SemanticScalarValue>(
            "-1e-29");

        Assert.Equal(SemanticScalarKind.Integer, positiveInteger!.Kind);
        Assert.Equal(SemanticScalarKind.Integer, negativeInteger!.Kind);
        Assert.Equal(SemanticScalarLimits.MaximumIntegerMagnitude, positiveInteger.IntegerValue);
        Assert.Equal(-SemanticScalarLimits.MaximumIntegerMagnitude, negativeInteger.IntegerValue);
        Assert.Equal(1e-29, smallNumber!.NumberValue);
        Assert.Equal(-1e-29, negativeSmallNumber!.NumberValue);
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            SemanticScalarValue.From(long.MaxValue));
        Assert.Throws<ArgumentOutOfRangeException>(() =>
            SemanticScalarValue.From(9_007_199_254_740_992d));
    }

    [Fact]
    public void IntegralFloatRoundTripRetainsNumberToken()
    {
        var scalar = JsonSerializer.Deserialize<SemanticScalarValue>("2334.0");

        var json = JsonSerializer.Serialize(scalar);
        var roundTripped = JsonSerializer.Deserialize<SemanticScalarValue>(json);

        Assert.Equal("2334.0", json);
        Assert.Equal(SemanticScalarKind.Number, roundTripped!.Kind);
    }

    [Fact]
    public void DetailValidatorPreservesCanonicalDecimalPrecisionAndScale()
    {
        const string exact = "1234567890123456.1200";
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Detail(runId);
        valid = valid with
        {
            Result = valid.Result! with
            {
                Rows =
                [
                    [
                        valid.Result.Rows[0][0],
                        SemanticScalarValue.From(exact)
                    ]
                ]
            }
        };

        SemanticRunDetailValidator.Validate(valid, runId);

        Assert.Equal(
            $"\"{exact}\"",
            JsonSerializer.Serialize(valid.Result.Rows[0][1]));
    }

    [Theory]
    [InlineData("+1.0")]
    [InlineData("01.0")]
    [InlineData("1.")]
    [InlineData("1e2")]
    [InlineData("-0.00")]
    [InlineData("10000000000000000000000000001")]
    [InlineData("0.00000000000000000000000000001")]
    public void DetailValidatorRejectsNonCanonicalOrUnboundedDecimals(string value)
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Detail(runId);
        var invalid = valid with
        {
            Result = valid.Result! with
            {
                Rows =
                [
                    [
                        valid.Result.Rows[0][0],
                        SemanticScalarValue.From(value)
                    ]
                ]
            }
        };

        var exception = Assert.Throws<SemanticBackendException>(() =>
            SemanticRunDetailValidator.Validate(invalid, runId));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public void DetailValidatorRejectsJsonNumberForDecimalColumn()
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Detail(runId);
        var invalid = valid with
        {
            Result = valid.Result! with
            {
                Rows =
                [
                    [
                        valid.Result.Rows[0][0],
                        SemanticScalarValue.From(1250.50d)
                    ]
                ]
            }
        };

        var exception = Assert.Throws<SemanticBackendException>(() =>
            SemanticRunDetailValidator.Validate(invalid, runId));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public void DetailValidatorRejectsUnboundedRowsAndNonScalarCells()
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Detail(runId);
        var unbounded = valid with
        {
            Result = valid.Result! with
            {
                Rows = Enumerable.Repeat(valid.Result.Rows[0], 1_001).ToArray(),
                RowCount = 1_001,
                Truncated = false
            }
        };

        var exception = Assert.Throws<SemanticBackendException>(() =>
            SemanticRunDetailValidator.Validate(unbounded, runId));
        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
        Assert.Throws<JsonException>(() =>
            JsonSerializer.Deserialize<SemanticScalarValue>("{\"unsafe\":true}"));
    }

    [Fact]
    public void DetailValidatorRejectsManifestForUnknownPhysicalNode()
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Detail(runId);
        var invalid = valid with
        {
            Manifest = valid.Manifest! with { NodeId = "unknown-node" }
        };

        var exception = Assert.Throws<SemanticBackendException>(() =>
            SemanticRunDetailValidator.Validate(invalid, runId));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public async Task DetailRejectsUnknownJsonProperties()
    {
        var runId = RunId.New();
        var payload = JsonSerializer.Serialize(
            StubSemanticBackendClient.Detail(runId),
            JsonOptions());
        payload = payload[..^1] + ",\"unbounded\":{}}";
        var handler = new DelegateHandler((_, _) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(payload, Encoding.UTF8, "application/json")
            }));
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetDetailAsync(runId, default));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public async Task DetailMapsUnpairedSurrogateToInvalidBackendResponse()
    {
        var runId = RunId.Parse(
            "run_0123456789abcdef0123456789abcdef",
            provider: null);
        var payload = File.ReadAllText(Path.Combine(
                AppContext.BaseDirectory,
                "Fixtures",
                "backend-run-detail.json"))
            .Replace(
                "\"question\": \"Compare synthetic regional revenue\"",
                "\"question\": \"\\uD800\"",
                StringComparison.Ordinal);

        var exception = await GetDetailFailureAsync(payload, runId);

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public async Task TimeoutIsMappedAndCancellationPropagates()
    {
        var handler = new DelegateHandler(async (_, cancellationToken) =>
        {
            await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
            return new HttpResponseMessage(HttpStatusCode.OK);
        });
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/"),
            Timeout = TimeSpan.FromMilliseconds(50)
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var timeout = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetStatusAsync(RunId.New(), default));
        Assert.Equal("semantic_backend_timeout", timeout.DiagnosticCode);

        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() =>
            client.GetStatusAsync(RunId.New(), cancellation.Token));
    }

    [Fact]
    public async Task MalformedJsonIsMappedToInvalidResponse()
    {
        var handler = new DelegateHandler((_, _) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent("{not-json", Encoding.UTF8, "application/json")
            }));
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        var exception = await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetStatusAsync(RunId.New(), default));

        Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
    }

    [Fact]
    public void BackendValidatorRejectsIdentityUsageCollectionAndTimelineViolations()
    {
        var runId = RunId.New();
        var valid = StubSemanticBackendClient.Status(runId, RunState.Running);
        SemanticRunStatus[] invalidStatuses =
        [
            valid with { RunId = RunId.New() },
            valid with { TokenUsage = new TokenUsage(-1, 0) },
            valid with { Stages = null! },
            valid with { Diagnostics = null! },
            valid with { StartedAt = null },
            valid with
            {
                State = RunState.Succeeded,
                FinalizedAt = null
            },
            valid with
            {
                Stages =
                [
                    new StageSummary(
                        "stage",
                        "Stage",
                        RunState.Succeeded,
                        DateTimeOffset.UtcNow,
                        DateTimeOffset.UtcNow.AddSeconds(-1),
                        [])
                ]
            }
        ];

        foreach (var status in invalidStatuses)
        {
            var exception = Assert.Throws<SemanticBackendException>(() =>
                SemanticRunStatusValidator.Validate(status, runId));
            Assert.Equal("semantic_backend_invalid_response", exception.DiagnosticCode);
            Assert.Equal(SemanticFailureKind.InvalidResponse, exception.FailureKind);
        }
    }

    private static JsonSerializerOptions JsonOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web);
        JsonContractOptions.Configure(options);
        SemanticJsonContractOptions.Configure(options);
        return options;
    }

    private static JsonObject BackendDetailFixture() =>
        JsonNode.Parse(File.ReadAllText(Path.Combine(
            AppContext.BaseDirectory,
            "Fixtures",
            "backend-run-detail.json")))!.AsObject();

    private static void RemoveJsonPath(JsonObject root, string path)
    {
        var segments = path.Split('/');
        JsonNode current = root;
        foreach (var segment in segments[..^1])
        {
            current = int.TryParse(segment, out var index)
                ? current[index]!
                : current[segment]!;
        }

        Assert.True(current.AsObject().Remove(segments[^1]));
    }

    private static async Task<SemanticBackendException> GetDetailFailureAsync(
        string payload,
        RunId runId)
    {
        var handler = new DelegateHandler((_, _) =>
            Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
            {
                Content = new StringContent(payload, Encoding.UTF8, "application/json")
            }));
        using var httpClient = new HttpClient(handler)
        {
            BaseAddress = new Uri("https://semantic.invalid/")
        };
        var client = new HttpSemanticBackendClient(
            httpClient,
            NullLogger<HttpSemanticBackendClient>.Instance);

        return await Assert.ThrowsAsync<SemanticBackendException>(() =>
            client.GetDetailAsync(runId, default));
    }
}
