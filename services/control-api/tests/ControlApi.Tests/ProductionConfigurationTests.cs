namespace ControlApi.Tests;

public sealed class ProductionConfigurationTests
{
    [Fact]
    public void ProductionRejectsDevelopmentAuthentication()
    {
        var exception = Assert.Throws<InvalidOperationException>(() =>
            ProductionSafety.Validate("Production", true, false));

        Assert.Contains(
            "Local development authentication cannot be enabled in Production",
            exception.ToString(),
            StringComparison.Ordinal);
    }

    [Fact]
    public void ProductionRejectsFakeSemanticBackend()
    {
        var exception = Assert.Throws<InvalidOperationException>(() =>
            ProductionSafety.Validate("Production", false, true));

        Assert.Contains(
            "fake semantic backend cannot be enabled in Production",
            exception.ToString(),
            StringComparison.OrdinalIgnoreCase);
    }
}
