using System.Globalization;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace ControlApi.Semantic;

internal static class CatalogResponseValidation
{
    private static readonly JsonSerializerOptions Json = new()
    {
        UnmappedMemberHandling = System.Text.Json.Serialization.JsonUnmappedMemberHandling.Disallow
    };

    private static void Require(bool condition)
    {
        if (!condition) throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
    }

    private static bool Id(string? value) =>
        value is not null && Regex.IsMatch(value, "^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\\z");

    private static bool Null(JsonElement element, string key) =>
        element.GetProperty(key).ValueKind == JsonValueKind.Null;

    internal static void Validate(
        CatalogQueryResponse result, CatalogPin expectedPin, string requestId,
        (string Identifier, int Revision)? continuation)
    {
        try
        {
            Require(result is not null && result.ContractVersion == "catalog-query-result/v1" &&
                Id(result.RequestId) && result.RequestId == requestId &&
                result.Provenance is not null && result.Provenance.Count <= 16 &&
                result.Provenance.All(pair => pair.Key.Length <= 128 && pair.Value is not null && pair.Value.Length <= 2048));
            var compilation = result!.Compilation;
            Require(compilation.ValueKind == JsonValueKind.Object &&
                compilation.GetProperty("contract_version").GetString() == "catalog-compilation/v1" &&
                compilation.GetProperty("catalog").Deserialize<CatalogPin>(Json) == expectedPin);
            var status = compilation.GetProperty("status").GetString();
            if (result.Status == "succeeded")
            {
                Require(status == "compiled" && result.RunId is not null && result.Result is not null);
                Require(Null(compilation, "clarification_id") && Null(compilation, "clarification_revision") &&
                    Null(compilation, "ambiguity") && Null(compilation, "expires_at"));
                var graph = compilation.GetProperty("graph");
                Require(graph.ValueKind == JsonValueKind.Object &&
                    graph.GetProperty("contract_version").GetString() == "sqg/v1" &&
                    graph.GetProperty("catalog").Deserialize<CatalogPin>(Json) == expectedPin);
                ValidateResultSchema(graph.GetProperty("result_schema"), result.Result!);
                SemanticRunDetailValidator.ValidateResult(result.Result!);
            }
            else if (result.Status == "clarification_required")
            {
                Require(status == "clarification" && result.RunId is null && result.Result is null &&
                    Null(compilation, "graph"));
                var identifier = compilation.GetProperty("clarification_id").GetString();
                var revision = compilation.GetProperty("clarification_revision").GetInt32();
                Require(Id(identifier) && revision >= 1);
                if (continuation is { } expected)
                    Require(identifier == expected.Identifier && revision == expected.Revision);
                var expiry = compilation.GetProperty("expires_at").GetString();
                Require(expiry is not null && Regex.IsMatch(expiry, @"(?:[Zz]|[+-]\d{2}:\d{2})\z") &&
                    DateTimeOffset.TryParse(expiry, CultureInfo.InvariantCulture, DateTimeStyles.None, out var parsed) &&
                    parsed > DateTimeOffset.UtcNow);
                var ambiguity = compilation.GetProperty("ambiguity");
                Require(ambiguity.ValueKind == JsonValueKind.Object &&
                    Id(ambiguity.GetProperty("id").GetString()) &&
                    !string.IsNullOrWhiteSpace(ambiguity.GetProperty("term").GetString()));
                var choices = ambiguity.GetProperty("choices");
                Require(choices.ValueKind == JsonValueKind.Array && choices.GetArrayLength() is >= 2 and <= 32);
                var ids = new HashSet<string>(StringComparer.Ordinal);
                foreach (var choice in choices.EnumerateArray())
                {
                    var id = choice.GetProperty("id").GetString();
                    var kind = choice.GetProperty("kind").GetString();
                    Require(Id(id) && ids.Add(id!) && Id(choice.GetProperty("target_id").GetString()) &&
                        !string.IsNullOrWhiteSpace(choice.GetProperty("label").GetString()) &&
                        kind is "entity" or "field" or "metric" or "member" or "time");
                    Require(kind == "member" ? Id(choice.GetProperty("field_id").GetString()) : Null(choice, "field_id"));
                }
            }
            else
            {
                Require(result.Status == "blocked" && status == "blocked" && result.Result is null &&
                    result.RunId is null && Null(compilation, "graph") &&
                    Null(compilation, "clarification_id") && Null(compilation, "clarification_revision") &&
                    Null(compilation, "ambiguity") && Null(compilation, "expires_at"));
            }
        }
        catch (Exception exception) when (exception is JsonException or KeyNotFoundException or
            InvalidOperationException or FormatException or OverflowException or SemanticBackendException)
        {
            throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE");
        }
    }

    private static void ValidateResultSchema(JsonElement schema, SemanticResultSet result)
    {
        Require(schema.ValueKind == JsonValueKind.Array && schema.GetArrayLength() is >= 1 and <= 64 &&
            result.Columns is not null && schema.GetArrayLength() == result.Columns.Count);
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var index = 0;
        foreach (var column in schema.EnumerateArray())
        {
            var name = column.GetProperty("name").GetString();
            Require(name is not null && Regex.IsMatch(name, "^[A-Za-z][A-Za-z0-9_]{0,63}\\z") &&
                seen.Add(name) && name == result.Columns![index].Key);
            var expected = column.GetProperty("data_type").GetString() switch
            {
                "string" => SemanticScalarType.String,
                "integer" => SemanticScalarType.Integer,
                "number" => SemanticScalarType.Float,
                "boolean" => SemanticScalarType.Boolean,
                "datetime" => SemanticScalarType.Timestamp,
                _ => throw new CatalogBackendException(502, "CATALOG_INVALID_RESPONSE")
            };
            Require(result.Columns![index].DataType == expected);
            index++;
        }
    }
}
