namespace ControlApi;

public static class HostingSafety
{
    public static void Validate(
        string environmentName,
        bool localDevelopmentAuthEnabled,
        bool fakeSemanticBackendEnabled)
    {
        var isDevelopment = string.Equals(
            environmentName,
            "Development",
            StringComparison.OrdinalIgnoreCase);
        var isProduction = string.Equals(
            environmentName,
            "Production",
            StringComparison.OrdinalIgnoreCase);

        if (localDevelopmentAuthEnabled && !isDevelopment)
        {
            throw new InvalidOperationException(
                "Local development authentication can only be enabled in Development.");
        }

        if (isProduction && fakeSemanticBackendEnabled)
        {
            throw new InvalidOperationException(
                "The fake semantic backend cannot be enabled in Production.");
        }
    }
}
