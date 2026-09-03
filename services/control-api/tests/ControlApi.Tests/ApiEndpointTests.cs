using System.Net;
using System.Net.Http.Json;
using System.Text.Json;
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
        Assert.Equal(2, backend.CancelCalls);

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

    private static JsonSerializerOptions CreateJsonOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web);
        options.Converters.Add(new JsonStringEnumConverter(allowIntegerValues: false));
        return options;
    }

    private static string Extension(ProblemDetails problem, string key)
    {
        Assert.True(problem.Extensions.TryGetValue(key, out var value));
        var element = Assert.IsType<JsonElement>(value);
        return Assert.IsType<string>(element.GetString());
    }
}
