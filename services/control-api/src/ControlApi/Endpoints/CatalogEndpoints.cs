using System.Buffers;
using System.Text;
using System.Text.RegularExpressions;
using ControlApi.Authentication;
using ControlApi.Semantic;

namespace ControlApi.Endpoints;

public static class CatalogEndpoints
{
    public static IEndpointRouteBuilder MapCatalogApi(this IEndpointRouteBuilder endpoints)
    {
        var group = endpoints.MapGroup("/api/v1/catalog")
            .WithOpenApi().RequireRateLimiting("api").RequireAuthorization(Policies.Compiler);
        group.MapPost("/queries", async (CatalogQueryRequest request, RunAccess access,
            ICatalogBackendClient client, CancellationToken cancellationToken) =>
        {
            ValidatePin(request.Catalog, access);
            if (request.ContractVersion != "catalog-compile/v1" ||
                !ValidId(request.RequestId) || !ValidQuestion(request.Question))
            {
                throw new BadHttpRequestException("Invalid catalog query");
            }
            return await client.QueryAsync(request, cancellationToken);
        });
        group.MapPost("/clarifications/{identifier}/answers", async (string identifier,
            CatalogAnswerRequest request, RunAccess access, ICatalogBackendClient client,
            CancellationToken cancellationToken) =>
        {
            ValidatePin(request.Catalog, access);
            if (!ValidId(identifier) || request.ContractVersion != "catalog-answer/v2" ||
                !ValidId(request.RequestId) ||
                request.Revision < 1 || !ValidId(request.ChoiceId))
            {
                throw new BadHttpRequestException("Invalid clarification answer");
            }
            return await client.AnswerAsync(identifier, request, cancellationToken);
        });
        return endpoints;
    }

    private static bool ValidId(string? value) =>
        value is not null && value.Length <= 128 && Regex.IsMatch(value, "^[A-Za-z0-9][A-Za-z0-9._:-]*\\z");

    internal static bool ValidQuestion(string? value)
    {
        if (string.IsNullOrEmpty(value)) return false;
        var span = value.AsSpan();
        var count = 0;
        var hasContent = false;
        while (!span.IsEmpty)
        {
            if (Rune.DecodeFromUtf16(span, out var scalar, out var consumed) != OperationStatus.Done ||
                ++count > 4000)
                return false;
            hasContent |= !Rune.IsWhiteSpace(scalar) && scalar.Value is not (>= 0x1c and <= 0x1f);
            span = span[consumed..];
        }
        return hasContent;
    }

    private static void ValidatePin(CatalogPin pin, RunAccess access)
    {
        var context = access.Context ?? throw new IdentityAccessException();
        if (pin is null || pin.Scope is null || pin.ContractVersion != "resource-version/v1" ||
            pin.ResourceKind != "ontology" || !ValidId(pin.ResourceId) || pin.Revision < 1 ||
            pin.ContentSha256 is null || !Regex.IsMatch(pin.ContentSha256, "^[a-f0-9]{64}\\z"))
        {
            throw new BadHttpRequestException("Invalid catalog pin");
        }
        if (pin.Scope.TenantId != context.Scope.TenantId || pin.Scope.WorkspaceId != context.Scope.WorkspaceId)
        {
            throw new IdentityAccessException();
        }
    }
}
