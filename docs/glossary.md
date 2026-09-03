# 术语表

本文用尽量少的前置知识解释项目中的核心名词。英文技术名保留，便于查阅公开资料。

## 业务与语义

| 术语 | 初学者解释 |
| --- | --- |
| **Ontology（本体）** | 一份版本化的“业务词典 + 关系图”。它说明系统允许讨论哪些 Entity、Field、Metric 和 Relation，而不是让模型从数据库列名临时猜测业务含义。 |
| **Semantic Layer（语义层）** | 位于用户语言与物理数据之间的稳定抽象层。它统一“收入”“活跃客户”等定义，并隐藏不同数据库的实现差异。 |
| **Control Plane（控制面）** | 管理配置和策略的部分，如 Ontology 版本、Resolver、凭据引用、模型配置、权限和发布流程。它不等于实际扫描大数据的 Data Plane。 |
| **Data Plane（数据面）** | 真正执行查询、读取数据、写结果的部分。它必须服从控制面发布的版本和策略。 |
| **Entity（实体）** | 可被业务讨论的对象或事实集合，例如 `sales`、`customer`、`order`。它不一定一对一对应数据库表。 |
| **Field（字段）** | Entity 上有类型的属性，例如 `sales.region` 或 `order.created_at`。Field ID 区分大小写并要求精确匹配。 |
| **Metric（指标）** | 有统一口径的可计算业务量，例如利润、订单数、转化率。Metric 应声明来源 Entity、字段、聚合或表达式，而不是只保存一个显示名。 |
| **Dimension（维度）** | 用来分组、筛选或切片的字段，例如地区、季度、产品类别。 |
| **Relation（关系）** | 两个 Entity 如何关联的语义声明，包括连接字段和基数，如一对多。Relation 是允许 JOIN 的白名单。 |
| **Cardinality（基数）** | 关系两端“一条记录对应多少条记录”的约束，如 `one_to_many`。错误基数会导致重复计数。 |
| **Resolver（解析器）** | 把一个受控的语义操作转成具体数据源操作的适配器。例如 Databricks Resolver 可把 Physical Plan 编译为参数化查询并执行；成员值 Resolver 可把“华东”解析为规范值。 |
| **Member-value resolution（成员值解析）** | 将用户输入的实体取值映射到数据中的规范值。例如“北美区”可能匹配 `North America`。它不同于把业务名映射到字段。 |
| **Knowledge Base（知识库）** | 为编译器提供经治理的业务定义、同义词、示例和说明的资料集合。它不能替代 Ontology 的强约束。 |

## 编译与计划

| 术语 | 初学者解释 |
| --- | --- |
| **SQG（Semantic Query Graph）** | 语义查询图。它是由有类型节点组成的 DAG，描述“选哪些语义概念、怎样筛选、聚合、关联和投影”，但不携带可直接执行的任意 SQL。 |
| **Typed IR（强类型中间表示）** | 编译过程中的机器可验证结构。SQG 就是一种 Typed IR：操作符和参数由 Schema 限定，未知字段不会被悄悄接受。 |
| **Operator（操作符）** | SQG 节点的动作类型。v0 只允许 `SELECT`、`FILTER`、`AGGREGATE`、`PIVOT`、`DERIVE`、`PROJECT`、`SORT`、`JOIN`。扩展必须发布新契约版本。 |
| **DAG（有向无环图）** | 节点之间只有单向依赖，且不能绕一圈回到自己。本项目还要求依赖节点先出现，使验证和重放完全确定。 |
| **Root（根/交付节点）** | SQG 最终对外产生结果的唯一节点。v0 要求图中每个节点都能沿依赖链到达 Root，也就是所有节点都属于 Root 的祖先闭包；孤立或位于 Root 下游的节点会被拒绝。 |
| **Initializer** | 在编译前冻结本次请求的上下文：用户、租户、策略、Ontology 版本、可用 Resolver、区域和预算。后续阶段读取这个快照，而不是使用会漂移的“当前配置”。 |
| **Compiler（编译器）** | 把自然语言意图变成 SQG 候选的组件。LLM 可参与候选生成，但最终输出必须通过结构化输出和确定性校验。 |
| **Deterministic validation（确定性校验）** | 相同输入永远得到相同结果的代码检查，包括 Schema、类型、依赖、输出、Ontology 成员和策略检查。它不依赖模型“再判断一次”。 |
| **Semantic Binding（语义绑定）** | 将 SQG 中的名字绑定到某个精确 Ontology 版本里的 Entity、Field、Metric、Relation 和 Resolver。绑定后不能因别名或新版本而悄悄变义。 |
| **Logical Plan（逻辑计划）** | 已验证“要做什么”的引擎无关计划，例如先筛选再聚合。它还没有决定由哪个系统执行。 |
| **Physical Plan（物理计划）** | 已决定“在哪里、怎样做”的可执行计划，例如筛选推到 Databricks、局部整形交给 DuckDB。它只能包含目标执行器声明支持的能力。 |
| **Capability-aware Optimizer（能力感知优化器）** | 根据 Resolver/Engine 的能力声明、成本、数据位置和策略选择 Physical Plan；不会假设所有引擎都支持相同函数或类型。 |
| **Pushdown（下推）** | 把过滤、聚合、投影等操作尽量放到数据所在引擎执行，以减少传输和本地计算。下推前必须验证引擎能力和语义等价性。 |
| **Substrait** | 一个开放的跨语言计算计划格式，可作为未来 Physical Plan 互操作参考。本项目 v0 不直接承诺 Substrait 兼容。 |
| **Query budget（查询预算）** | 对扫描量、返回行数、执行时间、并发和费用的上限。Optimizer 与 Runtime 都要执行预算。 |

