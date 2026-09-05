# 标准产品路线图：M0 → M1 → M2 → M3

总目标由 [#28](https://github.com/2012952877/semantic-data-nexus/issues/28) 跟踪：
交付可独立部署、具有逻辑 tenant/workspace 隔离的完整标准产品，再以实际证据进入商业交付。
不是扩充几个演示页面，也不默认开放公共多租户 SaaS 或真实计费。

本路线图替代原 P0–P5 排期。阶段表示交付门槛，不是日期、人员承诺或认证声明。
机器可读范围及逐项正例、反例、集成验收位于
[`contracts/product/v1/capabilities.json`](../contracts/product/v1/capabilities.json)；
状态含义见[产品契约说明](../contracts/product/README.md)。

## 已交付基线与尚未交付的能力

M0 `v0.1.0` 固定于 `3d53a2404e74d76c160aaeb92ace1e8a3e6bd999`：
Vue 工作台、ASP.NET Core 控制 API、Python 编译/执行服务、类型化 v0 契约、
两个确定性合成场景、受限 resolver 和本地集成基线已经存在。
**M0 不是标准产品，也不是商业就绪证明。**
运行/控制元数据和 inline 结果是进程内状态，编译器没有通用自然语言能力；
可选 Databricks 路径不等于所有连接器已验证。

此前文档中“未来 Vue/BFF”已经落后于 M0；此前写成完整能力的
“持久化运行、生产对象存储、企业授权、通用编译”则仍是目标。
部署配置或受保护演示入口不能证明这些应用层能力已实现。
Hosting #27 与本产品增量分开；不改变 M0 tag，不修改现有演示资源，不自动 redeploy。

| 阶段 | GitHub milestone | 目标 | 退出证据 |
| --- | --- | --- | --- |
| M1 企业与真实模型基础 | [milestone 2](https://github.com/2012952877/semantic-data-nexus/milestone/2) | 可执行范围契约、真实模型协议适配、事务性控制持久化、OIDC/workspace 授权、catalog-driven 编译与澄清 | 契约负例、真实数据库事务/重启、身份隔离负例、provider 协议及失败证据；live 模型证据与 mock 明确分开 |
| M2 标准工作流 | [milestone 3](https://github.com/2012952877/semantic-data-nexus/milestone/3) | durable runtime、对象结果、Ontology/KB 生命周期、全部标准控制台领域、受控 connector/operator conformance | UI/API/存储/授权端到端闭环；取消、重连、恢复、版本与语义等价证据 |
| M3 商业运行保障 | [milestone 4](https://github.com/2012952877/semantic-data-nexus/milestone/4) | 审计、metering/quotas、标准导出、license/SBOM、升级、恢复和负载/故障证据 | 固定候选版本及工作负载下的可复现实验、依赖审查、恢复记录；无未证实的合规认证或性能承诺 |

## 实施顺序与唯一责任 issue

| Issue | 范围与主责任面 | 依赖 / 边界 |
| --- | --- | --- |
| #29 · M1 | 产品能力 registry、workspace/principal/resource/audit/usage 契约与离线验收 | 本次增量；不接入中间件，不改变 v0 |
| #30 · M1 | Python 真实 structured model provider 协议 | 独立实现；本次限定 OpenAI Chat Completions 协议和 loopback mock，live 模型、Azure/private gateway 未实现或未实测，不得由 mock 推导上线能力 |
| #31 · M1 | PostgreSQL 控制元数据、事务、幂等、迁移 | 独立实现；不是 runtime/结果存储的自动持久化 |
| #32 · M1 | OIDC、workspace membership、用户/组及资源授权、secret references | #29、#31；issuer+subject 和服务端授权成员关系是权威 |
| #33 · M1 | 通用 catalog-driven compiler、多轮解析与澄清 | #29、#30；保留确定性 validator；完整交互还需 #32 和后续 durable runtime |
| #34 · M2 | durable run/event/result、恢复、manifest、lineage、流控 | #29、#31；绑定 #32 的授权上下文，不把控制库误当大结果存储 |
| #35 · M2 | Ontology 与知识库 authoring/import/publish/rollback、检索/同步/索引生命周期 | #32；不可把 member resolution 等同任意文档聊天 |
| #36 · M2 | 完整标准 workbench/admin、API reference、help | #35 及对应已集成后端能力；不能以页面或 mock 代替功能 |
| #37 · M2 | connector/operator catalog、测试工作台与 pushdown conformance | #29；每个 adapter 按版本与能力逐项通过，ACT 单独授权 |
| #38 · M3 | 审计、metering/quotas、OTel/OpenLineage、license/SBOM、升级/恢复/故障/负载保障 | #32、#34 及各已集成标准面；不自动启用真实计费 |

表中 milestone 是责任 issue 的实施阶段；较早阶段的接口工作不意味着依赖后续阶段的
完整用户工作流已经集成。registry 的依赖图才是 capability promotion 的前置检查。
Sibling PR 的实现状态在合并并审阅证据后再更新，不能预先计入本次交付。

## 标准版覆盖与验收方式

以下是对脱敏观察的独立能力归纳，不复制参考产品的接口、内部模型、提示词或页面结构。
观察置信度只说明“看到该领域”，不证明其后台算法、已部署连接器或实际安全性质。

| 标准领域 | 完成时必须体现的用户行为 |
| --- | --- |
| Ask | single、chat、ontology-assistant、clarification；流式进度、受控执行、结果与 lineage |
| Runs / Statistics | 检索、详情、取消、pin、保留策略、按范围聚合；重启后仍一致 |
| My Data | 上传预检、目录管理、引用检查、授权删除 |
| Ontologies / Knowledge Bases | 模型版本、发布/回滚、binding/policy、member 检索、同步/取消/索引生命周期 |
| Resolvers / Resolver Test | 发现、校验、导入预览、sample、动态测试规格与受限诊断 |
| Credentials / LLMs / Prompt Templates | secret 引用、模型/原创模板版本、默认、启停、授权与运行 provenance |
| Compute Engines / Result Stores | 注册、测试、能力/版本约束、结果先写后发布、分页/导出 |
| Users / Groups | 权威身份与成员关系、授权变更和撤销，不依赖 UI 隐藏按钮 |
| Run Feedback / API Logs / Replay | 受控反馈审阅、脱敏日志、校验过的离线事件回放；回放不能重新执行 |
| API Reference / Help | 我们自己的版本化契约、错误、边界和恢复指导；规划功能不得冒充可用功能 |

Operator inventory 共 26 个：ACT、AGGREGATE、ASK、DATE、DEDUPLICATE、DERIVE、
DISTINCT、EXCEPT、EXPLODE、FILTER、IMPUTE、INTERSECT、JOIN、PICK、PIVOT、PROJECT、
RESAMPLE、SAMPLE、SEARCH、SELECT、SORT、SUMMARIZE、UNION_ALL、UNION_DISTINCT、
UNPIVOT、WINDOW。每个条目单独记录缺口和验收；**inventory 不等于实现**。
M0 已有的 typed subset 不被抹去，但也不自动升级为完整标准 conformance。
ACT 当前 unsupported；未来必须显式授权具体 tool/action/resource，不允许隐式执行写操作。

## 跨阶段发布门

- `implemented` 只说明指定范围的代码/契约及正反例存在；`integrated` 需要跨面路径证据；
  `verified` 还需要固定候选版本、环境、失败用例和可复查结果。不能把 schema 当安全控制。
- 兼容：保留 `contracts/v0` 和现有 API；新契约采用独立版本，接入和迁移另行交付。
- 安全：OIDC 校验、服务端 membership、workspace 隔离、secret reference、拒绝隐式写操作。
- 可靠性：幂等、顺序事件、取消/重试、部分失败、对象提交、备份恢复；声明具体实验边界。
- 商业：精确版本的依赖 license/NOTICE/SBOM；真实 usage 和 quota 原子性；
  没有实测不得承诺准确率、SLO、RPO/RTO 或成本排名。
- Clean-room：仅独立行为归纳与合成示例；不得提交私有 URL/ID、业务数据、提示词、截图或资产。

技术默认和待验证替代路线见 [Build vs Buy](architecture/build-vs-buy.md)。
