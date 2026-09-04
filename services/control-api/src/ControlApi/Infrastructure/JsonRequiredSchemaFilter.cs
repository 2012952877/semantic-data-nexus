using System.Reflection;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.OpenApi.Models;
using Swashbuckle.AspNetCore.SwaggerGen;

namespace ControlApi.Infrastructure;

public sealed class JsonRequiredSchemaFilter : ISchemaFilter
{
    private static readonly NullabilityInfoContext NullabilityContext = new();

    public void Apply(OpenApiSchema schema, SchemaFilterContext context)
    {
        foreach (var property in context.Type.GetProperties(BindingFlags.Instance | BindingFlags.Public))
        {
            var propertyName =
                property.GetCustomAttribute<JsonPropertyNameAttribute>()?.Name ??
                JsonNamingPolicy.CamelCase.ConvertName(property.Name);
            if (!schema.Properties.TryGetValue(propertyName, out var propertySchema))
            {
                continue;
            }

            if (property.GetCustomAttribute<JsonRequiredAttribute>() is not null)
            {
                schema.Required.Add(propertyName);
            }

            propertySchema.Nullable = IsNullable(property);
        }
    }

    private static bool IsNullable(PropertyInfo property) =>
        NullabilityContext.Create(property).ReadState == NullabilityState.Nullable ||
        System.Nullable.GetUnderlyingType(property.PropertyType) is not null;
}
