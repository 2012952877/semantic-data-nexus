using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;
using ControlApi.Domain;

namespace ControlApi.Semantic;

#pragma warning disable CA1720 // Contract enum names intentionally mirror cross-language scalar types.

public enum SemanticOperatorKind
{
    Source,
    Select,
    Filter,
    Aggregate,
    Pivot,
    Derive,
    Project,
    Sort,
    Limit,
    Join
}

public enum SemanticScalarType
{
    String,
    Integer,
    Float,
    Decimal,
    Boolean,
    Date,
    Timestamp
}

public enum SemanticColumnFormat
{
    Text,
    Currency,
    Percent,
    Number,
    Date,
    Timestamp
}

public enum SemanticResultStorage
{
    Inline,
    Parquet
}

public enum SemanticLineageNodeKind
{
    Logical,
    Physical,
    Source,
    Result
}

public enum SemanticLineageRelation
{
    RealizedAs,
    ReadsFrom,
    DependsOn,
    Produces
}

public enum SemanticDiagnosticSeverity
{
    Info,
    Warning,
    Error
}

public enum SemanticDiagnosticScope
{
    Run,
    Stage,
    Node
}

public sealed record SemanticSqgFilter(string Field, string Operator, string Value);

public sealed record SemanticSqgSummary(
    string Version,
    string Intent,
    string Ontology,
    IReadOnlyList<string> ResolvedMembers,
    IReadOnlyList<string> Metrics,
    IReadOnlyList<string> Dimensions,
    IReadOnlyList<SemanticSqgFilter> Filters,
    IReadOnlyList<string> PolicyChecks);

public sealed record SemanticPhysicalNode(
    string Id,
    SemanticOperatorKind Kind,
    string Label,
    string PlainLanguage,
    IReadOnlyList<string> Inputs,
    IReadOnlyList<string> OutputFields);

public sealed record SemanticResultColumn(
    string Key,
    string Label,
    SemanticScalarType DataType,
    SemanticColumnFormat Format,
    bool Nullable);

public sealed record SemanticResultSet(
    IReadOnlyList<SemanticResultColumn> Columns,
    IReadOnlyList<IReadOnlyList<SemanticScalarValue>> Rows,
    long RowCount,
    bool Truncated);

public sealed record SemanticCommittedManifest(
    string ResultId,
    RunId RunId,
    string NodeId,
    SemanticResultStorage Storage,
    string Uri,
    long RowCount,
    long ByteCount,
    string Checksum,
    DateTimeOffset CommittedAt);

public sealed record SemanticLineageParameter(string Name, SemanticScalarType DataType);

public sealed record SemanticLineageNode(
    string Id,
    SemanticLineageNodeKind Kind,
    string? Operation,
    string? SourceAlias,
    string? SourceType,
    string? ResultId,
    IReadOnlyList<SemanticLineageParameter> Parameters);

public sealed record SemanticLineageEdge(
    string Source,
    string Target,
    SemanticLineageRelation Relation);

public sealed record SemanticLineage(
    string Version,
    RunId RunId,
    IReadOnlyList<SemanticLineageNode> Nodes,
    IReadOnlyList<SemanticLineageEdge> Edges);

public sealed record SemanticDetailDiagnostic(
    long Sequence,
    RunId RunId,
    SemanticDiagnosticScope Scope,
    string ScopeId,
    string Code,
    string Title,
    string Message,
    string Recovery,
    SemanticDiagnosticSeverity Severity,
    DateTimeOffset OccurredAt);

public sealed record SemanticRunDetail(
    RunId RunId,
    string Question,
    SemanticSqgSummary Sqg,
    IReadOnlyList<SemanticPhysicalNode> PhysicalNodes,
    SemanticResultSet? Result,
    SemanticCommittedManifest? Manifest,
    SemanticLineage Lineage,
    IReadOnlyList<SemanticDetailDiagnostic> Diagnostics);

public static class SemanticJsonContractOptions
{
    public static void Configure(JsonSerializerOptions options)
    {
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticOperatorKind>(
            JsonNamingPolicy.SnakeCaseUpper,
            allowIntegerValues: false));
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticScalarType>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticColumnFormat>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticResultStorage>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticLineageNodeKind>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticLineageRelation>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticDiagnosticSeverity>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
        options.Converters.Insert(0, new JsonStringEnumConverter<SemanticDiagnosticScope>(
            JsonNamingPolicy.SnakeCaseLower,
            allowIntegerValues: false));
    }
}

