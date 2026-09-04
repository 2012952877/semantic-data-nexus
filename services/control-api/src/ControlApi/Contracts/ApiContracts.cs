using System.ComponentModel.DataAnnotations;
using System.Text.Json;
using System.Text.Json.Serialization;
using ControlApi.Domain;

namespace ControlApi.Contracts;

public enum CompilationMode
{
    RegionalQuarterlyProfit = 1,
    MonthlyRegionalComparison = 2
}

public enum ExecutionMode
{
    Thread = 1
}

public enum OutputMode
{
    Normal = 1,
    Stream = 2
}

public sealed record CreateRunRequest(
    [property: Required] string ClientRequestId,
    [property: Required] string Workload,
    [property: Required] string Question,
    [property: JsonConverter(typeof(OffsetDateTimeJsonConverter))]
    [property: Required] DateTimeOffset EvaluationClock,
    [property: Required] string EvaluationTimezone,
    [property: Required] CompilationMode CompilationMode,
    [property: Required] ExecutionMode ExecutionMode,
    [property: Required] OutputMode OutputMode)
{
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
}

public sealed record CancelRunRequest(long? ExpectedVersion);

public sealed record SubmitFeedbackRequest(
    string SubmissionId,
    int Rating,
    [property: Required] FeedbackOutcome Outcome,
    IReadOnlyList<string> ReasonCodes,
    long ExpectedRunVersion);

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

        var value = reader.GetString();
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
