using System.Net;
using System.Net.Http.Json;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using ControlApi.Contracts;
using ControlApi.Domain;
using ControlApi.Semantic;
using Microsoft.AspNetCore.Mvc;

namespace ControlApi.Tests;

public sealed class ApiEndpointTests
{
    private static readonly JsonSerializerOptions JsonOptions = CreateJsonOptions();

    [Fact]
    public async Task ContributorCanCreateListReadCancelAndSubmitFeedback()
    {
        var backend = new StubSemanticBackendClient();
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var createResponse = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("request-001", "synthetic-workload"),
            JsonOptions);
        Assert.Equal(HttpStatusCode.Accepted, createResponse.StatusCode);
        var created = await createResponse.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        Assert.NotNull(created);
        Assert.Equal(RunState.Starting, created.State);

        var duplicateResponse = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("request-001", "synthetic-workload"),
            JsonOptions);
        Assert.Equal(HttpStatusCode.OK, duplicateResponse.StatusCode);
        Assert.Equal(1, backend.StartCalls);

        var getResponse = await client.GetAsync($"/api/v1/runs/{created.Id}");
        Assert.Equal(HttpStatusCode.OK, getResponse.StatusCode);

        backend.Runs[created.Id] = StubSemanticBackendClient.Status(created.Id, RunState.Running);
        var statusResponse = await client.GetAsync($"/api/v1/runs/{created.Id}/semantic-status");
        Assert.Equal(HttpStatusCode.OK, statusResponse.StatusCode);
        var refreshed = await statusResponse.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        Assert.Equal(RunState.Running, refreshed!.State);

        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        Assert.NotNull(list);
        Assert.Single(list.Items);

        var feedbackResponse = await client.PostAsJsonAsync(
            $"/api/v1/runs/{created.Id}/feedback",
            new SubmitFeedbackRequest(
                "feedback-001",
                5,
                FeedbackOutcome.Helpful,
                ["clear"],
                refreshed.Version),
            JsonOptions);
        Assert.Equal(HttpStatusCode.OK, feedbackResponse.StatusCode);
        var feedback = await feedbackResponse.Content.ReadFromJsonAsync<RunFeedback>(JsonOptions);
        Assert.NotNull(feedback);

        var cancelResponse = await client.PostAsJsonAsync(
            $"/api/v1/runs/{created.Id}/cancel",
            new CancelRunRequest(feedback.RunVersion),
            JsonOptions);
        Assert.Equal(HttpStatusCode.Accepted, cancelResponse.StatusCode);

        var repeatedCancel = await client.PostAsJsonAsync(
            $"/api/v1/runs/{created.Id}/cancel",
            new CancelRunRequest(1),
            JsonOptions);
        Assert.Equal(HttpStatusCode.Accepted, repeatedCancel.StatusCode);
        Assert.Equal(1, backend.CancelCalls);

        var feedbackItems = await client.GetFromJsonAsync<RunFeedback[]>(
            $"/api/v1/runs/{created.Id}/feedback",
            JsonOptions);
        Assert.Single(feedbackItems!);

        using var adminClient = factory.CreateAuthenticatedClient("admin");
        var statistics = await adminClient.GetFromJsonAsync<RunStatistics>(
            "/api/v1/statistics/summary",
            JsonOptions);
        Assert.NotNull(statistics);
        Assert.Equal(1, statistics.TotalRuns);
        Assert.Equal(1, statistics.FeedbackCount);
    }

    [Fact]
    public async Task CurrentPrincipalReturnsSyntheticClaims()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("reader,contributor");

        var principal = await client.GetFromJsonAsync<CurrentPrincipalResponse>("/api/v1/me");

        Assert.NotNull(principal);
        Assert.Equal("synthetic-user", principal.Subject);
        Assert.Contains("contributor", principal.Roles);
    }

    [Fact]
    public async Task MissingIdentityIsUnauthorizedAndReaderCannotCreate()
    {
        await using var factory = new ControlApiFactory();
        using var anonymousClient = factory.CreateClient();
        var unauthorized = await anonymousClient.GetAsync("/api/v1/me");
        Assert.Equal(HttpStatusCode.Unauthorized, unauthorized.StatusCode);
        var unauthorizedProblem = await unauthorized.Content.ReadFromJsonAsync<ProblemDetails>();
        Assert.Equal("authentication_required", Extension(unauthorizedProblem!, "code"));

        using var readerClient = factory.CreateAuthenticatedClient("reader");
        var forbidden = await readerClient.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("request-002", "synthetic-workload"));
        Assert.Equal(HttpStatusCode.Forbidden, forbidden.StatusCode);
        var forbiddenProblem = await forbidden.Content.ReadFromJsonAsync<ProblemDetails>();
        Assert.Equal("authorization_denied", Extension(forbiddenProblem!, "code"));

        var adminDenied = await readerClient.GetAsync("/api/v1/statistics/summary");
        Assert.Equal(HttpStatusCode.Forbidden, adminDenied.StatusCode);
    }

    [Fact]
    public async Task ReaderCanGetTypedRunDetail()
    {
        var backend = new StubSemanticBackendClient();
        await using var factory = new ControlApiFactory(backend);
        using var contributor = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest("detail-success") with
        {
            Question = "Compare synthetic regional revenue"
        };
        var create = await contributor.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);
        var run = await create.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        using var reader = factory.CreateAuthenticatedClient("reader");
        var response = await reader.GetAsync($"/api/v1/runs/{run!.Id}/detail");
        var detail = await response.Content.ReadFromJsonAsync<SemanticRunDetail>(JsonOptions);

        Assert.Equal(HttpStatusCode.OK, response.StatusCode);
        Assert.Equal(run.Id, detail!.RunId);
        Assert.Equal(request.Question, detail.Question);
        Assert.Equal(1, backend.DetailCalls);
    }

    [Fact]
    public async Task DetailRequiresAuthentication()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateClient();

        var response = await client.GetAsync($"/api/v1/runs/{RunId.New()}/detail");

        Assert.Equal(HttpStatusCode.Unauthorized, response.StatusCode);
    }

    [Theory]
    [InlineData("semantic_backend_timeout", HttpStatusCode.GatewayTimeout)]
    [InlineData("semantic_backend_detail_failed", HttpStatusCode.BadGateway)]
    public async Task DetailFailuresMapToStableProblems(
        string code,
        HttpStatusCode expectedStatus)
    {
        var backend = new StubSemanticBackendClient();
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var create = await client.PostAsJsonAsync(
            "/api/v1/runs",
            ValidCreateRequest($"detail-{code}"),
            JsonOptions);
        var run = await create.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        backend.DetailException = new SemanticBackendException(code, "Synthetic detail failure.");

        var response = await client.GetAsync($"/api/v1/runs/{run!.Id}/detail");
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(expectedStatus, response.StatusCode);
        Assert.Equal(code, Extension(problem!, "code"));
    }

    [Fact]
    public async Task DetailQuestionMismatchIsRejected()
    {
        var backend = new StubSemanticBackendClient();
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var create = await client.PostAsJsonAsync(
            "/api/v1/runs",
            ValidCreateRequest("detail-question"),
            JsonOptions);
        var run = await create.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        backend.DetailResult = StubSemanticBackendClient.Detail(run!.Id, "Different question");

        var response = await client.GetAsync($"/api/v1/runs/{run.Id}/detail");
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Equal("semantic_backend_invalid_response", Extension(problem!, "code"));
    }

    [Fact]
    public async Task InvalidRunIdReturnsStableProblemDetailsAndTraceId()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient();
        client.DefaultRequestHeaders.Add("X-Correlation-ID", "test-correlation-001");

        var response = await client.GetAsync("/api/v1/runs/not-a-run-id");

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();
        Assert.NotNull(problem);
        Assert.Equal("invalid_run_id", Extension(problem, "code"));
        Assert.False(string.IsNullOrWhiteSpace(Extension(problem, "traceId")));
        Assert.Equal("test-correlation-001", Extension(problem, "correlationId"));
    }

    [Theory]
    [InlineData("question", " ", "invalid_question")]
    [InlineData("evaluationTimezone", "Not//AZone", "invalid_evaluation_timezone")]
    public async Task InvalidRunRequestFieldsReturnStableProblems(
        string field,
        string value,
        string expectedCode)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest($"invalid-{field}");
        request = field switch
        {
            "question" => request with { Question = value },
            "evaluationTimezone" => request with { EvaluationTimezone = value },
            _ => throw new InvalidOperationException("Unsupported test field.")
        };

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal(expectedCode, Extension(problem!, "code"));
    }

    [Fact]
    public async Task EvaluationClockWithoutOffsetIsRejected()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        using var content = new StringContent(
            """
            {
              "clientRequestId": "invalid-clock",
              "workload": "synthetic-workload",
              "question": "Compare synthetic regional revenue",
              "evaluationClock": "2026-08-15T09:00:00",
              "evaluationTimezone": "Asia/Shanghai",
              "compilationMode": "regional_quarterly_profit",
              "executionMode": "thread",
              "outputMode": "normal"
            }
            """,
            System.Text.Encoding.UTF8,
            "application/json");

        var response = await client.PostAsync("/api/v1/runs", content);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Theory]
    [InlineData("clientRequestId")]
    [InlineData("workload")]
    [InlineData("question")]
    [InlineData("evaluationClock")]
    [InlineData("evaluationTimezone")]
    [InlineData("compilationMode")]
    [InlineData("executionMode")]
    [InlineData("outputMode")]
    public async Task MissingCreateFieldsAreRejected(string missingField)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = JsonSerializer.SerializeToNode(
            ValidCreateRequest("missing-field"),
            JsonOptions)!.AsObject();
        Assert.True(request.Remove(missingField));

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Fact]
    public async Task UnknownCreateMemberIsRejected()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = JsonSerializer.SerializeToNode(
            ValidCreateRequest("unknown-member"),
            JsonOptions)!.AsObject();
        request["executionOptions"] = new JsonObject();

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Theory]
    [InlineData("clientRequestId")]
    [InlineData("workload")]
    public async Task MetadataCannotStartWithPunctuation(string field)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest("leading-punctuation");
        request = field switch
        {
            "clientRequestId" => request with { ClientRequestId = "_request" },
            "workload" => request with { Workload = ".workload" },
            _ => throw new InvalidOperationException("Unsupported test field.")
        };

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
    }

    [Theory]
    [InlineData("\u0001")]
    [InlineData("\u200B")]
    [InlineData("\uE000")]
    public async Task QuestionRejectsUnicodeCategoryCCharacters(string disallowed)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest("category-c") with
        {
            Question = $"Synthetic{disallowed} question"
        };

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_question", Extension(problem!, "code"));
    }

    [Fact]
    public async Task QuestionLimitCountsUnicodeScalars()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest("unicode-limit") with
        {
            Question = string.Concat(Enumerable.Repeat("\U0001F680", 4_000))
        };

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, response.StatusCode);
    }

    [Theory]
    [InlineData("Asia/Shanghai")]
    [InlineData("America/New_York")]
    [InlineData("Etc/UTC")]
    [InlineData("UTC")]
    [InlineData("Synthetic/Zone")]
    public async Task IanaTimeZonesAreAcceptedWithInvariantGlobalization(string timeZone)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest($"iana-{timeZone.Replace('/', '-')}")
            with
        { EvaluationTimezone = timeZone };

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, response.StatusCode);
    }

    [Fact]
    public async Task WindowsTimeZoneIdIsNotAcceptedAsIana()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest("windows-timezone")
            with
        { EvaluationTimezone = "Eastern Standard Time" };

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_evaluation_timezone", Extension(problem!, "code"));
    }

    [Theory]
    [InlineData("CET")]
    [InlineData("GMT")]
    [InlineData("Japan")]
    [InlineData("Asia//Shanghai")]
    [InlineData("Asia/Shang.hai")]
    [InlineData("Asia/ComponentLongerThan14")]
    public async Task NonCanonicalIanaAliasesAreRejected(string timeZone)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = ValidCreateRequest("invalid-iana-alias") with
        {
            EvaluationTimezone = timeZone
        };

        var response = await client.PostAsJsonAsync("/api/v1/runs", request, JsonOptions);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_evaluation_timezone", Extension(problem!, "code"));
    }

    [Fact]
    public async Task UnpairedSurrogateInRequestMapsToBadRequest()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        using var content = new StringContent(
            """
            {
              "clientRequestId": "invalid-surrogate",
              "workload": "synthetic-workload",
              "question": "\uD800",
              "evaluationClock": "2026-08-15T09:00:00+08:00",
              "evaluationTimezone": "UTC",
              "compilationMode": "regional_quarterly_profit",
              "executionMode": "thread",
              "outputMode": "normal"
            }
            """,
            Encoding.UTF8,
            "application/json");

        var response = await client.PostAsync("/api/v1/runs", content);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Fact]
    public async Task UnpairedSurrogateInEvaluationClockMapsToBadRequest()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        using var content = new StringContent(
            """
            {
              "clientRequestId": "invalid-clock-surrogate",
              "workload": "synthetic-workload",
              "question": "Synthetic question",
              "evaluationClock": "\uD800",
              "evaluationTimezone": "UTC",
              "compilationMode": "regional_quarterly_profit",
              "executionMode": "thread",
              "outputMode": "normal"
            }
            """,
            Encoding.UTF8,
            "application/json");

        var response = await client.PostAsync("/api/v1/runs", content);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Theory]
    [InlineData("enum")]
    [InlineData("property")]
    public async Task UnpairedSurrogateInEnumOrPropertyNameMapsToBadRequest(string location)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var payload =
            """
            {
              "clientRequestId": "invalid-json-unicode",
              "workload": "synthetic-workload",
              "question": "Synthetic question",
              "evaluationClock": "2026-08-15T09:00:00+08:00",
              "evaluationTimezone": "UTC",
              "compilationMode": "regional_quarterly_profit",
              "executionMode": "thread",
              "outputMode": "normal"
            }
            """;
        payload = location == "enum"
            ? payload.Replace(
                "\"compilationMode\": \"regional_quarterly_profit\"",
                "\"compilationMode\": \"\\uD800\"",
                StringComparison.Ordinal)
            : payload.TrimEnd()[..^1] + ",\n  \"\\uD800\": true\n}";
        using var content = new StringContent(payload, Encoding.UTF8, "application/json");

        var response = await client.PostAsync("/api/v1/runs", content);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Fact]
    public async Task ValidUtf16JsonRequestRemainsSupported()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var payload = JsonSerializer.Serialize(
            ValidCreateRequest("utf16-json"),
            JsonOptions);
        using var content = new ByteArrayContent(Encoding.Unicode.GetBytes(payload));
        content.Headers.ContentType =
            new System.Net.Http.Headers.MediaTypeHeaderValue("application/json")
            {
                CharSet = "utf-16"
            };

        var response = await client.PostAsync("/api/v1/runs", content);

        Assert.Equal(HttpStatusCode.Accepted, response.StatusCode);
    }

    [Theory]
    [InlineData("utf-7")]
    [InlineData("x-unsupported-charset")]
    public async Task UnsupportedJsonCharsetMapsToBadRequest(string charset)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var payload = JsonSerializer.Serialize(
            ValidCreateRequest("unsupported-charset"),
            JsonOptions);
        using var content = new ByteArrayContent(Encoding.UTF8.GetBytes(payload));
        content.Headers.ContentType =
            new System.Net.Http.Headers.MediaTypeHeaderValue("application/json")
            {
                CharSet = charset
            };

        var response = await client.PostAsync("/api/v1/runs", content);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Theory]
    [InlineData("semantic_backend_timeout", HttpStatusCode.GatewayTimeout)]
    [InlineData("semantic_backend_unavailable", HttpStatusCode.BadGateway)]
    public async Task SemanticFailuresMapToStableProblems(string code, HttpStatusCode expectedStatus)
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(code, "Synthetic backend failure.")
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var response = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest($"request-{code}", "synthetic-workload"));

        Assert.Equal(expectedStatus, response.StatusCode);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();
        Assert.Equal(code, Extension(problem!, "code"));
    }

    [Fact]
    public async Task ReadinessReflectsSemanticBackend()
    {
        var backend = new StubSemanticBackendClient { Ready = false };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateClient();

        var live = await client.GetAsync("/health/live");
        var ready = await client.GetAsync("/health/ready");

        Assert.Equal(HttpStatusCode.OK, live.StatusCode);
        Assert.Equal(HttpStatusCode.ServiceUnavailable, ready.StatusCode);
    }

    [Fact]
    public async Task RateLimitReturnsExplicitProblemDetails()
    {
        await using var factory = new ControlApiFactory(
            settings: new Dictionary<string, string?>
            {
                ["RateLimiting:PermitLimit"] = "1",
                ["RateLimiting:WindowSeconds"] = "60"
            });
        using var client = factory.CreateAuthenticatedClient();

        var first = await client.GetAsync("/api/v1/me");
        var rejected = await client.GetAsync("/api/v1/me");

        Assert.Equal(HttpStatusCode.OK, first.StatusCode);
        Assert.Equal(HttpStatusCode.TooManyRequests, rejected.StatusCode);
        var problem = await rejected.Content.ReadFromJsonAsync<ProblemDetails>();
        Assert.Equal("rate_limit_exceeded", Extension(problem!, "code"));
    }

    [Fact]
    public async Task AuthenticationFailuresAreRateLimitedByClientAddress()
    {
        await using var factory = new ControlApiFactory(
            settings: new Dictionary<string, string?>
            {
                ["RateLimiting:PermitLimit"] = "1",
                ["RateLimiting:WindowSeconds"] = "60"
            });
        using var client = factory.CreateClient();

        var first = await client.GetAsync("/api/v1/me");
        var rejected = await client.GetAsync("/api/v1/me");

        Assert.Equal(HttpStatusCode.Unauthorized, first.StatusCode);
        Assert.Equal(HttpStatusCode.TooManyRequests, rejected.StatusCode);
    }

    [Fact]
    public async Task FailedCancellationDispatchCanBeRetried()
    {
        var backend = new StubSemanticBackendClient();
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var createResponse = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("cancel-retry", "synthetic-workload"));
        var created = await createResponse.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        backend.CancelException = new SemanticBackendException(
            "semantic_backend_unavailable",
            "Synthetic backend failure.");
        var failed = await client.PostAsJsonAsync(
            $"/api/v1/runs/{created!.Id}/cancel",
            new CancelRunRequest(created.Version));
        Assert.Equal(HttpStatusCode.BadGateway, failed.StatusCode);

        backend.CancelException = null;
        var retried = await client.PostAsJsonAsync(
            $"/api/v1/runs/{created.Id}/cancel",
            new CancelRunRequest(created.Version));
        Assert.Equal(HttpStatusCode.Accepted, retried.StatusCode);
        Assert.Equal(2, backend.CancelCalls);

        var delivered = await retried.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        Assert.Equal(CancellationDeliveryState.Delivered, delivered!.CancellationDelivery);
        var repeated = await client.PostAsJsonAsync(
            $"/api/v1/runs/{created.Id}/cancel",
            new CancelRunRequest(created.Version));
        Assert.Equal(HttpStatusCode.Accepted, repeated.StatusCode);
        Assert.Equal(2, backend.CancelCalls);
    }

    [Fact]
    public async Task AmbiguousStartIsReconciledByRunIdOnDuplicate()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic timeout.")
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("start-reconcile", "synthetic-workload");

        var failed = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, failed.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var pending = Assert.Single(list!.Items);
        Assert.Equal(RunState.DispatchUnknown, pending.State);

        backend.Runs[pending.Id] = StubSemanticBackendClient.Status(pending.Id, RunState.Running);
        backend.StartException = null;
        var reconciled = await client.PostAsJsonAsync("/api/v1/runs", request);
        var run = await reconciled.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.OK, reconciled.StatusCode);
        Assert.Equal(pending.Id, run!.Id);
        Assert.Equal(RunState.Running, run.State);
        Assert.Equal(1, backend.StartCalls);
        Assert.Equal(1, backend.StatusCalls);
    }

    [Fact]
    public async Task LostStartResponseReconcilesBackendCancellation()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic lost start response.")
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("start-cancelled-reconcile", "synthetic-workload");

        var failed = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, failed.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var unknown = Assert.Single(list!.Items);
        Assert.Equal(RunState.DispatchUnknown, unknown.State);

        backend.Runs[unknown.Id] =
            StubSemanticBackendClient.Status(unknown.Id, RunState.Cancelled);
        backend.StartException = null;
        var reconciled = await client.PostAsJsonAsync("/api/v1/runs", request);
        var run = await reconciled.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.OK, reconciled.StatusCode);
        Assert.Equal(unknown.Id, run!.Id);
        Assert.Equal(RunState.Cancelled, run.State);
        Assert.Equal(1, backend.StartCalls);
        Assert.Equal(1, backend.StatusCalls);
    }

    [Fact]
    public async Task UnknownStartRetriesSameRunIdWhenReconciliationFindsNothing()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_unavailable",
                "Synthetic connection failure.")
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("start-retry", "synthetic-workload");

        var failed = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.BadGateway, failed.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var pending = Assert.Single(list!.Items);

        backend.StartException = null;
        var retried = await client.PostAsJsonAsync("/api/v1/runs", request);
        var started = await retried.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.OK, retried.StatusCode);
        Assert.Equal(pending.Id, started!.Id);
        Assert.Equal(2, backend.StartCalls);
        Assert.Equal(1, backend.StatusCalls);
    }

    [Fact]
    public async Task OnlyDefinitiveStartRejectionMarksRunFailed()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_start_failed",
                "Synthetic rejection.",
                HttpStatusCode.BadRequest,
                SemanticFailureKind.Rejected)
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var response = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("start-rejected", "synthetic-workload"));
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);

        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Equal(RunState.Failed, Assert.Single(list!.Items).State);
    }

    [Fact]
    public async Task InvalidStartResponseIsStableAndRemainsReconcileable()
    {
        var backend = new StubSemanticBackendClient
        {
            StartResult = StubSemanticBackendClient.Status(RunId.New(), RunState.Running)
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var response = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("start-invalid", "synthetic-workload"));
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);

        Assert.Equal(HttpStatusCode.BadGateway, response.StatusCode);
        Assert.Equal("semantic_backend_invalid_response", Extension(problem!, "code"));
        Assert.Equal(RunState.DispatchUnknown, Assert.Single(list!.Items).State);
    }

    [Fact]
    public async Task InterruptedStartPersistsUnknownDispatchState()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new OperationCanceledException("Synthetic client cancellation.")
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var response = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("start-cancelled", "synthetic-workload"));
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);

        Assert.Equal(HttpStatusCode.InternalServerError, response.StatusCode);
        Assert.Equal(RunState.DispatchUnknown, Assert.Single(list!.Items).State);
    }

    [Fact]
    public async Task InvalidFeedbackShapeReturnsProblemDetails()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var createResponse = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("feedback-validation", "synthetic-workload"));
        var created = await createResponse.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        var invalidJson = JsonContent.Create(new
        {
            submissionId = "feedback-invalid",
            rating = 4,
            outcome = "Helpful",
            reasonCodes = (string[]?)null,
            expectedRunVersion = created!.Version
        });
        var response = await client.PostAsync(
            $"/api/v1/runs/{created.Id}/feedback",
            invalidJson);

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();
        Assert.Equal("invalid_feedback", Extension(problem!, "code"));
    }

    [Fact]
    public async Task MissingFeedbackOutcomeReturnsProblemDetails()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var createResponse = await client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("feedback-missing-outcome", "synthetic-workload"));
        var created = await createResponse.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        var request = JsonContent.Create(new
        {
            submissionId = "feedback-missing-outcome",
            rating = 5,
            reasonCodes = Array.Empty<string>(),
            expectedRunVersion = created!.Version
        });

        var response = await client.PostAsync(
            $"/api/v1/runs/{created.Id}/feedback",
            request);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Theory]
    [InlineData("submissionId")]
    [InlineData("rating")]
    [InlineData("outcome")]
    [InlineData("reasonCodes")]
    [InlineData("expectedRunVersion")]
    public async Task MissingFeedbackFieldsAreRejected(string missingField)
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = JsonSerializer.SerializeToNode(
            new SubmitFeedbackRequest(
                "feedback-required",
                5,
                FeedbackOutcome.Helpful,
                ["clear"],
                1),
            JsonOptions)!.AsObject();
        Assert.True(request.Remove(missingField));

        var response = await client.PostAsJsonAsync(
            $"/api/v1/runs/{RunId.New()}/feedback",
            request,
            JsonOptions);
        var problem = await response.Content.ReadFromJsonAsync<ProblemDetails>();

        Assert.Equal(HttpStatusCode.BadRequest, response.StatusCode);
        Assert.Equal("invalid_request", Extension(problem!, "code"));
    }

    [Fact]
    public async Task OpenApiDescribesRequiredRunOptionsFeedbackAndDetail()
    {
        await using var factory = new ControlApiFactory();
        using var client = factory.CreateClient();

        using var document = JsonDocument.Parse(
            await client.GetStringAsync("/swagger/v1/swagger.json"));
        var schemas = document.RootElement
            .GetProperty("components")
            .GetProperty("schemas");
        var feedbackRequired = schemas
            .GetProperty(nameof(SubmitFeedbackRequest))
            .GetProperty("required")
            .EnumerateArray()
            .Select(item => item.GetString())
            .ToArray();
        var createSchema = schemas.GetProperty(nameof(CreateRunRequest));
        var createRequired = createSchema
            .GetProperty("required")
            .EnumerateArray()
            .Select(item => item.GetString())
            .ToArray();
        var createProperties = createSchema.GetProperty("properties");
        var detailOperation = document.RootElement
            .GetProperty("paths")
            .GetProperty("/api/v1/runs/{runId}/detail")
            .GetProperty("get");

        Assert.Equal(
            new[]
            {
                "submissionId",
                "rating",
                "outcome",
                "reasonCodes",
                "expectedRunVersion"
            }.Order(),
            feedbackRequired.Order());
        Assert.Contains("question", createRequired);
        Assert.Contains("evaluationClock", createRequired);
        Assert.Contains("evaluationTimezone", createRequired);
        Assert.Contains("compilationMode", createRequired);
        Assert.Contains("executionMode", createRequired);
        Assert.Contains("outputMode", createRequired);
        Assert.True(createProperties.TryGetProperty("question", out _));
        Assert.True(createProperties.TryGetProperty("evaluationClock", out _));
        var detailResponses = detailOperation.GetProperty("responses");
        Assert.True(detailResponses.TryGetProperty("200", out var detailResponse));
        Assert.Equal(
            "#/components/schemas/SemanticRunDetail",
            detailResponse
                .GetProperty("content")
                .GetProperty("application/json")
                .GetProperty("schema")
                .GetProperty("$ref")
                .GetString());
        Assert.True(
            detailOperation
                .GetProperty("security")[0]
                .TryGetProperty("Bearer", out var scopes));
        Assert.Equal(JsonValueKind.Array, scopes.ValueKind);
        Assert.Equal(
            "http",
            document.RootElement
                .GetProperty("components")
                .GetProperty("securitySchemes")
                .GetProperty("Bearer")
                .GetProperty("type")
                .GetString());
        var runIdSchema = schemas
            .GetProperty(nameof(SemanticRunDetail))
            .GetProperty("properties")
            .GetProperty("runId");
        Assert.Equal("string", runIdSchema.GetProperty("type").GetString());
        Assert.Equal(
            "^run_[0-9a-f]{32}$",
            runIdSchema.GetProperty("pattern").GetString());
        var scalarSchema = schemas
            .GetProperty(nameof(SemanticResultSet))
            .GetProperty("properties")
            .GetProperty("rows")
            .GetProperty("items")
            .GetProperty("items");
        var scalarAlternatives = scalarSchema.GetProperty("anyOf").EnumerateArray().ToArray();
        Assert.Equal(
            ["string", "integer", "number", "boolean"],
            scalarAlternatives.Select(item => item.GetProperty("type").GetString()).ToArray());
        Assert.True(scalarAlternatives[0].GetProperty("nullable").GetBoolean());
        Assert.Equal("int64", scalarAlternatives[1].GetProperty("format").GetString());
        Assert.Equal(
            SemanticScalarLimits.MaximumIntegerMagnitude,
            scalarAlternatives[1].GetProperty("maximum").GetInt64());
        Assert.Equal("double", scalarAlternatives[2].GetProperty("format").GetString());
        Assert.Equal(
            SemanticScalarLimits.MaximumNumberMagnitude,
            scalarAlternatives[2].GetProperty("maximum").GetDecimal());
        Assert.Equal(
            "integer",
            scalarAlternatives[2].GetProperty("not").GetProperty("type").GetString());
        var detailRequired = schemas
            .GetProperty(nameof(SemanticRunDetail))
            .GetProperty("required")
            .EnumerateArray()
            .Select(item => item.GetString())
            .ToArray();
        Assert.Equal(
            new[] { "runId", "question", "sqg", "physicalNodes", "lineage", "diagnostics" }
                .Order(),
            detailRequired.Order());
        var detailProperties = schemas.GetProperty(nameof(SemanticRunDetail))
            .GetProperty("properties");
        Assert.False(
            detailProperties.GetProperty("question").TryGetProperty("nullable", out var questionNullable) &&
            questionNullable.GetBoolean());
        Assert.True(detailProperties.GetProperty("result").GetProperty("nullable").GetBoolean());
        Assert.True(detailProperties.GetProperty("manifest").GetProperty("nullable").GetBoolean());
        Assert.DoesNotContain("result", detailRequired);
        Assert.DoesNotContain("manifest", detailRequired);
        var resultRequired = schemas.GetProperty(nameof(SemanticResultSet))
            .GetProperty("required")
            .EnumerateArray()
            .Select(item => item.GetString())
            .ToArray();
        Assert.Equal(
            new[] { "columns", "rows", "rowCount", "truncated" }.Order(),
            resultRequired.Order());
        var lineageNodeSchema = schemas.GetProperty(nameof(SemanticLineageNode));
        Assert.Contains(
            "operation",
            lineageNodeSchema
                .GetProperty("required")
                .EnumerateArray()
                .Select(item => item.GetString()));
        Assert.True(
            lineageNodeSchema
                .GetProperty("properties")
                .GetProperty("operation")
                .GetProperty("nullable")
                .GetBoolean());
        Assert.Equal(
            ["SOURCE", "SELECT", "FILTER", "AGGREGATE", "PIVOT", "DERIVE", "PROJECT", "SORT", "LIMIT", "JOIN"],
            schemas
                .GetProperty(nameof(SemanticPhysicalNode))
                .GetProperty("properties")
                .GetProperty("kind")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
        Assert.Contains(
            "decimal",
            schemas
                .GetProperty(nameof(SemanticResultColumn))
                .GetProperty("properties")
                .GetProperty("dataType")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString()));
        Assert.Contains(
            "realized_as",
            schemas
                .GetProperty(nameof(SemanticLineageEdge))
                .GetProperty("properties")
                .GetProperty("relation")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString()));
        Assert.Equal(
            ["info", "warning", "error"],
            schemas
                .GetProperty(nameof(SemanticDetailDiagnostic))
                .GetProperty("properties")
                .GetProperty("severity")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
        Assert.Equal(
            ["regional_quarterly_profit", "monthly_regional_comparison"],
            createProperties
                .GetProperty("compilationMode")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
        Assert.Equal(
            ["thread"],
            createProperties
                .GetProperty("executionMode")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
        Assert.Equal(
            ["normal", "stream"],
            createProperties
                .GetProperty("outputMode")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
        Assert.Equal(
            ["Helpful", "PartiallyHelpful", "NotHelpful"],
            schemas
                .GetProperty(nameof(SubmitFeedbackRequest))
                .GetProperty("properties")
                .GetProperty("outcome")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
        var metadataProperties = schemas
            .GetProperty(nameof(RunMetadata))
            .GetProperty("properties");
        Assert.Equal(
            [
                "StartPending",
                "DispatchUnknown",
                "Queued",
                "Starting",
                "Running",
                "CancelRequested",
                "Cancelled",
                "Succeeded",
                "Failed"
            ],
            metadataProperties
                .GetProperty("state")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
        Assert.Equal(
            ["NotRequested", "Pending", "Delivered"],
            metadataProperties
                .GetProperty("cancellationDelivery")
                .GetProperty("enum")
                .EnumerateArray()
                .Select(item => item.GetString())
                .ToArray());
    }

    [Fact]
    public async Task StartCompletesBeforeConcurrentCancellationDispatch()
    {
        var backend = new StubSemanticBackendClient
        {
            StartEntered = NewGate(),
            ReleaseStart = NewGate(),
            CancelEntered = NewGate()
        };
        var coordinator = new ObservableRunDispatchCoordinator();
        await using var factory = new ControlApiFactory(
            backend,
            dispatchCoordinator: coordinator);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var createTask = client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("start-cancel-race", "synthetic-workload"));
        await backend.StartEntered.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var pending = Assert.Single(list!.Items);

        var cancelTask = client.PostAsJsonAsync(
            $"/api/v1/runs/{pending.Id}/cancel",
            new CancelRunRequest(null));
        await coordinator.WaitForAttemptAsync(2).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.False(backend.CancelEntered.Task.IsCompleted);

        backend.ReleaseStart.TrySetResult(true);
        var created = await createTask;
        await backend.CancelEntered.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var cancelled = await cancelTask;
        var cancellation = await cancelled.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, created.StatusCode);
        Assert.Equal(HttpStatusCode.Accepted, cancelled.StatusCode);
        Assert.Equal(RunState.CancelRequested, cancellation!.State);
        Assert.Equal(CancellationDeliveryState.Delivered, cancellation.CancellationDelivery);
        Assert.Equal(["start", "cancel"], backend.Operations.ToArray());
    }

    [Fact]
    public async Task CancellationWinningBeforeReconciliationPreventsRunRevival()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic timeout.")
        };
        var coordinator = new ObservableRunDispatchCoordinator();
        await using var factory = new ControlApiFactory(
            backend,
            dispatchCoordinator: coordinator);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("cancel-reconcile-race", "synthetic-workload");

        var failed = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, failed.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var unknown = Assert.Single(list!.Items);
        backend.Runs[unknown.Id] =
            StubSemanticBackendClient.Status(unknown.Id, RunState.Running);
        backend.StartException = null;
        backend.CancelEntered = NewGate();
        backend.ReleaseCancel = NewGate();

        var cancelTask = client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        await backend.CancelEntered.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var duplicateTask = client.PostAsJsonAsync("/api/v1/runs", request);
        await coordinator.WaitForAttemptAsync(3).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.False(duplicateTask.IsCompleted);
        Assert.Equal(1, backend.StartCalls);
        Assert.Equal(1, backend.StatusCalls);

        backend.ReleaseCancel.TrySetResult(true);
        var cancelled = await cancelTask;
        var duplicate = await duplicateTask;
        var replay = await duplicate.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        var repeatedCancellation = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));

        Assert.Equal(HttpStatusCode.Accepted, cancelled.StatusCode);
        Assert.Equal(HttpStatusCode.OK, duplicate.StatusCode);
        Assert.Equal(HttpStatusCode.Accepted, repeatedCancellation.StatusCode);
        Assert.Equal(RunState.CancelRequested, replay!.State);
        Assert.Equal(CancellationDeliveryState.Delivered, replay.CancellationDelivery);
        Assert.Equal(1, backend.StartCalls);
        Assert.Equal(1, backend.StatusCalls);
        Assert.Equal(1, backend.CancelCalls);
    }

    [Fact]
    public async Task UnknownStartCancellationFinalizesLocallyWhenBackendConfirmsAbsence()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic timeout.")
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("cancel-absent-start", "synthetic-workload");

        var failed = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, failed.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var unknown = Assert.Single(list!.Items);

        var cancelled = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var result = await cancelled.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        var replay = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var replayResult = await replay.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, cancelled.StatusCode);
        Assert.Equal(HttpStatusCode.Accepted, replay.StatusCode);
        Assert.Equal(RunState.Cancelled, result!.State);
        Assert.Equal(CancellationDeliveryState.Delivered, result.CancellationDelivery);
        Assert.Equal(result.Version, replayResult!.Version);
        Assert.Equal(1, backend.StatusCalls);
        Assert.Equal(0, backend.CancelCalls);
    }

    [Fact]
    public async Task MissingCancellationAndStatusFinalizeUnknownStartIdempotently()
    {
        var notFound = new SemanticBackendException(
            "semantic_backend_status_failed",
            "Synthetic run was not found.",
            HttpStatusCode.NotFound,
            SemanticFailureKind.NotFound);
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic timeout."),
            CancelException = notFound
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("cancel-missing-recheck", "synthetic-workload");

        var failed = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, failed.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var unknown = Assert.Single(list!.Items);
        backend.StatusResponses.Enqueue(id =>
            StubSemanticBackendClient.Status(id, RunState.Running));
        backend.StatusResponses.Enqueue(_ => throw notFound);

        var cancelled = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var result = await cancelled.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        var replay = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var replayResult = await replay.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, cancelled.StatusCode);
        Assert.Equal(HttpStatusCode.Accepted, replay.StatusCode);
        Assert.Equal(RunState.Cancelled, result!.State);
        Assert.Equal(CancellationDeliveryState.Delivered, result.CancellationDelivery);
        Assert.Equal(result.Version, replayResult!.Version);
        Assert.Equal(2, backend.StatusCalls);
        Assert.Equal(1, backend.CancelCalls);
    }

    [Fact]
    public async Task TransientUnknownStartReconciliationPersistsCancellationIntent()
    {
        var unavailable = new SemanticBackendException(
            "semantic_backend_unavailable",
            "Synthetic backend failure.");
        var notFound = new SemanticBackendException(
            "semantic_backend_status_failed",
            "Synthetic run was not found.",
            HttpStatusCode.NotFound,
            SemanticFailureKind.NotFound);
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic timeout."),
            StatusException = unavailable
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("cancel-transient-reconcile", "synthetic-workload");

        var failedStart = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, failedStart.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var unknown = Assert.Single(list!.Items);

        var failedCancel = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var afterFailure = await client.GetFromJsonAsync<RunMetadata>(
            $"/api/v1/runs/{unknown.Id}",
            JsonOptions);
        var duplicate = await client.PostAsJsonAsync("/api/v1/runs", request);
        var duplicateRun = await duplicate.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.BadGateway, failedCancel.StatusCode);
        Assert.Equal(RunState.CancelRequested, afterFailure!.State);
        Assert.Equal(CancellationDeliveryState.Pending, afterFailure.CancellationDelivery);
        Assert.Equal(HttpStatusCode.OK, duplicate.StatusCode);
        Assert.Equal(RunState.CancelRequested, duplicateRun!.State);
        Assert.Equal(1, backend.StartCalls);

        backend.StatusException = null;
        backend.CancelException = notFound;
        var retried = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var result = await retried.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, retried.StatusCode);
        Assert.Equal(RunState.Cancelled, result!.State);
        Assert.Equal(1, backend.CancelCalls);
        Assert.Equal(2, backend.StatusCalls);
        Assert.Equal(1, backend.StartCalls);
    }

    [Fact]
    public async Task UnknownStartCancellationReplayAcceptsReconciledTerminalRun()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic timeout.")
        };
        await using var factory = new ControlApiFactory(backend);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("cancel-terminal-reconcile", "synthetic-workload");

        var failed = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, failed.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var unknown = Assert.Single(list!.Items);
        backend.Runs[unknown.Id] =
            StubSemanticBackendClient.Status(unknown.Id, RunState.Succeeded);

        var cancelled = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var result = await cancelled.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        var replay = await client.PostAsJsonAsync(
            $"/api/v1/runs/{unknown.Id}/cancel",
            new CancelRunRequest(unknown.Version));
        var replayResult = await replay.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, cancelled.StatusCode);
        Assert.Equal(HttpStatusCode.Accepted, replay.StatusCode);
        Assert.Equal(RunState.Succeeded, result!.State);
        Assert.Equal(CancellationDeliveryState.Delivered, result.CancellationDelivery);
        Assert.Equal(result.Version, replayResult!.Version);
        Assert.Equal(1, backend.StatusCalls);
        Assert.Equal(0, backend.CancelCalls);
    }

    [Fact]
    public async Task FeedbackWaitsForPendingStartProjection()
    {
        var backend = new StubSemanticBackendClient
        {
            StartEntered = NewGate(),
            ReleaseStart = NewGate()
        };
        var coordinator = new ObservableRunDispatchCoordinator();
        await using var factory = new ControlApiFactory(
            backend,
            dispatchCoordinator: coordinator);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var createTask = client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("feedback-start-race", "synthetic-workload"));
        await backend.StartEntered.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var pending = Assert.Single(list!.Items);

        var feedbackTask = client.PostAsJsonAsync(
            $"/api/v1/runs/{pending.Id}/feedback",
            new SubmitFeedbackRequest(
                "feedback-race",
                5,
                FeedbackOutcome.Helpful,
                ["clear"],
                pending.Version),
            JsonOptions);
        await coordinator.WaitForAttemptAsync(2).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.False(feedbackTask.IsCompleted);

        backend.ReleaseStart.TrySetResult(true);
        var created = await createTask;
        var feedback = await feedbackTask;
        var current = await client.GetFromJsonAsync<RunMetadata>(
            $"/api/v1/runs/{pending.Id}",
            JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, created.StatusCode);
        Assert.Equal(HttpStatusCode.Conflict, feedback.StatusCode);
        Assert.Equal(RunState.Starting, current!.State);
        Assert.Equal(1, backend.StartCalls);
    }

    [Fact]
    public async Task CancellationBeforeInitialDispatchFinalizesLocally()
    {
        var backend = new StubSemanticBackendClient();
        var coordinator = new DelayedFirstRunDispatchCoordinator();
        await using var factory = new ControlApiFactory(
            backend,
            dispatchCoordinator: coordinator);
        using var client = factory.CreateAuthenticatedClient("contributor");

        var createTask = client.PostAsJsonAsync(
            "/api/v1/runs",
            new CreateRunRequest("cancel-before-start", "synthetic-workload"));
        await coordinator.FirstAttempted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var pending = Assert.Single(list!.Items);

        var cancel = await client.PostAsJsonAsync(
            $"/api/v1/runs/{pending.Id}/cancel",
            new CancelRunRequest(pending.Version),
            JsonOptions);
        var locallyCancelled = await cancel.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);
        coordinator.ReleaseFirst.TrySetResult(true);
        var create = await createTask;
        var createReplay = await create.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, cancel.StatusCode);
        Assert.Equal(HttpStatusCode.Accepted, create.StatusCode);
        Assert.Equal(RunState.Cancelled, locallyCancelled!.State);
        Assert.Equal(CancellationDeliveryState.Delivered, locallyCancelled.CancellationDelivery);
        Assert.Equal(RunState.Cancelled, createReplay!.State);
        Assert.Equal(0, backend.StartCalls);
    }

    [Fact]
    public async Task ConcurrentCreatedRequestReconcilesCurrentUnknownState()
    {
        var backend = new StubSemanticBackendClient
        {
            StartException = new SemanticBackendException(
                "semantic_backend_timeout",
                "Synthetic timeout.")
        };
        var coordinator = new DelayedFirstRunDispatchCoordinator();
        await using var factory = new ControlApiFactory(
            backend,
            dispatchCoordinator: coordinator);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var request = new CreateRunRequest("concurrent-create", "synthetic-workload");

        var first = client.PostAsJsonAsync("/api/v1/runs", request);
        await coordinator.FirstAttempted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        var second = await client.PostAsJsonAsync("/api/v1/runs", request);
        Assert.Equal(HttpStatusCode.GatewayTimeout, second.StatusCode);
        var list = await client.GetFromJsonAsync<RunListResponse>("/api/v1/runs", JsonOptions);
        var unknown = Assert.Single(list!.Items);
        Assert.Equal(RunState.DispatchUnknown, unknown.State);

        backend.StartException = null;
        backend.Runs[unknown.Id] =
            StubSemanticBackendClient.Status(unknown.Id, RunState.Running);
        coordinator.ReleaseFirst.TrySetResult(true);
        var reconciled = await first;
        var run = await reconciled.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, reconciled.StatusCode);
        Assert.Equal(RunState.Running, run!.State);
        Assert.Equal(1, backend.StartCalls);
        Assert.Equal(1, backend.StatusCalls);
        Assert.Equal(0, backend.CancelCalls);
    }

    [Fact]
    public async Task NonexistentRunsDoNotAllocateDispatchGates()
    {
        var coordinator = new ObservableRunDispatchCoordinator();
        await using var factory = new ControlApiFactory(
            dispatchCoordinator: coordinator);
        using var client = factory.CreateAuthenticatedClient("contributor");
        var missing = RunId.New();

        var cancel = await client.PostAsJsonAsync(
            $"/api/v1/runs/{missing}/cancel",
            new CancelRunRequest(null));
        var status = await client.GetAsync($"/api/v1/runs/{missing}/semantic-status");
        var feedback = await client.PostAsJsonAsync(
            $"/api/v1/runs/{missing}/feedback",
            new SubmitFeedbackRequest(
                "missing-feedback",
                5,
                FeedbackOutcome.Helpful,
                [],
                1),
            JsonOptions);

        Assert.Equal(HttpStatusCode.NotFound, cancel.StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, status.StatusCode);
        Assert.Equal(HttpStatusCode.NotFound, feedback.StatusCode);
        Assert.Equal(0, coordinator.AttemptCount);
    }

    private static JsonSerializerOptions CreateJsonOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web);
        JsonContractOptions.Configure(options);
        SemanticJsonContractOptions.Configure(options);
        return options;
    }

    private static CreateRunRequest ValidCreateRequest(string clientRequestId) =>
        new(
            clientRequestId,
            "synthetic-workload",
            "Compare synthetic regional revenue",
            new DateTimeOffset(2026, 8, 15, 9, 0, 0, TimeSpan.FromHours(8)),
            "Asia/Shanghai",
            CompilationMode.RegionalQuarterlyProfit,
            ExecutionMode.Thread,
            OutputMode.Normal);

    private static TaskCompletionSource<bool> NewGate() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    private static string Extension(ProblemDetails problem, string key)
    {
        Assert.True(problem.Extensions.TryGetValue(key, out var value));
        var element = Assert.IsType<JsonElement>(value);
        return Assert.IsType<string>(element.GetString());
    }
}
