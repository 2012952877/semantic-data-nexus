# 路线图 P0–P5

路线图以可验证的纵向能力为阶段，不以页面数量或代码量衡量完成度。人员数字是并行投入的建议区间，不是承诺日期。

| 阶段 | 目标与范围 | 退出标准 | 建议人员 | 主要风险 |
| --- | --- | --- | --- | --- |
| **P0 Architecture & Contracts** | Clean-room 治理、模块边界、Ontology/SQG v0、Run/Result/Lineage 契约、确定性验证 CLI | Schema 可验证；负例覆盖依赖/类型/成员关系；CI 通过；架构/ADR 可评审 | 1 Semantic/Backend + 0.5 Architect | 契约过度设计；术语不一致 |
| **P1 First vertical slice** | **唯一一个 Databricks Resolver**；合成 Ontology；受控问题 -> SQG -> 单引擎 Plan -> Runtime -> Result Manifest/Lineage | 不存在直连 LLM SQL；Databricks 集成测试使用隔离测试数据；取消/超时/预算有效；本地 DuckDB 路径可开发；Run 时间线可查询 | 2 Backend/Data + 1 Applied AI + 0.5 Platform/Security | 数据源能力误判；凭据与重试；Databricks 成本 |
| **P2 Usable MVP** | Vue Ask UI、ASP.NET Core BFF、Ontology/Resolver 基础管理、成员值解析、Azure AI Search、本地/云环境 | 关键问题集达到约定准确率；SSE 状态完整；权限负例通过；部署/回滚手册可演练 | P1 + 1 Frontend + 1 Platform + 0.5 Product/QA | 模型质量、成员值召回、前后端契约漂移 |
| **P3 Governance & operations** | 版本发布/回滚、审批、审计、Run 统计、OpenTelemetry SLO、成本与保留策略 | Ontology/Prompt/模型/Resolver 版本可追溯；故障演练达标；无敏感遥测；SLO 和预算告警有效 | 2 Backend + 1 Platform/SRE + 1 Frontend + Security/QA | 运维复杂度、日志泄露、控制面权限 |
| **P4 Multi-engine & optimization** | 第二数据源或计算引擎、Capability Catalog、成本模型、受限跨引擎计划 | Capability conformance suite 通过；计划可解释；错误估算不会绕过预算；无隐式语义变化 | 2 Query/Compiler + 2 Connector + 1 Platform | 联邦查询爆炸、类型/函数差异、数据移动成本 |
| **P5 Enterprise readiness** | 多租户强化、HA/DR、企业身份/策略、扩展 SDK、生态集成 | 威胁模型与渗透测试完成；RTO/RPO 演练；租户隔离验证；升级兼容与弃用策略生效 | 2 Platform/SRE + 2 Backend + 1 Security + Product/Support | 合规范围扩张、兼容债务、区域/主权要求 |

## P1 垂直切片的严格边界

第一条真实数据路径只实现一个 Databricks Resolver，不并行开发多个连接器。它必须展示：

1. 问题绑定到固定 Ontology 版本；
2. Compiler 只能产出 `sqg/v0`；
3. Validator/Binder 拒绝未知概念和不合法图；
4. Optimizer 依据 Databricks Capability 生成单引擎 Physical Plan；
5. Resolver 使用参数化/受控生成，不接受 LLM 任意 SQL；
6. Runtime 持久化 Run / Stage / Node 并支持超时、取消和预算；
7. 结果以 Parquet/JSON 分片与 committed Manifest 发布；
8. Answer 和 Lineage 可由同一 Run 追溯；
9. 同一契约可在本地 DuckDB/fixture 上进行确定性开发测试。

## 跨阶段质量门

- **安全**：威胁模型、最小权限、凭据引用、依赖和镜像扫描；
- **语义**：固定评测问题集、Ontology 版本、失败分类和人工复核样本；
- **契约**：兼容性测试、迁移说明、弃用窗口；
- **可靠性**：幂等、取消、超时、重试和部分失败测试；
- **可观测性**：Run/trace 关联、SLO、成本信号、敏感数据过滤；
- **Clean-room**：每个贡献的来源声明和检查清单。

## 显式非目标

- 不克隆任何专有产品的 UI、Prompt、内部对象模型或协议；
- 不允许 LLM 生成 SQL 后直接执行；
- P0/P1 不做任意 SQL 控制台或“高级模式”逃生通道；
- P0 不部署 Azure 资源，不交付 Bicep/Terraform；
- P1 不做多数据源联邦、自动 Ontology 生成或复杂成本优化；
- MVP 不承诺多区域主动/主动、离线移动端或像素级可视化编辑器；
- 不把向量数据库当作授权、语义契约或事实来源；
- 不在 PostgreSQL 中保存大查询结果，不在日志中保存业务数据正文。

