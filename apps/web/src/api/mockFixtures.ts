import type {
  AskRequest,
  ComponentStatus,
  Lineage,
  Ontology,
  PlanNode,
  ResultSet,
  Run,
  SqgSummary,
  Stage,
} from '@/domain'

const stageDefinitions: Array<Pick<Stage, 'key' | 'label' | 'description'>> = [
  { key: 'initialize', label: '初始化 · Initialize', description: '确认范围与解析成员' },
  { key: 'compile', label: '编译 · Compile', description: '把问题编译为类型化 SQG' },
  { key: 'optimize', label: '优化 · Optimize', description: '应用策略并优化执行图' },
  { key: 'execute', label: '执行 · Execute', description: '在受控引擎中运行' },
  { key: 'generate', label: '生成 · Generate', description: '生成结果并提交清单' },
]

export const createStages = (): Stage[] =>
  stageDefinitions.map((stage) => ({ ...stage, state: 'pending' }))

export const nodes: PlanNode[] = [
  {
    id: 'node-aggregate',
    kind: 'AGGREGATE',
    label: '按区域汇总',
    plainLanguage: '把每个区域的订单收入和目标值分别加总。',
    inputs: ['sales'],
    outputFields: ['region', 'revenue', 'target'],
  },
  {
    id: 'node-pivot',
    kind: 'PIVOT',
    label: '按期间对齐',
    plainLanguage: '把本期与去年同期放到同一行，便于比较。',
    inputs: ['node-aggregate'],
    outputFields: ['region', 'revenue', 'revenue_last_year', 'target'],
  },
  {
    id: 'node-derive',
    kind: 'DERIVE',
    label: '计算达成率与同比',
    plainLanguage: '基于已聚合的数据计算目标达成率和同比变化。',
    inputs: ['node-pivot'],
    outputFields: ['attainment', 'year_over_year'],
  },
  {
    id: 'node-project',
    kind: 'PROJECT',
    label: '输出业务字段',
    plainLanguage: '只保留最终表格需要的字段，并应用显示格式。',
    inputs: ['node-derive'],
    outputFields: ['region', 'revenue', 'attainment', 'year_over_year'],
  },
]

export const successResult: ResultSet = {
  columns: [
    { key: 'region', label: '区域', format: 'text' },
    { key: 'revenue', label: '净销售额', format: 'currency' },
    { key: 'attainment', label: '目标达成率', format: 'percent' },
    { key: 'year_over_year', label: '同比', format: 'percent' },
  ],
  rows: [
    { region: '华东', revenue: 4286000, attainment: 1.08, year_over_year: 0.124 },
    { region: '华南', revenue: 3512000, attainment: 0.96, year_over_year: 0.071 },
    { region: '华北', revenue: 3189000, attainment: 1.02, year_over_year: 0.088 },
    { region: '西部', revenue: 2248000, attainment: 0.91, year_over_year: -0.018 },
  ],
  rowCount: 4,
  coverage: '2025-04-01 至 2025-06-30，已覆盖全部四个销售区域。',
}

export const emptyResult: ResultSet = {
  columns: successResult.columns,
  rows: [],
  rowCount: 0,
  coverage: '所选期间 2022-01-01 至 2022-03-31 早于当前合成数据覆盖范围。',
}

export const lineage: Lineage = {
  sources: [
    {
      id: 'source-sales',
      name: 'regional_sales',
      kind: '合成事实表',
      freshness: '每日 06:00 刷新',
      contribution: '订单净额、区域与期间',
    },
    {
      id: 'source-targets',
      name: 'regional_targets',
      kind: '合成目标表',
      freshness: '每月首日刷新',
      contribution: '区域季度目标',
    },
  ],
  transformations: ['区域编码标准化', '退货净额扣减', '本期/同期对齐', '四舍五入至展示精度'],
}

export const createSqg = (request: AskRequest): SqgSummary => ({
  version: '0.1',
  intent: request.question,
  ontology: 'regional-sales@1.4',
  resolvedMembers: ['sales.region', 'sales.net_revenue', 'targets.target_amount'],
  metrics: ['net_revenue', 'target_attainment', 'year_over_year'],
  dimensions: ['sales.region'],
  filters: [
    {
      field: 'targets.quarter',
      operator: 'equals',
      value: request.scenario === 'empty'
        ? '2022-Q1'
        : request.scenario === 'failure'
          ? '2025-Q3'
          : '2025-Q2',
    },
  ],
  policyChecks: ['仅允许已发布指标', '回溯范围小于 36 个月', '结果不包含个人信息'],
})

