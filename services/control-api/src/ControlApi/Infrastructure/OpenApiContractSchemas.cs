using System.Text.Json;
using Microsoft.OpenApi.Any;
using Microsoft.OpenApi.Models;

namespace ControlApi.Infrastructure;

public static class OpenApiContractSchemas
{
    public static OpenApiSchema StringEnum<T>()
        where T : struct, Enum =>
        CreateStringEnum<T>(name => name);

    public static OpenApiSchema StringEnum<T>(JsonNamingPolicy namingPolicy)
        where T : struct, Enum =>
        CreateStringEnum<T>(namingPolicy.ConvertName);

    private static OpenApiSchema CreateStringEnum<T>(Func<string, string> convertName)
        where T : struct, Enum =>
        new()
        {
            Type = "string",
            Enum = Enum.GetNames<T>()
                .Select(name => new OpenApiString(convertName(name)))
                .Cast<IOpenApiAny>()
                .ToList()
        };
}