public enum SemanticScalarKind
{
    Null,
    String,
    Integer,
    Number,
    Boolean
}

[JsonConverter(typeof(SemanticScalarValueJsonConverter))]
public sealed record SemanticScalarValue
{
    private SemanticScalarValue(
        SemanticScalarKind kind,
        string? stringValue = null,
        long integerValue = 0,
        decimal numberValue = 0,
        bool booleanValue = false)
    {
        Kind = kind;
        StringValue = stringValue;
        IntegerValue = integerValue;
        NumberValue = numberValue;
        BooleanValue = booleanValue;
    }

    public SemanticScalarKind Kind { get; }
    public string? StringValue { get; }
    public long IntegerValue { get; }
    public decimal NumberValue { get; }
    public bool BooleanValue { get; }

    public static SemanticScalarValue Null { get; } = new(SemanticScalarKind.Null);
    public static SemanticScalarValue From(string value) => new(SemanticScalarKind.String, value);
    public static SemanticScalarValue From(long value) =>
        new(SemanticScalarKind.Integer, integerValue: value);
    public static SemanticScalarValue From(decimal value) =>
        new(SemanticScalarKind.Number, numberValue: value);
    public static SemanticScalarValue From(bool value) =>
        new(SemanticScalarKind.Boolean, booleanValue: value);
}

public sealed class SemanticScalarValueJsonConverter : JsonConverter<SemanticScalarValue>
{
    public override bool HandleNull => true;

    public override SemanticScalarValue Read(
        ref Utf8JsonReader reader,
        Type typeToConvert,
        JsonSerializerOptions options) =>
        reader.TokenType switch
        {
            JsonTokenType.Null => SemanticScalarValue.Null,
            JsonTokenType.String => SemanticScalarValue.From(
                reader.GetString() ??
                throw new JsonException("Scalar strings cannot be null.")),
            JsonTokenType.Number when reader.TryGetInt64(out var integer) =>
                SemanticScalarValue.From(integer),
            JsonTokenType.Number when reader.TryGetDecimal(out var number) =>
                SemanticScalarValue.From(number),
            JsonTokenType.True => SemanticScalarValue.From(true),
            JsonTokenType.False => SemanticScalarValue.From(false),
            _ => throw new JsonException("Result cells must be JSON scalar values.")
        };

    public override void Write(
        Utf8JsonWriter writer,
        SemanticScalarValue value,
        JsonSerializerOptions options)
    {
        switch (value.Kind)
        {
            case SemanticScalarKind.Null:
                writer.WriteNullValue();
                break;
            case SemanticScalarKind.String:
                writer.WriteStringValue(value.StringValue);
                break;
            case SemanticScalarKind.Integer:
                writer.WriteNumberValue(value.IntegerValue);
                break;
            case SemanticScalarKind.Number:
                writer.WriteNumberValue(value.NumberValue);
                break;
            case SemanticScalarKind.Boolean:
                writer.WriteBooleanValue(value.BooleanValue);
                break;
            default:
                throw new JsonException("Unsupported scalar value.");
        }
    }
}

public static class SemanticRunDetailValidator
{
    private const int MaximumQuestionLength = 4_000;
    private const int MaximumSummaryItems = 100;
    private const int MaximumPhysicalNodes = 1_000;
    private const int MaximumNodeItems = 100;
    private const int MaximumColumns = 100;
    private const int MaximumRows = 1_000;
    private const int MaximumScalarStringLength = 4_000;
    private const int MaximumLineageNodes = 5_000;
    private const int MaximumLineageEdges = 10_000;
    private const int MaximumDiagnostics = 1_000;

    public static void Validate(SemanticRunDetail detail, RunId expectedRunId)
    {
        if (detail.RunId != expectedRunId)
        {
            throw Invalid("The semantic backend returned a different run ID.");
        }

        if (!ValidText(detail.Question, MaximumQuestionLength))
        {
            throw Invalid("The semantic backend returned an invalid question.");
        }

        ValidateSqg(detail.Sqg);
        ValidatePhysicalNodes(detail.PhysicalNodes);
        if (detail.Result is not null)
        {
            ValidateResult(detail.Result);
        }

        if (detail.Manifest is not null)
        {
            ValidateManifest(detail.Manifest, expectedRunId);
        }

        ValidateLineage(detail.Lineage, expectedRunId);
        ValidateDiagnostics(detail.Diagnostics, expectedRunId);
    }

    public static SemanticBackendException Invalid(string message) =>
        new(
            "semantic_backend_invalid_response",
            message,
            failureKind: SemanticFailureKind.InvalidResponse);

