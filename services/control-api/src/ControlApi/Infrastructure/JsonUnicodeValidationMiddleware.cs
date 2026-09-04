using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace ControlApi.Infrastructure;

public sealed class JsonUnicodeValidationMiddleware(RequestDelegate next)
{
    private const int MaximumBufferedRequestBytes = 1_048_576;

    public async Task InvokeAsync(HttpContext context)
    {
        if (context.Request.HasJsonContentType())
        {
            context.Request.EnableBuffering(
                bufferThreshold: 30 * 1_024,
                bufferLimit: MaximumBufferedRequestBytes);
            Stream? transcodingStream = null;
            try
            {
                var sourceEncoding = GetRequestEncoding(context.Request.ContentType);
                var jsonStream = context.Request.Body;
                if (sourceEncoding.CodePage != Encoding.UTF8.CodePage)
                {
                    transcodingStream = Encoding.CreateTranscodingStream(
                        context.Request.Body,
                        sourceEncoding,
                        Encoding.UTF8,
                        leaveOpen: true);
                    jsonStream = transcodingStream;
                }

                using var document = await JsonDocument.ParseAsync(
                    jsonStream,
                    new JsonDocumentOptions
                    {
                        AllowTrailingCommas = false,
                        CommentHandling = JsonCommentHandling.Disallow,
                        MaxDepth = 64
                    },
                    context.RequestAborted);
                ValidateStrings(document.RootElement);
            }
            catch (Exception exception) when (
                exception is JsonException or
                    InvalidOperationException or
                    IOException or
                    DecoderFallbackException or
                    ArgumentException or
                    FormatException or
                    NotSupportedException)
            {
                throw new BadHttpRequestException(
                    "The JSON request body is invalid.",
                    exception);
            }
            finally
            {
                if (transcodingStream is not null)
                {
                    await transcodingStream.DisposeAsync();
                }

                context.Request.Body.Position = 0;
            }

        }

        await next(context);
    }

    private static Encoding GetRequestEncoding(string? contentType)
    {
        var charset = MediaTypeHeaderValue.Parse(contentType!).CharSet?.Trim('"').ToLowerInvariant();
        return charset switch
        {
            null or "" or "utf-8" or "unicode-1-1-utf-8" =>
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true),
            "utf-16" or "utf-16le" or "unicode" =>
                new UnicodeEncoding(
                    bigEndian: false,
                    byteOrderMark: true,
                    throwOnInvalidBytes: true),
            "utf-16be" =>
                new UnicodeEncoding(
                    bigEndian: true,
                    byteOrderMark: true,
                    throwOnInvalidBytes: true),
            _ => throw new NotSupportedException("The JSON request charset is not supported.")
        };
    }

    private static void ValidateStrings(JsonElement element)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                foreach (var property in element.EnumerateObject())
                {
                    _ = property.Name;
                    ValidateStrings(property.Value);
                }

                break;
            case JsonValueKind.Array:
                foreach (var item in element.EnumerateArray())
                {
                    ValidateStrings(item);
                }

                break;
            case JsonValueKind.String:
                _ = element.GetString();
                break;
        }
    }
}
