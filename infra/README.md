# Infrastructure boundary

P0 **不包含可部署 Azure 基础设施**。本目录只记录未来 IaC 的边界，避免架构文档被误认为已可部署。

后续独立 PR 将优先使用 Bicep，并在生成资源前完成：

- 环境、订阅、区域、命名和标签约定；
- Container Apps、PostgreSQL、Storage、Key Vault、AI Search、Azure OpenAI/Foundry 和 Monitor 的 SKU/配额决策；
- VNet、子网、Private Endpoint、Private DNS 和出站控制；
- Managed Identity、RBAC、OBO 和 break-glass 流程；
- 诊断设置、预算、备份、保留、RTO/RPO 和销毁策略；
- What-if、lint、安全扫描和最小权限部署身份；
- 本地 Docker Compose/PostgreSQL/MinIO 等价环境。

本目录在该 PR 中不得出现订阅 ID、租户 ID、资源名、凭据、部署脚本或 Terraform/Bicep 资源。

