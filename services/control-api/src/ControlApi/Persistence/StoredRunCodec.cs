using System.Text.Json;
using System.Text.Json.Serialization.Metadata;
using ControlApi.Domain;
using ControlApi.Semantic;

namespace ControlApi.Persistence;

internal static class StoredRunCodec
{
    private static readonly JsonSerializerOptions Options = new()
    {
        IgnoreReadOnlyProperties = true,
        MaxDepth = 32,
        TypeInfoResolver = new DefaultJsonTypeInfoResolver
        {
            Modifiers =
            {
                typeInfo =>
                {
                    foreach (var property in typeInfo.Properties.Where(property => property.Set is not null))
                    {
                        property.IsRequired = true;
                    }
                }
            }
        }
    };

    public static string Encode<T>(T value) => JsonSerializer.Serialize(value, Options);

    public static RunMetadata Run(string json)
    {
        var run = Decode<RunMetadata>(json);
        if (!RunId.TryParse(run.Id.Value, null, out _) ||
            !Identifier(run.ClientRequestId) || !Identifier(run.Workload) ||
            string.IsNullOrWhiteSpace(run.CreatedBy) ||
            string.IsNullOrWhiteSpace(run.Question) || run.Question.Length > 8_000 ||
            run.EvaluationClock == default || string.IsNullOrWhiteSpace(run.EvaluationTimezone) ||
            run.EvaluationTimezone.Length > 100 ||
            !Enum.IsDefined(run.CompilationMode) || !Enum.IsDefined(run.ExecutionMode) ||
            !Enum.IsDefined(run.OutputMode) || !Enum.IsDefined(run.State) ||
            !Enum.IsDefined(run.CancellationDelivery) || run.Version < 1 ||
            run.CancellationGeneration < 0 || run.CreatedAt == default || run.UpdatedAt == default ||
            (run.CancellationGeneration == 0) != (run.CancellationDelivery == CancellationDeliveryState.NotRequested) ||
            (run.State == RunState.CancelRequested && run.CancellationGeneration == 0) ||
            (run.State.IsTerminal() && run.CancellationDelivery == CancellationDeliveryState.Pending))
        {
            throw new StorageCorruptionException();
        }
        // Validate BFF-only states through an equivalent backend timeline without relaxing child validation.
        var validationState = run.State is RunState.StartPending or RunState.DispatchUnknown or RunState.CancelRequested
            ? (run.StartedAt is null ? RunState.Queued : RunState.Running) : run.State;
        try
        {
            SemanticRunStatusValidator.Validate(new SemanticRunStatus(run.Id, validationState,
                run.StartedAt, run.CompletedAt, run.Stages, run.TokenUsage, run.Diagnostics), run.Id);
        }
        catch (SemanticBackendException)
        {
            throw new StorageCorruptionException();
        }
        return run;
    }

    public static RunFeedback Feedback(string json)
    {
        var feedback = Decode<RunFeedback>(json);
        if (!RunId.TryParse(feedback.RunId.Value, null, out _) ||
            string.IsNullOrWhiteSpace(feedback.SubmissionId) || feedback.SubmissionId.Length > 64 ||
            feedback.Rating is < 1 or > 5 ||
            !Enum.IsDefined(feedback.Outcome) || feedback.ReasonCodes is null ||
            feedback.ReasonCodes.Count > 10 || feedback.ReasonCodes.Any(code =>
                string.IsNullOrWhiteSpace(code) || code.Length > 32 ||
                !code.All(character => char.IsAsciiLetterOrDigit(character) || character is '-' or '_' or '.')) ||
            string.IsNullOrWhiteSpace(feedback.SubmittedBy) ||
            feedback.SubmittedAt == default || feedback.RunVersion < 2)
        {
            throw new StorageCorruptionException();
        }
        return feedback;
    }

    private static bool Identifier(string? value) =>
        !string.IsNullOrWhiteSpace(value) && value.Length <= 64 &&
        char.IsAsciiLetterOrDigit(value[0]) &&
        value.All(character => char.IsAsciiLetterOrDigit(character) || character is '-' or '_' or '.');

    private static T Decode<T>(string json)
    {
        try
        {
            return JsonSerializer.Deserialize<T>(json, Options) ?? throw new StorageCorruptionException();
        }
        catch (JsonException)
        {
            throw new StorageCorruptionException();
        }
    }
}

public sealed class StorageCorruptionException() : Exception("Persisted control-plane data is invalid.");
public sealed class StorageConfigurationException(string message) : Exception(message);
public sealed class DispatchRecoveryRequiredException()
    : Exception("A durable dispatch intent already exists. Backend recovery or operator reconciliation is required.");
public sealed class DurableDispatchUnavailableException()
    : Exception("The semantic backend is unavailable. Retry the same creation request to reconcile the existing run; do not use a new request ID.");
