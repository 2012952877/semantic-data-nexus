# Azure deployment baseline

This directory defines a clean-room, resource group-scoped Azure baseline for a semantic natural-language analytics platform. The baseline creates no proprietary data, prompts, model deployments, tenant-specific identifiers, credentials, or application configuration.

## Topology

The deployment creates:

- Azure Container Apps environment with a public web placeholder, internal control and semantic API placeholders, and a manually triggered worker job.
- User-assigned managed identities for the control API, semantic API, and worker; the web app uses a system-assigned identity.
- Azure Container Registry with local admin and anonymous access disabled.
- PostgreSQL Flexible Server with Microsoft Entra authentication enabled and password authentication disabled.
- StorageV2 account and private `results` blob container with shared-key access disabled, versioning, change feed, and soft delete.
- Key Vault using RBAC authorization, soft delete, and environment-sensitive purge protection.
- Azure AI Search using Microsoft Entra data-plane authorization and no API keys.
- Log Analytics and workspace-based Application Insights.
- An optional Azure OpenAI account boundary with local authentication disabled. It creates no model deployment.
- Resource-scoped RBAC and diagnostic settings for supported services.

APIM, Front Door, private endpoints, DNS zones, and hub networking are intentionally outside the lowest-cost baseline. Add them before exposing a production workload.

## Prerequisites

- Azure CLI and Bicep CLI.
- Permission to create resources and role assignments in the target resource group. `Owner` or `User Access Administrator` plus `Contributor` is normally required for the initial deployment.
- Registered resource providers for the services in `infra/bicep/main.bicep`.
- A pre-created resource group in the selected Azure region.
- PostgreSQL Entra administrator details supplied at deployment time. Do not commit real identifiers.

## Local-to-Azure mapping

| Local responsibility | Azure baseline |
| --- | --- |
| Vue development server | Public Container App (`web`) |
| Typed control API/BFF | Internal Container App (`control-api`) |
| Python semantic/compiler/runtime API | Internal Container App (`semantic-api`) |
| Background process | Manual Container Apps Job (`worker`) |
| Local DuckDB operators | Ephemeral compute inside semantic API or worker |
| Local Parquet output | Private Blob `results` container |
| Local metadata store | PostgreSQL Flexible Server |
| Local search fixture | Azure AI Search |
| Local environment values | Managed identity plus Key Vault references where a service cannot use identity |
| Local logs/traces | Log Analytics and Application Insights |

The committed images are public placeholders. Build immutable application images in ACR and override the image parameters during deployment. The identities already have resource-scoped `AcrPull`.

## Validate and inspect changes

Static validation requires no Azure subscription:

```powershell
.\infra\scripts\validate.ps1
```

Run a non-mutating what-if after signing in:

```powershell
az login
.\infra\scripts\validate.ps1 `
  -ResourceGroup <resource-group> `
  -Environment dev `
  -PostgresEntraAdministratorObjectId <object-id> `
  -PostgresEntraAdministratorPrincipalName <display-name>
```

The CI workflow performs only Bicep build and parameter compilation, so pull requests do not require federated Azure credentials.

## Deploy

The committed parameter files contain an all-zero synthetic PostgreSQL administrator object ID and are examples, not unattended deployment inputs. Override the administrator values for every what-if and deployment:

```powershell
.\infra\scripts\validate.ps1 -ResourceGroup <resource-group> -Environment dev

az deployment group create `
  --resource-group <resource-group> `
  --template-file .\infra\bicep\main.bicep `
  --parameters .\infra\bicep\parameters\dev.bicepparam `
  --parameters postgresEntraAdministratorObjectId=<object-id> `
               postgresEntraAdministratorPrincipalName=<display-name> `
               postgresEntraAdministratorPrincipalType=Group
```

No password bootstrap path is provided. Workloads should obtain Microsoft Entra tokens through managed identity.

## Cost-sensitive defaults

- Container Apps can scale API placeholders to zero in non-production environments.
- The worker is a manual job and incurs compute only while executing.
- ACR uses Basic, Search uses Basic, PostgreSQL uses burstable compute, and storage uses locally redundant standard storage in development.
- Log Analytics uses a 30-day retention period and a 1 GB/day ingestion cap in development.
- Azure OpenAI is disabled by default and no model deployment is defined.
- Private endpoints, zone redundancy, geo-redundant backup, APIM, and Front Door are omitted from development because each adds fixed or usage-based cost.

These are cost categories, not price estimates. Confirm current regional pricing and quotas before deployment.

## Production hardening

The production example keeps authenticated public endpoints enabled because the baseline does not yet create a private network path. Set `publicNetworkAccessEnabled` to `false` only in the same change that adds working private endpoints, private DNS, and Container Apps VNet integration; disabling it earlier makes ACR, PostgreSQL, Storage, Key Vault, and Search unreachable.

Before production:

1. Integrate the Container Apps environment with a dedicated virtual network and use an internal environment where architecture permits.
2. Add private endpoints and private DNS for PostgreSQL, Storage, Key Vault, Search, ACR, Azure OpenAI, Log Analytics, and Application Insights; keep public data-plane access disabled.
3. Put Front Door with WAF or APIM in front of the public web/API boundary and enforce identity-aware authorization.
4. Use Premium ACR for Private Link and retention capabilities, zone-redundant services where available, PostgreSQL high availability, and tested backup restoration.
5. Replace public placeholders with signed, vulnerability-scanned, digest-pinned images.
6. Add Azure Policy, Defender for Cloud, resource locks, budgets, alerts, and deployment approvals.
7. Review diagnostic volume, sampling, retention, and privacy controls before enabling payload-level telemetry.

See [Azure deployment operations](../docs/operations/azure-deployment.md) for identity, networking, recovery, and observability procedures.

## Teardown

For an isolated development resource group, preview and then delete the resource group:

```powershell
az group delete --name <resource-group> --no-wait
```

Key Vault soft delete retains the vault after resource group deletion. Production purge protection is irreversible and prevents immediate purge. Retain or export required PostgreSQL and Blob data before teardown.