export const createSeedRun = (
  id: string,
  question: string,
  state: Run['state'],
  minutesAgo: number,
): Run => {
  const request: AskRequest = {
    question,
    scenario: state === 'empty' ? 'empty' : 'success',
    model: 'Nexus Planner Small',
    executionMode: '受控执行',
    outputMode: '表格',
  }
  const createdAt = new Date(Date.now() - minutesAgo * 60_000)
  const result = state === 'empty' ? emptyResult : successResult
  return {
    id,
    question,
    state,
    createdAt: createdAt.toISOString(),
    completedAt: new Date(createdAt.getTime() + 1840).toISOString(),
    elapsedMs: 1840,
    model: request.model,
    executionMode: request.executionMode,
    outputMode: request.outputMode,
    tokens: { input: 386, output: 214 },
    stages: createStages().map((stage, index) => ({
      ...stage,
      state: 'succeeded',
      durationMs: [180, 360, 250, 740, 310][index],
    })),
    sqg: createSqg(request),
    nodes,
    result,
    lineage,
    diagnostics: state === 'empty'
      ? [{
          code: 'DATA_COVERAGE_GAP',
          title: '该期间没有可用数据',
          message: result.coverage,
          recovery: '将期间调整为 2024 年之后再试。',
          severity: 'info',
        }]
      : [],
    manifest: {
      uri: `mock://result-store/${id}/manifest.json`,
      format: 'Arrow + JSON manifest',
      checksum: 'sha256:7ba2…c14e',
      committedAt: new Date(createdAt.getTime() + 1840).toISOString(),
    },
    scenario: request.scenario,
  }
}

export const ontology: Ontology = {
  name: '区域销售语义模型',
  version: '1.4.0',
  entities: [
    {
      name: 'sales',
      label: '销售事实',
      description: '一笔已确认订单的业务记录，是金额与数量指标的来源。',
      fields: [
        { name: 'order_date', type: 'date', description: '订单确认日期，用于按日、月、季度筛选。' },
        { name: 'region', type: 'dimension', description: '销售负责区域，不包含客户地址。' },
        { name: 'product_category', type: 'dimension', description: '合成商品的业务分类。' },
        { name: 'net_revenue', type: 'measure', description: '扣除退货与折让后的合成销售额。' },
      ],
    },
    {
      name: 'targets',
      label: '区域目标',
      description: '每个区域按季度设定的合成目标，用于计算达成率。',
      fields: [
        { name: 'region', type: 'dimension', description: '目标对应的销售区域。' },
        { name: 'quarter', type: 'period', description: '目标所属季度。' },
        { name: 'target_amount', type: 'measure', description: '该区域的季度销售目标。' },
      ],
    },
  ],
  metrics: [
    { name: 'net_revenue', label: '净销售额', expression: 'SUM(sales.net_revenue)', description: '把筛选范围内的净销售额相加。' },
    { name: 'target_attainment', label: '目标达成率', expression: 'net_revenue / targets.target_amount', description: '实际净销售额占目标金额的比例。' },
    { name: 'year_over_year', label: '同比增长', expression: '(current - prior) / prior', description: '与去年同一期间相比的增减幅度。' },
  ],
  relations: [
    { from: 'sales.region', to: 'targets.region', cardinality: '多对一', description: '多笔销售记录对应一个区域目标。' },
  ],
  queryPolicy: {
    allowedDimensions: ['sales.region', 'targets.quarter', 'sales.product_category'],
    maxLookbackMonths: 36,
    description: '只允许已发布指标和非敏感维度；单次查询最多回溯 36 个月。',
  },
}

export const componentStatus: ComponentStatus[] = [
  { name: '语义编译器', provider: 'Nexus SQG Compiler', status: 'healthy', detail: '类型契约 v0.1 已加载' },
  { name: '执行引擎', provider: 'DuckDB · 开源', status: 'healthy', detail: 'Mock 隔离模式' },
  { name: '结果存储', provider: 'Azure Blob Storage', status: 'healthy', detail: '只读模拟连接' },
  { name: '成员解析器', provider: 'Azure AI Search', status: 'healthy', detail: '合成索引 regional-sales' },
  { name: '模型提供方', provider: 'Azure OpenAI', status: 'degraded', detail: 'Mock 固定响应，不发出网络请求' },
]
