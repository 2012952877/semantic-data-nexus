using System.ComponentModel.DataAnnotations;
using System.Text.Json;
using System.Text.Json.Serialization;
using ControlApi.Domain;

namespace ControlApi.Contracts;

[JsonConverter(typeof(JsonContractOptions.SnakeCaseCompilationModeJsonConverter))]
public enum CompilationMode
{
    RegionalQuarterlyProfit = 1,
    MonthlyRegionalComparison = 2
}

[JsonConverter(typeof(JsonContractOptions.SnakeCaseExecutionModeJsonConverter))]
public enum ExecutionMode
{
    Thread = 1
}

[JsonConverter(typeof(JsonContractOptions.SnakeCaseOutputModeJsonConverter))]
public enum OutputMode
{
    Normal = 1,
    Stream = 2
}

public sealed record CreateRunRequest
{
    [JsonConstructor]
    public CreateRunRequest(
        string clientRequestId,
        string workload,
        string question,
        DateTimeOffset evaluationClock,
        string evaluationTimezone,
        CompilationMode compilationMode,
        ExecutionMode executionMode,
        OutputMode outputMode)
    {
        ClientRequestId = clientRequestId;
        Workload = workload;
        Question = question;
        EvaluationClock = evaluationClock;
        EvaluationTimezone = evaluationTimezone;
        CompilationMode = compilationMode;
        ExecutionMode = executionMode;
        OutputMode = outputMode;
    }

    public CreateRunRequest(string clientRequestId, string workload)
        : this(
            clientRequestId,
            workload,
            "Synthetic governed question",
            DateTimeOffset.UnixEpoch,
            "Etc/UTC",
            CompilationMode.RegionalQuarterlyProfit,
            ExecutionMode.Thread,
            OutputMode.Normal)
    {
    }

    [Required]
    [JsonRequired]
    public string ClientRequestId { get; init; }

    [Required]
    [JsonRequired]
    public string Workload { get; init; }

    [Required]
    [JsonRequired]
    public string Question { get; init; }

    [Required]
    [JsonRequired]
    [JsonConverter(typeof(OffsetDateTimeJsonConverter))]
    public DateTimeOffset EvaluationClock { get; init; }

    [Required]
    [JsonRequired]
    public string EvaluationTimezone { get; init; }

    [Required]
    [JsonRequired]
    public CompilationMode CompilationMode { get; init; }

    [Required]
    [JsonRequired]
    public ExecutionMode ExecutionMode { get; init; }

    [Required]
    [JsonRequired]
    public OutputMode OutputMode { get; init; }
}

public sealed record CancelRunRequest(long? ExpectedVersion);

public sealed record SubmitFeedbackRequest(
    [property: JsonRequired] string SubmissionId,
    [property: JsonRequired] int Rating,
    [property: Required, JsonRequired] FeedbackOutcome Outcome,
    [property: JsonRequired] IReadOnlyList<string> ReasonCodes,
    [property: JsonRequired] long ExpectedRunVersion);

public sealed record CurrentPrincipalResponse(
    string Subject,
    string? DisplayName,
    IReadOnlyList<string> Roles,
    IReadOnlyList<string> Scopes);

public sealed record RunListResponse(IReadOnlyList<RunMetadata> Items, int Count);

public static class JsonContractOptions
{
    public static void Configure(JsonSerializerOptions options)
    {
        options.Converters.Add(new StrictStringJsonConverter());
        options.Converters.Add(new JsonStringEnumConverter<CompilationMode>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
        options.Converters.Add(new JsonStringEnumConverter<ExecutionMode>(
            JsonNamingPolicy.CamelCase,
            allowIntegerValues: false));
        options.Converters.Add(new JsonStringEnumConverter<OutputMode>(
            JsonNamingPolicy.CamelCase,
            allowIntegerValues: false));
        options.Converters.Add(new JsonStringEnumConverter(allowIntegerValues: false));
    }

    public sealed class StrictStringJsonConverter : JsonConverter<string>
    {
        public override string? Read(
            ref Utf8JsonReader reader,
            Type typeToConvert,
            JsonSerializerOptions options)
        {
            try
            {
                return reader.GetString();
            }
            catch (InvalidOperationException exception)
            {
                throw new JsonException(
                    "JSON strings must contain valid Unicode scalar values.",
                    exception);
            }
        }

        public override void Write(
            Utf8JsonWriter writer,
            string value,
            JsonSerializerOptions options) =>
            writer.WriteStringValue(value);
    }

    public sealed class SnakeCaseCompilationModeJsonConverter()
        : JsonStringEnumConverter<CompilationMode>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false);

    public sealed class SnakeCaseExecutionModeJsonConverter()
        : JsonStringEnumConverter<ExecutionMode>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false);

    public sealed class SnakeCaseOutputModeJsonConverter()
        : JsonStringEnumConverter<OutputMode>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false);
}

public sealed class OffsetDateTimeJsonConverter : JsonConverter<DateTimeOffset>
{
    public override DateTimeOffset Read(
        ref Utf8JsonReader reader,
        Type typeToConvert,
        JsonSerializerOptions options)
    {
        if (reader.TokenType != JsonTokenType.String)
        {
            throw new JsonException("Evaluation clocks must be JSON strings.");
        }

        string? value;
        try
        {
            value = reader.GetString();
        }
        catch (InvalidOperationException exception)
        {
            throw new JsonException(
                "Evaluation clocks must contain valid Unicode scalar values.",
                exception);
        }
        var timeSeparator = value?.IndexOf('T', StringComparison.Ordinal) ?? -1;
        if (value is null ||
            timeSeparator < 0 ||
            (!value.EndsWith('Z') &&
             value.LastIndexOf('+') <= timeSeparator &&
             value.LastIndexOf('-') <= timeSeparator) ||
            !DateTimeOffset.TryParse(
                value,
                System.Globalization.CultureInfo.InvariantCulture,
                System.Globalization.DateTimeStyles.RoundtripKind,
                out var result))
        {
            throw new JsonException("Evaluation clocks must include an explicit UTC offset.");
        }

        return result;
    }

    public override void Write(
        Utf8JsonWriter writer,
        DateTimeOffset value,
        JsonSerializerOptions options) =>
        writer.WriteStringValue(value);
}
