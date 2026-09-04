FROM mcr.microsoft.com/dotnet/sdk:8.0-alpine AS build
WORKDIR /src

COPY SemanticDataNexus.ControlApi.sln ./
COPY src/ControlApi/ControlApi.csproj src/ControlApi/
COPY tests/ControlApi.Tests/ControlApi.Tests.csproj tests/ControlApi.Tests/
RUN dotnet restore SemanticDataNexus.ControlApi.sln

COPY . .
RUN dotnet publish src/ControlApi/ControlApi.csproj \
    --configuration Release \
    --output /app/publish \
    --no-restore \
    /p:UseAppHost=false

FROM mcr.microsoft.com/dotnet/aspnet:8.0-alpine AS final
WORKDIR /app
RUN apk add --no-cache curl tzdata
COPY --from=build /app/publish .

ENV ASPNETCORE_HTTP_PORTS=8080
EXPOSE 8080
USER app
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8080/health/ready"]
ENTRYPOINT ["dotnet", "ControlApi.dll"]