    private static void ValidateSqg(SemanticSqgSummary? sqg)
    {
        if (sqg is null ||
            !ValidLabel(sqg.Version) ||
            !ValidText(sqg.Intent, 512) ||
            !ValidLabel(sqg.Ontology) ||
            !ValidLabels(sqg.ResolvedMembers, MaximumSummaryItems) ||
            !ValidLabels(sqg.Metrics, MaximumSummaryItems) ||
            !ValidLabels(sqg.Dimensions, MaximumSummaryItems) ||
            !ValidLabels(sqg.PolicyChecks, MaximumSummaryItems) ||
            sqg.Filters is null ||
            sqg.Filters.Count > MaximumSummaryItems ||
            sqg.Filters.Any(filter =>
                filter is null ||
                !ValidLabel(filter.Field) ||
                !ValidLabel(filter.Operator) ||
                !ValidText(filter.Value, 512)))
        {
            throw Invalid("The semantic backend returned an invalid SQG summary.");
        }
    }

    #pragma warning restore CA1720

    private static void ValidatePhysicalNodes(IReadOnlyList<SemanticPhysicalNode>? nodes)
    {
        if (nodes is null || nodes.Count > MaximumPhysicalNodes)
        {
            throw Invalid("The semantic backend returned an invalid physical node collection.");
        }

        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var node in nodes)
        {
            if (node is null ||
                !ValidLabel(node.Id) ||
                !ids.Add(node.Id) ||
                !Enum.IsDefined(node.Kind) ||
                !ValidText(node.Label, 256) ||
                !ValidText(node.PlainLanguage, 1_000) ||
                !ValidLabels(node.Inputs, MaximumNodeItems) ||
                !ValidLabels(node.OutputFields, MaximumNodeItems))
            {
                throw Invalid("The semantic backend returned an invalid physical node.");
            }
        }
    }

    private static void ValidateResult(SemanticResultSet result)
    {
        if (result.Columns is null ||
            result.Columns.Count > MaximumColumns ||
            result.Rows is null ||
            result.Rows.Count > MaximumRows ||
            result.RowCount < result.Rows.Count ||
            result.RowCount > int.MaxValue ||
            result.Truncated != (result.RowCount > result.Rows.Count))
        {
            throw Invalid("The semantic backend returned an invalid bounded result.");
        }

        var keys = new HashSet<string>(StringComparer.Ordinal);
        foreach (var column in result.Columns)
        {
            if (column is null ||
                !ValidLabel(column.Key) ||
                !keys.Add(column.Key) ||
                !ValidText(column.Label, 256) ||
                !Enum.IsDefined(column.DataType) ||
                !Enum.IsDefined(column.Format))
            {
                throw Invalid("The semantic backend returned an invalid result column.");
            }
        }

        foreach (var row in result.Rows)
        {
            if (row is null || row.Count != result.Columns.Count)
            {
                throw Invalid("The semantic backend returned an invalid result row.");
            }

            for (var index = 0; index < row.Count; index++)
            {
                ValidateCell(row[index], result.Columns[index]);
            }
        }
    }

    private static void ValidateCell(
        SemanticScalarValue? value,
        SemanticResultColumn column)
    {
        if (value is null || (value.Kind == SemanticScalarKind.Null && !column.Nullable))
        {
            throw Invalid("The semantic backend returned an invalid null result cell.");
        }

        var valid = column.DataType switch
        {
            SemanticScalarType.String =>
                value.Kind == SemanticScalarKind.String &&
                value.StringValue!.Length <= MaximumScalarStringLength,
            SemanticScalarType.Integer => value.Kind == SemanticScalarKind.Integer,
            SemanticScalarType.Float or SemanticScalarType.Decimal =>
                value.Kind is SemanticScalarKind.Integer or SemanticScalarKind.Number,
            SemanticScalarType.Boolean => value.Kind == SemanticScalarKind.Boolean,
            SemanticScalarType.Date =>
                value.Kind == SemanticScalarKind.String &&
                DateOnly.TryParseExact(
                    value.StringValue,
                    "yyyy-MM-dd",
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.None,
                    out _),
            SemanticScalarType.Timestamp =>
                value.Kind == SemanticScalarKind.String &&
                HasExplicitOffset(value.StringValue!) &&
                DateTimeOffset.TryParse(
                    value.StringValue,
                    CultureInfo.InvariantCulture,
                    DateTimeStyles.RoundtripKind,
                    out _),
            _ => false
        };

        if (!valid && value.Kind != SemanticScalarKind.Null)
        {
            throw Invalid("The semantic backend returned a result cell with the wrong scalar type.");
        }
    }

    private static void ValidateManifest(SemanticCommittedManifest manifest, RunId expectedRunId)
    {
        if (manifest.RunId != expectedRunId ||
            !ValidLabel(manifest.ResultId) ||
            !ValidLabel(manifest.NodeId) ||
            !Enum.IsDefined(manifest.Storage) ||
            !ValidText(manifest.Uri, 2_048) ||
            manifest.RowCount < 0 ||
            manifest.ByteCount < 0 ||
            !ValidText(manifest.Checksum, 256) ||
            manifest.CommittedAt == default)
        {
            throw Invalid("The semantic backend returned an invalid committed manifest.");
        }
    }

    private static void ValidateLineage(SemanticLineage? lineage, RunId expectedRunId)
    {
        if (lineage is null ||
            lineage.RunId != expectedRunId ||
            !ValidLabel(lineage.Version) ||
            lineage.Nodes is null ||
            lineage.Nodes.Count > MaximumLineageNodes ||
            lineage.Edges is null ||
            lineage.Edges.Count > MaximumLineageEdges)
        {
            throw Invalid("The semantic backend returned invalid lineage.");
        }

        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var node in lineage.Nodes)
        {
            if (node is null ||
                !ValidLabel(node.Id) ||
                !ids.Add(node.Id) ||
                !Enum.IsDefined(node.Kind) ||
                !ValidOptionalLabel(node.Operation) ||
                !ValidOptionalLabel(node.SourceAlias) ||
                !ValidOptionalLabel(node.SourceType) ||
                !ValidOptionalLabel(node.ResultId) ||
                node.Parameters is null ||
                node.Parameters.Count > MaximumNodeItems ||
                node.Parameters.Any(parameter =>
                    parameter is null ||
                    !ValidLabel(parameter.Name) ||
                    !Enum.IsDefined(parameter.DataType)))
            {
                throw Invalid("The semantic backend returned an invalid lineage node.");
            }
        }

        foreach (var edge in lineage.Edges)
        {
            if (edge is null ||
                !ids.Contains(edge.Source) ||
                !ids.Contains(edge.Target) ||
                !Enum.IsDefined(edge.Relation))
            {
                throw Invalid("The semantic backend returned an invalid lineage edge.");
            }
        }
    }

    private static void ValidateDiagnostics(
        IReadOnlyList<SemanticDetailDiagnostic>? diagnostics,
        RunId expectedRunId)
    {
        if (diagnostics is null || diagnostics.Count > MaximumDiagnostics)
        {
            throw Invalid("The semantic backend returned invalid diagnostics.");
        }

        long previousSequence = -1;
        foreach (var diagnostic in diagnostics)
        {
            if (diagnostic is null ||
                diagnostic.RunId != expectedRunId ||
                diagnostic.Sequence <= previousSequence ||
                !Enum.IsDefined(diagnostic.Scope) ||
                !ValidLabel(diagnostic.ScopeId) ||
                !ValidLabel(diagnostic.Code) ||
                !ValidText(diagnostic.Title, 256) ||
                !ValidText(diagnostic.Message, 1_000) ||
                !ValidText(diagnostic.Recovery, 1_000) ||
                !Enum.IsDefined(diagnostic.Severity) ||
                diagnostic.OccurredAt == default)
            {
                throw Invalid("The semantic backend returned an invalid diagnostic.");
            }

            previousSequence = diagnostic.Sequence;
        }
    }

    private static bool ValidLabels(IReadOnlyList<string>? values, int maximum) =>
        values is not null &&
        values.Count <= maximum &&
        values.All(ValidLabel);

    private static bool ValidLabel(string? value) => ValidText(value, 128);

    private static bool ValidOptionalLabel(string? value) =>
        value is null || ValidLabel(value);

    private static bool ValidText(string? value, int maximumLength) =>
        !string.IsNullOrWhiteSpace(value) &&
        value.Length <= maximumLength &&
        !value.Any(character => char.IsControl(character));

    private static bool HasExplicitOffset(string value)
    {
        var timeSeparator = value.IndexOf('T', StringComparison.Ordinal);
        return timeSeparator >= 0 &&
            (value.EndsWith('Z') ||
             value.LastIndexOf('+') > timeSeparator ||
             value.LastIndexOf('-') > timeSeparator);
    }
}
