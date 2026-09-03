using ControlApi.Domain;

namespace ControlApi.Contracts;

public sealed record CreateRunRequest(string ClientRequestId, string Workload);

public sealed record CancelRunRequest(long? ExpectedVersion);

public sealed record SubmitFeedbackRequest(
    string SubmissionId,
    int Rating,
    FeedbackOutcome Outcome,
    IReadOnlyList<string> ReasonCodes,
    long ExpectedRunVersion);

public sealed record CurrentPrincipalResponse(
    string Subject,
    string? DisplayName,
    IReadOnlyList<string> Roles,
    IReadOnlyList<string> Scopes);

public sealed record RunListResponse(IReadOnlyList<RunMetadata> Items, int Count);
