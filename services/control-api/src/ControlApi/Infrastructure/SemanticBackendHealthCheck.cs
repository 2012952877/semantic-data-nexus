using ControlApi.Semantic;
using Microsoft.Extensions.Diagnostics.HealthChecks;

namespace ControlApi;

public sealed class SemanticBackendHealthCheck(ISemanticBackendClient client) : IHealthCheck
{
    public async Task<HealthCheckResult> CheckHealthAsync(
        HealthCheckContext context,
        CancellationToken cancellationToken = default)
    {
        var ready = await client.IsReadyAsync(cancellationToken);
        return ready
            ? HealthCheckResult.Healthy()
            : HealthCheckResult.Unhealthy("Semantic backend is not ready.");
    }
}