## 运行、结果与可观测性

| 术语 | 初学者解释 |
| --- | --- |
| **Runtime / Coordinator** | 按 Physical Plan 调度任务、重试可重试步骤、取消运行、提交结果并记录状态的组件。Coordinator 管流程，Resolver 管数据源细节。 |
| **Run / Stage / Node** | 三层执行单位。Run 对应一次用户请求；Stage 是可独立调度的一组工作；Node 对应计划中的一个最小操作。三层状态便于时间线、统计和故障定位。 |
| **Result Store（结果存储）** | 保存结果数据和 Manifest 的系统。生产建议 Blob + Parquet；本地可用文件系统或 MinIO。它不是控制面 PostgreSQL。 |
| **Result Manifest（结果清单）** | 结果的“提交收据”，记录分片 URI、哈希、行数、字节数、格式和提交时间。只有 `committed=true` 的 Manifest 才能被读取。 |
| **Answer Contract（答案契约）** | 面向 UI 的稳定返回结构，包括摘要、列定义、预览、警告和结果 Manifest 引用。自然语言摘要不能改变底层结果。 |
| **Lineage（血缘）** | 记录结果从哪些 Entity、Field、Metric 和 SQG Node 派生而来，回答“这个数字从哪里来”。 |
| **Event sourcing（事件记录）** | 以只追加事件记录状态变化，再投影出当前 Run State。v0 先定义事件和状态契约，不承诺完整事件溯源实现。 |
| **SSE（Server-Sent Events）** | 服务端通过单个 HTTP 连接持续向浏览器推送事件，适合 Run 时间线。它是单向推送，不是双向 WebSocket。 |
| **OpenTelemetry（OTel）** | 开放的 Trace、Metric、Log 采集标准。它定义如何埋点和传输，不等于具体监控后端。 |
| **Trace / Span** | Trace 表示一次跨服务请求，Span 表示其中一个步骤。Run、Stage、Node ID 会作为受控属性关联，但不能把 Prompt 或数据行写入遥测。 |
| **OpenLineage** | 用于表达作业、运行和数据集血缘事件的开放标准。可作为未来导出协议，不取代内部版本化 Lineage Contract。 |

## 安全、身份与平台

| 术语 | 初学者解释 |
| --- | --- |
| **OBO（On-Behalf-Of）** | “代表用户”交换令牌的 OAuth 2.0 流程。中间服务用用户身份换取下游 API 的受限令牌，使审计保留最终用户，而不是共享服务账号。 |
| **Managed Identity（托管身份）** | Azure 为工作负载管理的身份。应用无需保存客户端密钥即可访问 Key Vault、Blob 等服务。 |
| **Secret reference（密钥引用）** | 控制面只保存“去哪里取密钥”的引用，不保存明文凭据。Runtime 在授权范围内按需获取。 |
| **Private Endpoint / Private Link** | 让 Azure PaaS 服务通过虚拟网络私有 IP 访问。设计应预留私网和 DNS 边界，即使 MVP 先采用受限公网入口。 |
| **BFF（Backend for Frontend）** | 专门服务某个前端的后端。未来 ASP.NET Core BFF 负责身份、会话、聚合和 API 契约，不承担大数据执行。 |
| **Tenant（租户）** | 数据、配置、身份和配额的隔离边界。每个请求必须携带可验证的租户上下文。 |
| **RLS / CLS** | Row-Level Security / Column-Level Security，分别限制用户能看到哪些行和列。语义权限不能弱化底层数据源权限。 |
