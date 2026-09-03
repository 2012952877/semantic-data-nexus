namespace ControlApi;

public static class ProductionSafety
{
    public static void Validate(
        string environmentName,
        bool localDevelopmentAuthEnabled,
        bool fakeSemanticBackendEnabled)
    {
        if (!string.Equals(environmentName, "Production", StringComparison.OrdinalIgnoreCase))
        {
            return;
        }

        if (localDevelopmentAuthEnabled)
        {
            throw new InvalidOperationException(
                "Local development authentication cannot be enabled in Production.");
        }

        if (fakeSemanticBackendEnabled)
        {
            throw new InvalidOperationException(
                "The fake semantic backend cannot be enabled in Production.");
        }
    }
}
