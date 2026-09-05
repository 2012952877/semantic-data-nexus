# Build vs Buy：保留语义权威，选择有限的标准集成

本决策服务于 #28 的标准产品，而不是为演示拼装更多平台。
**本项目继续拥有语义权威**：Ontology/SQG、binding/policy、确定性验证、
能力匹配、执行授权、Run/Result/Lineage 与版本兼容。
第三方可以提供模型、存储或 adapter，但其内部模型、提示词和业务逻辑不能成为我们的公共契约。

本页替代此前未锁定版本的产品许可表和“WrenAI 首选起点”结论。
此前列出的 Apache/MIT/商业边界不能作为任何版本的采用依据；
本增量没有安装 WrenAI、Cube 或其他候选平台，也没有取得它们的商业采用许可结论。

## 当前实现与工作默认不是一回事

| 层 | 工作默认 / 自建责任 | 本仓库状态与下一道门 |
| --- | --- | --- |
| 公共接口 | 自有版本化 JSON Schema + OpenAPI | M0 JSON Schema 已存在；本次增补 product/v1。完整 API drift/reference 集成由 #36，商业互操作保障由 #38 |
| 身份与授权 | OIDC/OAuth2 接入 IdP；自有 tenant/workspace membership 与资源 policy | #29 仅定义 context；#32 验证 issuer、subject、audience、有效期和成员关系。任意 request header 不构成身份权威 |
| 控制元数据 | PostgreSQL；自建事务、scope、幂等、版本、迁移 | #31；不能从数据库选型推断 runtime/results 已持久化 |
| 结果与批数据 | 对象存储 + 自有 committed manifest；Arrow/Parquet 为目标交换格式 | #34；M0 inline 结果仍是进程内。对象存储实现需 adapter、分页与失败恢复证据 |
| 模型 | 受限 structured-output provider；自有验证器保留最终裁决 | #30 只限定 OpenAI Chat Completions 协议与 loopback mock；live、Azure/private gateway 不计入已验证支持；#33 负责通用编译 |
| 观测 | OpenTelemetry 标准接口；后端可替换 | #38；Run 状态库不能用遥测后端替代，遥测不得包含原始 prompt/SQL/结果/凭据 |
| Lineage | 自有细粒度 lineage；目标导出 OpenLineage | #38；需锁定协议版本、facet 映射及隐私过滤，不声称现已兼容 |
| 凭据 | 仅保存受控 secret reference；adapter 边界按授权解析 | #32；secret manager/工作负载身份由部署环境决定，配置引用本身不授予权限 |
| 执行 | 继续使用现有受限 resolver/runtime 边界，按 catalog 宣告已验证能力 | #37；现有 fake/opt-in Databricks 不是无限 connector 支持，也不代表 26 个 operator 均已实现 |

这里的“默认”是接口与数据职责决策，不是安装清单或云资源部署授权。
PostgreSQL 保存控制元数据；大结果进入对象存储。模型输出必须经过 typed SQG、
绑定、策略与预算验证；无论使用哪家 provider，都不能直接执行模型 SQL。
ACT 仍为 unsupported，未来必须显式授权 tool/action/resource，不能借第三方 agent 自动写入。

## 市场替代方案与同条件比较

共同任务是：在指定 tenant/workspace、固定语义版本和预算下回答一组问题，
输出可追溯结果，并在取消、进程重启和权限撤销后保持正确行为。
不是先假定某个平台“更完整”，也不是把检索库与完整分析产品当作同类替代品。
以下仅列待研究路线，**未做同条件基准测试，不给性能、成本或许可排名**。

