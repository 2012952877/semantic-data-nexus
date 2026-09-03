namespace ControlApi.Tests;

public sealed class ProductionConfigurationTests
{
    [Fact]
    public void ProductionRejectsDevelopmentAuthentication()
    {
        var exception = Assert.Throws<InvalidOperationException>(() =>
            HostingSafety.Validate("Production", true, false));

        Assert.Contains(
            "Local development authentication can only be enabled in Development",
            exception.ToString(),
            StringComparison.Ordinal);
    }

    [Fact]
    public void ProductionRejectsFakeSemanticBackend()
    {
        var exception = Assert.Throws<InvalidOperationException>(() =>
            HostingSafety.Validate("Production", false, true));

        Assert.Contains(
            "fake semantic backend cannot be enabled in Production",
            exception.ToString(),
            StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("Staging")]
    [InlineData("QA")]
    [InlineData("Test")]
    public void NonDevelopmentEnvironmentsRejectHeaderAuthentication(string environment)
    {
        var exception = Assert.Throws<InvalidOperationException>(() =>
            HostingSafety.Validate(environment, true, false));

        Assert.Contains(
            "only be enabled in Development",
            exception.Message,
            StringComparison.Ordinal);
    }
}
