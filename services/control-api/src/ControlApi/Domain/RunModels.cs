using System.Diagnostics.CodeAnalysis;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace ControlApi.Domain;

[JsonConverter(typeof(RunIdJsonConverter))]
public readonly record struct RunId(string Value) : IParsable<RunId>
{
    public static RunId New() => new($"run_{Guid.NewGuid():N}");

    public static RunId Parse(string s, IFormatProvider? provider) =>
        TryParse(s, provider, out var result)
            ? result
            : throw new FormatException("Run IDs must use the canonical run_<32 lowercase hex characters> format.");

    public static bool TryParse([NotNullWhen(true)] string? s, IFormatProvider? provider, out RunId result)
    {
        if (s is { Length: 36 } &&
            s.StartsWith("run_", StringComparison.Ordinal) &&
            IsLowerHex(s.AsSpan(4)))
        {
            result = new RunId(s);
            return true;
        }

        result = default;
        return false;
    }

    public override string ToString() => Value;

    private static bool IsLowerHex(ReadOnlySpan<char> value)
    {
        foreach (var character in value)
        {
            if (!char.IsAsciiHexDigit(character) || char.IsAsciiLetterUpper(character))
            {
                return false;
            }
        }

        return true;
    }
}

public sealed class RunIdJsonConverter : JsonConverter<RunId>
{
    public override RunId Read(
        ref Utf8JsonReader reader,
        Type typeToConvert,
        JsonSerializerOptions options)
    {
        var value = reader.GetString();
        return RunId.TryParse(value, null, out var runId)
            ? runId
            : throw new JsonException("Invalid run ID.");
    }

    public override void Write(
        Utf8JsonWriter writer,
        RunId value,
        JsonSerializerOptions options) =>
        writer.WriteStringValue(value.Value);
}

public enum RunState
{
    StartPending,
    DispatchUnknown,
    Queued,
    Starting,
    Running,
    CancelRequested,
    Cancelled,
    Succeeded,
    Failed
}

public static class RunStateMachine
{
    private static readonly Dictionary<RunState, RunState[]> AllowedTransitions =
        new Dictionary<RunState, RunState[]>
        {
            [RunState.StartPending] =
                [RunState.DispatchUnknown, RunState.Queued, RunState.Starting, RunState.Running,
                    RunState.CancelRequested, RunState.Succeeded, RunState.Failed],
            [RunState.DispatchUnknown] =
                [RunState.Queued, RunState.Starting, RunState.Running, RunState.CancelRequested,
                    RunState.Succeeded, RunState.Failed],
            [RunState.Queued] =
                [RunState.Starting, RunState.Running, RunState.CancelRequested, RunState.Succeeded, RunState.Failed],
            [RunState.Starting] =
                [RunState.Running, RunState.CancelRequested, RunState.Succeeded, RunState.Failed],
            [RunState.Running] = [RunState.CancelRequested, RunState.Succeeded, RunState.Failed],
            [RunState.CancelRequested] = [RunState.Cancelled, RunState.Succeeded, RunState.Failed],
            [RunState.Cancelled] = [],
            [RunState.Succeeded] = [],
            [RunState.Failed] = []
        };

    public static bool IsTerminal(this RunState state) =>
        state is RunState.Cancelled or RunState.Succeeded or RunState.Failed;

    public static bool RequiresStartReconciliation(this RunState state) =>
        state is RunState.StartPending or RunState.DispatchUnknown;

    public static bool CanTransitionTo(this RunState current, RunState next) =>
        current == next || AllowedTransitions[current].Contains(next);
}

public enum CancellationDeliveryState
{
    NotRequested,
    Pending,
    Delivered
}

public sealed record TokenUsage(long InputTokens, long OutputTokens)
{
    public long TotalTokens => checked(InputTokens + OutputTokens);
}

public sealed record DiagnosticSummary(
    string Code,
    string Message,
    string? Stage,
    DateTimeOffset OccurredAt);

public sealed record NodeSummary(
    string NodeId,
    string Kind,
    RunState State,
    DateTimeOffset? StartedAt,
    DateTimeOffset? CompletedAt)
{
    public TimeSpan? Duration =>
        StartedAt is null || CompletedAt is null ? null : CompletedAt - StartedAt;
}

public sealed record StageSummary(
    string StageId,
    string Name,
    RunState State,
    DateTimeOffset? StartedAt,
    DateTimeOffset? CompletedAt,
    IReadOnlyList<NodeSummary> Nodes)
{
    public TimeSpan? Duration =>
        StartedAt is null || CompletedAt is null ? null : CompletedAt - StartedAt;
}

public sealed record RunMetadata(
    RunId Id,
    string ClientRequestId,
    string Workload,
    string CreatedBy,
    RunState State,
    CancellationDeliveryState CancellationDelivery,
    long CancellationGeneration,
    DateTimeOffset CreatedAt,
    DateTimeOffset UpdatedAt,
    DateTimeOffset? StartedAt,
    DateTimeOffset? CompletedAt,
    long Version,
    IReadOnlyList<StageSummary> Stages,
    TokenUsage TokenUsage,
    IReadOnlyList<DiagnosticSummary> Diagnostics)
{
    public TimeSpan? Duration =>
        State.IsTerminal() && StartedAt is not null && CompletedAt is not null
            ? CompletedAt - StartedAt
            : null;
}

public enum FeedbackOutcome
{
    Helpful,
    PartiallyHelpful,
    NotHelpful
}

public sealed record RunFeedback(
    string SubmissionId,
    RunId RunId,
    int Rating,
    FeedbackOutcome Outcome,
    IReadOnlyList<string> ReasonCodes,
    string SubmittedBy,
    DateTimeOffset SubmittedAt,
    long RunVersion);

public sealed record RunStatistics(
    long TotalRuns,
    IReadOnlyDictionary<RunState, long> RunsByState,
    double? AverageDurationMilliseconds,
    long TotalInputTokens,
    long TotalOutputTokens,
    long FeedbackCount);