| 路线 | 与本项目的关系 | 比较时必须取得的证据 | 当前决定 |
| --- | --- | --- | --- |
| 保留现有自有 compiler/runtime，接入标准模型与存储 adapter | 当前基线；完整承担语义和运行控制 | M0 两场景以外的 held-out catalog、恢复、隔离、运维成本 | 优先扩展现有边界，不重做已交付 v0 |
| WrenAI / Cube | 候选语义或分析组件，不能假设能直接替代自有 SQG/runtime | 锁定 commit 的模块 license/NOTICE、可使用的接口、语义映射、工作区隔离及恢复边界 | 未采用；不再指定 WrenAI 为优先起点 |
| MetricFlow / Malloy | 候选建模/指标作者工具，不等同完整标准产品 | 既有模型迁移、表达力、类型/空值语义、部署组件和许可 | 有具体作者需求时再做独立 adapter ADR |
| Vanna / DB-GPT | 候选交互或 agent 组件，不是执行授权边界 | 受限工具行为、结构化输出、敏感数据处理、版本许可及失败隔离 | 未采用；不得复制上游或专有 prompts/business logic |
| OpenMetadata / DataHub | 互补目录/治理路线，不替代 compiler 或 query engine | 自有资源版本和 grant 映射、同步冲突、运维负担、版本许可 | 目录规模有证据后择一比较，不同时引入 |
| Trino / DataFusion / Calcite / Substrait | 联邦执行、执行内核或计划交换的不同路线，并非同类整体平台 | 确切职责、类型/函数等价、成本预算、跨源策略、adapter 与许可 | 第二引擎需求和基准明确后才选；不宣称 v0 兼容 |
| Temporal / Dagster | 运行持久化或资产编排的候选路线，需区分交互 run 与离线作业 | 取消/重试/补偿语义、恢复范围、运维组件、授权传递及锁定许可 | 先完成 #34 的明确状态/结果契约，再按瓶颈决策 |
| 企业已有 BI semantic model / 托管目录 | 可复用既有权威资产，不是本项目全功能替代 | 可调用 API、许可和租户边界、模型版本、导出限制、迁移代价 | 有目标客户资产时单独评估；不推断任何托管产品已接入 |

所有路线使用同一组带标签的合成问题和反例：类型/decimal/null、join 基数、
policy、流式结果、取消、事件重连、部分写入、进程恢复、workspace 隔离。
记录版本、配置、资源、数据规模、操作成本和失败结果；
未运行的维度标记“未验证”，不能用主页宣传或安装成功代替。

如果客户已有稳定指标模型，保留其模型并构建窄 adapter 可能比迁移更合适；
如果只需确定性 SQL 分析，不应为“完整标准版”强行引入 agent 平台。
实际选择仍需上述同条件证据，不在本 PR 推测结论。

## 采用门：先锁定许可证，再讨论安装

| 门 | 必需记录 | 未通过时 |
| --- | --- | --- |
| 身份 | 官方仓库、精确 tag/commit、包与镜像 digest、依赖锁文件 | 不做采用决定 |
| 法律与来源 | 对该锁定版本实际读取 LICENSE/NOTICE；模块/文件例外、传递依赖、商业托管/嵌入限制及必要审批 | 状态保持“未验证”，禁止以其他版本或项目口号替代 |
| 契约与 adapter | 自有 contract 映射、支持/不支持矩阵、版本兼容和拒绝路径 | 不暴露给 planner/runtime |
| 质量与运维 | 合成 conformance、恢复/取消/权限反例、容量与维护成本证据 | 不提升为 integrated/verified |
| 发布供应链 | 与实际 artifact 一致的 SBOM、NOTICE、漏洞/来源记录与升级策略 | #38 发布门阻断 |

本次没有新增生产依赖或选择第三方产品版本，因此没有“已审查某上游许可证”的声明。
现有依赖仍需 #38 对最终锁定发布物做完整复核；本仓库 LICENSE 不自动覆盖全部依赖。
具体云托管服务、IdP、secret manager 与 object-store adapter 需部署专属决定，
不能从这张默认表推断已购买、已部署或通过认证。

## 标准入口与后续决策位置

以下是后续固定版本审查的公开入口，不是当前兼容或许可证证明：

- JSON Schema Draft 2020-12：<https://json-schema.org/draft/2020-12>
- OpenAPI：<https://spec.openapis.org/oas/latest.html>
- OpenID Connect Core：<https://openid.net/specs/openid-connect-core-1_0.html>
- OpenTelemetry：<https://opentelemetry.io/docs/specs/>
- Arrow / Parquet：<https://arrow.apache.org/docs/format/Columnar.html>、<https://parquet.apache.org/docs/>
- PostgreSQL：<https://www.postgresql.org/docs/>
- OpenLineage：<https://openlineage.io/docs/spec/>

产品依赖图与实施阶段以 [roadmap](../roadmap.md) 和
[product contracts](../../contracts/product/README.md) 为准。
新增 adapter 必须补具体 ADR、锁定许可记录和 conformance 证据，而不是继续扩充平台清单。
