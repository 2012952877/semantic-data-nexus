# ADR-0005：初始技术选择

- 状态：Accepted
- 日期：2026-09-03

## 2026-09-05 状态澄清

以下保留初始决策的历史背景，不作为交付清单。M0 已包含 Vue/TypeScript 与
ASP.NET Core BFF，不能再把这两项描述为尚无实现；PostgreSQL 控制持久化、
durable object-store results、企业 OIDC/workspace、真实模型与搜索/观测集成
则不能因本 ADR 的选型文字而计为已交付。
当前阶段和状态以 [roadmap](../roadmap.md)、
[Build vs Buy](../architecture/build-vs-buy.md) 和
[ADR-0006](0006-product-contract-foundations.md) 为准。
具体云服务和第三方 adapter 仍需版本、许可、兼容与运行证据，不自动安装或部署。

## 背景

P0 需要实现可运行的契约核心，同时为 UI、控制面、Azure 与本地开发保留清晰方向，但不能把文档选型误当作已交付系统。

## 决策

- Semantic/Core/Compiler 初期使用 Python 3.12 与 Pydantic v2；
- 契约使用 JSON Schema Draft 2020-12；
- 未来 Web UX 使用 Vue 3 + TypeScript；
- 未来 BFF/控制面优先 ASP.NET Core，控制元数据使用 PostgreSQL；
- 第一真实 Resolver 是 Databricks，开发/小结果计算优先 DuckDB；
- 生产结果使用 Blob + Parquet + committed Manifest，本地使用文件系统/MinIO；
- Azure 运行优先 Container Apps，身份/密钥使用 Entra ID、Managed Identity、Key Vault；
- 检索优先 Azure AI Search，LLM 通过 Azure OpenAI/Microsoft Foundry provider；
- 可观测性使用 OpenTelemetry，Azure 后端使用 Application Insights/Log Analytics。

本决策不授权 P0 创建云资源，也不排除经过基准测试后选择 DataFusion、Temporal 等替代项。详细 Build vs Buy 见架构矩阵。

## 结果

P0 保持依赖最小且本地可运行；后续技术引入必须通过接口和 ADR，而不能让 Azure SDK 或框架类型泄漏到核心契约。
