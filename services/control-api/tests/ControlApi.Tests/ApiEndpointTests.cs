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
        Assert.Equal(0, backend.StatusCalls);

        backend.ReleaseCancel.TrySetResult(true);
        var cancelled = await cancelTask;
        var duplicate = await duplicateTask;
        var replay = await duplicate.Content.ReadFromJsonAsync<RunMetadata>(JsonOptions);

        Assert.Equal(HttpStatusCode.Accepted, cancelled.StatusCode);
        Assert.Equal(HttpStatusCode.OK, duplicate.StatusCode);
        Assert.Equal(RunState.CancelRequested, replay!.State);
        Assert.Equal(CancellationDeliveryState.Delivered, replay.CancellationDelivery);
        Assert.Equal(1, backend.StartCalls);
        Assert.Equal(0, backend.StatusCalls);
    }

    private static JsonSerializerOptions CreateJsonOptions()
    {
        var options = new JsonSerializerOptions(JsonSerializerDefaults.Web);
        options.Converters.Add(new JsonStringEnumConverter(allowIntegerValues: false));
        return options;
    }

    private static TaskCompletionSource<bool> NewGate() =>
        new(TaskCreationOptions.RunContinuationsAsynchronously);

    private static string Extension(ProblemDetails problem, string key)
    {
        Assert.True(problem.Extensions.TryGetValue(key, out var value));
        var element = Assert.IsType<JsonElement>(value);
        return Assert.IsType<string>(element.GetString());
    }
}
