import type { SemanticNexusClient } from './semanticNexusClient'
import type {
  AskRequest,
  Diagnostic,
  Lineage,
  MockScenario,
  ResultColumn,
  ResultSet,
  Run,
  RunState,
  SqgSummary,
  Stage,
  StageKey,
  StageState,
} from '@/domain'

type Fetch = typeof fetch
export type NexusTokenProvider = (signal: AbortSignal) => Promise<string | undefined>

type BffCompilationMode =
  | 'regional_quarterly_profit'
  | 'monthly_regional_comparison'
type BffExecutionMode = 'thread'
type BffOutputMode = 'normal' | 'stream'
type BffRunState =
  | 'StartPending'
  | 'DispatchUnknown'
  | 'Queued'
  | 'Starting'
  | 'Running'
  | 'CancelRequested'
  | 'Cancelled'
  | 'Succeeded'
  | 'Failed'
type BffCancellationDelivery = 'NotRequested' | 'Pending' | 'Delivered'
type BffOperatorKind =
  | 'SOURCE'
  | 'SELECT'
  | 'FILTER'
  | 'AGGREGATE'
  | 'PIVOT'
  | 'DERIVE'
  | 'PROJECT'
  | 'SORT'
  | 'LIMIT'
  | 'JOIN'
type BffScalarType =
  | 'string'
  | 'integer'
  | 'float'
  | 'decimal'
  | 'boolean'
  | 'date'
  | 'timestamp'
type BffColumnFormat = ResultColumn['format']
type BffResultStorage = 'inline' | 'parquet'
type BffLineageNodeKind = 'logical' | 'physical' | 'source' | 'result'
type BffLineageRelation = 'realized_as' | 'reads_from' | 'depends_on' | 'produces'
type BffDiagnosticSeverity = Diagnostic['severity']
type BffDiagnosticScope = 'run' | 'stage' | 'node'
type BffScalar = string | number | boolean | null

interface BffNodeSummary {
  nodeId: string
  kind: string
  state: BffRunState
  startedAt: string | null
  completedAt: string | null
  duration?: string | null
}

interface BffStageSummary {
  stageId: string
  name: string
  state: BffRunState
  startedAt: string | null
  completedAt: string | null
  duration?: string | null
  nodes: BffNodeSummary[]
}

interface BffSummaryDiagnostic {
  code: string
  message: string
  stage: string | null
  occurredAt: string
}

interface BffRunSummary {
  id: string
  clientRequestId: string
  workload: string
  question: string
  evaluationClock: string
  evaluationTimezone: string
  compilationMode: BffCompilationMode
  executionMode: BffExecutionMode
  outputMode: BffOutputMode
  createdBy: string
  state: BffRunState
  cancellationDelivery: BffCancellationDelivery
  cancellationGeneration: number
  createdAt: string
  updatedAt: string
  startedAt: string | null
  completedAt: string | null
  duration?: string | null
  version: number
  stages: BffStageSummary[]
  tokenUsage: {
    inputTokens: number
    outputTokens: number
    totalTokens: number
  }
  diagnostics: BffSummaryDiagnostic[]
}

interface BffSqgSummary {
  version: string
  intent: string
  ontology: string
  resolvedMembers: string[]
  metrics: string[]
  dimensions: string[]
  filters: Array<{ field: string; operator: string; value: string }>
  policyChecks: string[]
}

interface BffPhysicalNode {
  id: string
  kind: BffOperatorKind
  label: string
  plainLanguage: string
  inputs: string[]
  outputFields: string[]
}

interface BffResultColumn {
  key: string
  label: string
  dataType: BffScalarType
  format: BffColumnFormat
  nullable: boolean
}

interface BffResultSet {
  columns: BffResultColumn[]
  rows: BffScalar[][]
  rowCount: number
  truncated: boolean
}

interface BffCommittedManifest {
  resultId: string
  runId: string
  nodeId: string
  storage: BffResultStorage
  uri: string
  rowCount: number
  byteCount: number
  checksum: string
  committedAt: string
}

interface BffLineageParameter {
  name: string
  dataType: BffScalarType
}

interface BffLineageNode {
  id: string
  kind: BffLineageNodeKind
  operation: string | null
  sourceAlias: string | null
  sourceType: string | null
  resultId: string | null
  parameters: BffLineageParameter[]
}

interface BffLineageEdge {
  source: string
  target: string
  relation: BffLineageRelation
}

interface BffLineage {
  version: string
  runId: string
  nodes: BffLineageNode[]
  edges: BffLineageEdge[]
}

interface BffDetailDiagnostic {
  sequence: number
  runId: string
  scope: BffDiagnosticScope
  scopeId: string
  code: string
  title: string
  message: string
  recovery: string
  severity: BffDiagnosticSeverity
  occurredAt: string
}

interface BffRunDetail {
  runId: string
  question: string
  sqg: BffSqgSummary
  physicalNodes: BffPhysicalNode[]
  result: BffResultSet | null
  manifest: BffCommittedManifest | null
  lineage: BffLineage
  diagnostics: BffDetailDiagnostic[]
}

interface BffRunList {
  items: BffRunSummary[]
  count: number
}

interface ProblemDetails {
  title: string
  detail?: string
  code?: string
  status?: number
}

interface PendingCreateAttempt {
  requestKey: string
  payload: {
    clientRequestId: string
    workload: string
    question: string
    evaluationClock: string
    evaluationTimezone: string
    compilationMode: BffCompilationMode
    executionMode: BffExecutionMode
    outputMode: BffOutputMode
  }
  runId?: string
}

export interface HttpSemanticNexusClientOptions {
  baseUrl: string
  tokenProvider?: NexusTokenProvider
  localDevelopment?: {
    subject?: string
    roles?: string
  }
  fetch?: Fetch
  requestTimeoutMs?: number
  polling?: {
    initialDelayMs?: number
    maximumDelayMs?: number
    deadlineMs?: number
  }
  defaults?: {
    workload?: string
    compilationMode?: BffCompilationMode
    evaluationTimezone?: string
  }
  now?: () => Date
  requestId?: () => string
}

export type NexusClientErrorCode =
  | 'configuration'
  | 'request'
  | 'network'
  | 'http'
  | 'invalid-response'
  | 'timeout'

export class NexusClientError extends Error {
  constructor(
    readonly code: NexusClientErrorCode,
    message: string,
    readonly status?: number,
    readonly clientCause?: unknown,
  ) {
    super(message)
    this.name = 'NexusClientError'
  }
}

const runStates = [
  'StartPending',
  'DispatchUnknown',
  'Queued',
  'Starting',
  'Running',
  'CancelRequested',
  'Cancelled',
  'Succeeded',
  'Failed',
] as const
const cancellationDeliveryStates = ['NotRequested', 'Pending', 'Delivered'] as const
const compilationModes = [
  'regional_quarterly_profit',
  'monthly_regional_comparison',
] as const
const executionModes = ['thread'] as const
const outputModes = ['normal', 'stream'] as const
const operatorKinds = [
  'SOURCE',
  'SELECT',
  'FILTER',
  'AGGREGATE',
  'PIVOT',
  'DERIVE',
  'PROJECT',
  'SORT',
  'LIMIT',
  'JOIN',
] as const
const scalarTypes = [
  'string',
  'integer',
  'float',
  'decimal',
  'boolean',
  'date',
  'timestamp',
] as const
const columnFormats = ['text', 'currency', 'percent', 'number', 'date', 'timestamp'] as const
const resultStorageKinds = ['inline', 'parquet'] as const
const lineageNodeKinds = ['logical', 'physical', 'source', 'result'] as const
const lineageRelations = ['realized_as', 'reads_from', 'depends_on', 'produces'] as const
const diagnosticSeverities = ['info', 'warning', 'error'] as const
const diagnosticScopes = ['run', 'stage', 'node'] as const
const terminalStates = new Set<BffRunState>(['Cancelled', 'Succeeded', 'Failed'])
const runIdPattern = /^run_[0-9a-f]{32}$/
const sensitiveHeaderPattern = /authorization|cookie|credential|secret|token|api[-_]?key/i

const stageDefinitions: Array<Pick<Stage, 'key' | 'label' | 'description'>> = [
  { key: 'initialize', label: '初始化 · Initialize', description: '确认范围与解析成员' },
  { key: 'compile', label: '编译 · Compile', description: '把问题编译为类型化 SQG' },
  { key: 'optimize', label: '优化 · Optimize', description: '应用策略并优化执行图' },
  { key: 'execute', label: '执行 · Execute', description: '在受控引擎中运行' },
  { key: 'generate', label: '生成 · Generate', description: '生成结果并提交清单' },
]

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
const isString = (value: unknown): value is string => typeof value === 'string'
const isText = (value: unknown, maximum = 4_000): value is string =>
  isString(value)
  && value.trim().length > 0
  && [...value].length <= maximum
  && !/\p{C}/u.test(value)
const isLabel = (value: unknown): value is string => isText(value, 128)
const isOptionalLabel = (value: unknown): value is string | null =>
  value === null || isLabel(value)
const isFiniteNonNegative = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= 0
const isInteger = (value: unknown): value is number =>
  isFiniteNonNegative(value) && Number.isSafeInteger(value)
const isStringArray = (value: unknown, maximum = 100): value is string[] =>
  Array.isArray(value)
  && value.length <= maximum
  && value.every(isLabel)
const isEnum = <T extends string>(value: unknown, allowed: readonly T[]): value is T =>
  isString(value) && allowed.includes(value as T)
const isIsoWithOffset = (value: unknown): value is string =>
  isString(value)
  && /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  && !Number.isNaN(Date.parse(value))
const isNullableIsoWithOffset = (value: unknown): value is string | null =>
  value === null || isIsoWithOffset(value)
const isOptionalDuration = (value: unknown): value is string | null | undefined =>
  value === undefined || value === null || (isString(value) && value.length <= 64)
const isRunId = (value: unknown): value is string =>
  isString(value) && runIdPattern.test(value)
const isIsoDate = (value: unknown): value is string => {
  if (!isString(value) || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  if (year === undefined || month === undefined || day === undefined) return false
  const parsed = new Date(Date.UTC(year, month - 1, day))
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day
}

const maximumDecimalMagnitude = `1${'0'.repeat(28)}`
const maximumNumberMagnitude = 1e28
const isCanonicalDecimal = (value: unknown): value is string => {
  if (!isString(value) || !/^-?(?:0|[1-9][0-9]*)(?:\.[0-9]{1,28})?$/.test(value)) {
    return false
  }
  const negative = value.startsWith('-')
  const unsigned = negative ? value.slice(1) : value
  const [integer = '', fraction = ''] = unsigned.split('.')
  const coefficient = `${integer}${fraction}`.replace(/^0+/, '')
  if (coefficient.length > 29 || (negative && coefficient.length === 0)) return false
  if (integer.length > maximumDecimalMagnitude.length
    || (integer.length === maximumDecimalMagnitude.length
      && integer > maximumDecimalMagnitude)) return false
  return true
}

const isNodeSummary = (value: unknown): value is BffNodeSummary =>
  isRecord(value)
  && isLabel(value.nodeId)
  && isLabel(value.kind)
  && isEnum(value.state, runStates)
  && isNullableIsoWithOffset(value.startedAt)
  && isNullableIsoWithOffset(value.completedAt)
  && isOptionalDuration(value.duration)

const isStageSummary = (value: unknown): value is BffStageSummary =>
  isRecord(value)
  && isLabel(value.stageId)
  && isText(value.name, 256)
  && isEnum(value.state, runStates)
  && isNullableIsoWithOffset(value.startedAt)
  && isNullableIsoWithOffset(value.completedAt)
  && isOptionalDuration(value.duration)
  && Array.isArray(value.nodes)
  && value.nodes.length <= 1_000
  && value.nodes.every(isNodeSummary)

const isSummaryDiagnostic = (value: unknown): value is BffSummaryDiagnostic =>
  isRecord(value)
  && isLabel(value.code)
  && isText(value.message, 1_000)
  && isOptionalLabel(value.stage)
  && isIsoWithOffset(value.occurredAt)

const isTokenUsage = (value: unknown): value is BffRunSummary['tokenUsage'] =>
  isRecord(value)
  && isInteger(value.inputTokens)
  && isInteger(value.outputTokens)
  && isInteger(value.totalTokens)
  && value.totalTokens === value.inputTokens + value.outputTokens

const isBffRunSummary = (value: unknown): value is BffRunSummary =>
  isRecord(value)
  && isRunId(value.id)
  && isLabel(value.clientRequestId)
  && isLabel(value.workload)
  && isText(value.question)
  && isIsoWithOffset(value.evaluationClock)
  && isLabel(value.evaluationTimezone)
  && isEnum(value.compilationMode, compilationModes)
  && isEnum(value.executionMode, executionModes)
  && isEnum(value.outputMode, outputModes)
  && isLabel(value.createdBy)
  && isEnum(value.state, runStates)
  && isEnum(value.cancellationDelivery, cancellationDeliveryStates)
  && isInteger(value.cancellationGeneration)
  && isIsoWithOffset(value.createdAt)
  && isIsoWithOffset(value.updatedAt)
  && isNullableIsoWithOffset(value.startedAt)
  && isNullableIsoWithOffset(value.completedAt)
  && isOptionalDuration(value.duration)
  && isInteger(value.version)
  && Array.isArray(value.stages)
  && value.stages.length <= 100
  && value.stages.every(isStageSummary)
  && isTokenUsage(value.tokenUsage)
  && Array.isArray(value.diagnostics)
  && value.diagnostics.length <= 1_000
  && value.diagnostics.every(isSummaryDiagnostic)

const isSqg = (value: unknown): value is BffSqgSummary =>
  isRecord(value)
  && isLabel(value.version)
  && isText(value.intent, 512)
  && isLabel(value.ontology)
  && isStringArray(value.resolvedMembers)
  && isStringArray(value.metrics)
  && isStringArray(value.dimensions)
  && Array.isArray(value.filters)
  && value.filters.length <= 100
  && value.filters.every((filter) =>
    isRecord(filter)
    && isLabel(filter.field)
    && isLabel(filter.operator)
    && isText(filter.value, 512))
  && isStringArray(value.policyChecks)

const isPhysicalNode = (value: unknown): value is BffPhysicalNode =>
  isRecord(value)
  && isLabel(value.id)
  && isEnum(value.kind, operatorKinds)
  && isText(value.label, 256)
  && isText(value.plainLanguage, 1_000)
  && isStringArray(value.inputs)
  && isStringArray(value.outputFields)

const isResultColumn = (value: unknown): value is BffResultColumn =>
  isRecord(value)
  && isLabel(value.key)
  && isText(value.label, 256)
  && isEnum(value.dataType, scalarTypes)
  && isEnum(value.format, columnFormats)
  && typeof value.nullable === 'boolean'

const isCellForColumn = (value: unknown, column: BffResultColumn) => {
  if (value === null) return column.nullable
  if (column.dataType === 'string') return isString(value) && [...value].length <= 4_000
  if (column.dataType === 'integer') return typeof value === 'number' && Number.isSafeInteger(value)
  if (column.dataType === 'float') {
    return typeof value === 'number'
      && Number.isFinite(value)
      && Math.abs(value) <= maximumNumberMagnitude
      && (!Number.isInteger(value) || Number.isSafeInteger(value))
  }
  if (column.dataType === 'decimal') return isCanonicalDecimal(value)
  if (column.dataType === 'boolean') return typeof value === 'boolean'
  if (column.dataType === 'date') {
    return isIsoDate(value)
  }
  return isIsoWithOffset(value)
}

const isResultSet = (value: unknown): value is BffResultSet => {
  if (!isRecord(value) || !Array.isArray(value.columns) || !Array.isArray(value.rows)) {
    return false
  }
  const columns = value.columns
  const rows = value.rows
  if (columns.length > 100
    || !columns.every(isResultColumn)
    || new Set(columns.map((column) => column.key)).size !== columns.length
    || rows.length > 1_000
    || !isInteger(value.rowCount)
    || value.rowCount > 2_147_483_647
    || typeof value.truncated !== 'boolean'
    || value.rowCount < rows.length
    || value.truncated !== (value.rowCount > rows.length)) return false
  return rows.every((row) =>
    Array.isArray(row)
    && row.length === columns.length
    && row.every((cell, index) => {
      const column = columns[index]
      return column !== undefined && isCellForColumn(cell, column)
    }))
}

const isManifest = (value: unknown): value is BffCommittedManifest =>
  isRecord(value)
  && isLabel(value.resultId)
  && isRunId(value.runId)
  && isLabel(value.nodeId)
  && isEnum(value.storage, resultStorageKinds)
  && isText(value.uri, 2_048)
  && isInteger(value.rowCount)
  && isInteger(value.byteCount)
  && isText(value.checksum, 256)
  && isIsoWithOffset(value.committedAt)

const isLineageParameter = (value: unknown): value is BffLineageParameter =>
  isRecord(value)
  && isLabel(value.name)
  && isEnum(value.dataType, scalarTypes)

const isLineageNode = (value: unknown): value is BffLineageNode =>
  isRecord(value)
  && isLabel(value.id)
  && isEnum(value.kind, lineageNodeKinds)
  && isOptionalLabel(value.operation)
  && isOptionalLabel(value.sourceAlias)
  && isOptionalLabel(value.sourceType)
  && isOptionalLabel(value.resultId)
  && Array.isArray(value.parameters)
  && value.parameters.length <= 100
  && value.parameters.every(isLineageParameter)

const isLineageEdge = (value: unknown): value is BffLineageEdge =>
  isRecord(value)
  && isLabel(value.source)
  && isLabel(value.target)
  && isEnum(value.relation, lineageRelations)

const isLineage = (value: unknown): value is BffLineage => {
  if (!isRecord(value)
    || !isLabel(value.version)
    || !isRunId(value.runId)
    || !Array.isArray(value.nodes)
    || value.nodes.length > 5_000
    || !value.nodes.every(isLineageNode)
    || new Set(value.nodes.map((node) => node.id)).size !== value.nodes.length
    || !Array.isArray(value.edges)
    || value.edges.length > 10_000
    || !value.edges.every(isLineageEdge)) return false
  const ids = new Set(value.nodes.map((node) => node.id))
  return value.edges.every((edge) => ids.has(edge.source) && ids.has(edge.target))
}

const isDetailDiagnostic = (value: unknown): value is BffDetailDiagnostic =>
  isRecord(value)
  && isInteger(value.sequence)
  && isRunId(value.runId)
  && isEnum(value.scope, diagnosticScopes)
  && isLabel(value.scopeId)
  && isLabel(value.code)
  && isText(value.title, 256)
  && isText(value.message, 1_000)
  && isText(value.recovery, 1_000)
  && isEnum(value.severity, diagnosticSeverities)
  && isIsoWithOffset(value.occurredAt)

const isBffRunDetail = (value: unknown): value is BffRunDetail => {
  if (!isRecord(value)
    || !isRunId(value.runId)
    || !isText(value.question)
    || !isSqg(value.sqg)
    || !Array.isArray(value.physicalNodes)
    || value.physicalNodes.length > 1_000
    || !value.physicalNodes.every(isPhysicalNode)
    || new Set(value.physicalNodes.map((node) => node.id)).size !== value.physicalNodes.length
    || !(value.result === null || isResultSet(value.result))
    || !(value.manifest === null || isManifest(value.manifest))
    || !isLineage(value.lineage)
    || !Array.isArray(value.diagnostics)
    || value.diagnostics.length > 1_000
    || !value.diagnostics.every(isDetailDiagnostic)) return false
  let sequence = -1
  return value.diagnostics.every((diagnostic) => {
    if (diagnostic.sequence <= sequence) return false
    sequence = diagnostic.sequence
    return true
  })
}

const isBffRunList = (value: unknown): value is BffRunList =>
  isRecord(value)
  && Array.isArray(value.items)
  && value.items.length <= 100
  && value.items.every(isBffRunSummary)
  && isInteger(value.count)
  && value.count === value.items.length

const isProblemDetails = (value: unknown): value is ProblemDetails =>
  isRecord(value)
  && isText(value.title, 256)
  && (value.detail === undefined || isString(value.detail))
  && (value.code === undefined || isLabel(value.code))
  && (value.status === undefined || isInteger(value.status))

const clone = <T>(value: T): T => structuredClone(value)

const mapRunState = (state: BffRunState): RunState => {
  if (state === 'Succeeded') return 'succeeded'
  if (state === 'Failed') return 'failed'
  if (state === 'Cancelled') return 'canceled'
  if (state === 'Queued' || state === 'StartPending' || state === 'DispatchUnknown') {
    return 'queued'
  }
  return 'running'
}

const mapStageState = (state: BffRunState): StageState => {
  if (state === 'Succeeded') return 'succeeded'
  if (state === 'Failed') return 'failed'
  if (state === 'Cancelled') return 'canceled'
  if (state === 'Queued' || state === 'StartPending' || state === 'DispatchUnknown') {
    return 'pending'
  }
  return 'running'
}

const mapScenario = (state: BffRunState): MockScenario =>
  state === 'Failed' ? 'failure' : 'success'

const millisecondsBetween = (start: string | null, end: string | null) =>
  start && end ? Math.max(0, Date.parse(end) - Date.parse(start)) : undefined

const stageKey = (stage: BffStageSummary, index: number): StageKey => {
  const value = `${stage.stageId} ${stage.name}`.toLowerCase()
  if (/initial|resolve/.test(value)) return 'initialize'
  if (/compil/.test(value)) return 'compile'
  if (/optimi/.test(value)) return 'optimize'
  if (/execut|run/.test(value)) return 'execute'
  if (/generat|final|commit/.test(value)) return 'generate'
  return stageDefinitions[index]?.key ?? 'generate'
}

const mapStages = (stages: BffStageSummary[]): Stage[] => {
  const mapped = new Map<StageKey, BffStageSummary>()
  stages.slice(0, stageDefinitions.length).forEach((stage, index) => {
    const key = stageKey(stage, index)
    if (!mapped.has(key)) mapped.set(key, stage)
  })
  return stageDefinitions.map((definition) => {
    const source = mapped.get(definition.key)
    if (!source) return { ...definition, state: 'pending' }
    const durationMs = millisecondsBetween(source.startedAt, source.completedAt)
    return {
      ...definition,
      state: mapStageState(source.state),
      ...(durationMs === undefined ? {} : { durationMs }),
    }
  })
}

const emptySqg = (summary: BffRunSummary): SqgSummary => ({
  version: 'unavailable',
  intent: summary.question,
  ontology: summary.workload,
  resolvedMembers: [],
  metrics: [],
  dimensions: [],
  filters: [],
  policyChecks: [],
})

const mapSummaryDiagnostics = (summary: BffRunSummary): Diagnostic[] =>
  summary.diagnostics.map((diagnostic) => ({
    code: diagnostic.code,
    title: diagnostic.stage
      ? `${diagnostic.stage} · ${diagnostic.code}`
      : diagnostic.code,
    message: diagnostic.message,
    recovery: '查看运行详情并根据诊断重新发起请求。',
    severity: summary.state === 'Failed' ? 'error' : 'warning',
  }))

const mapSummary = (summary: BffRunSummary): Run => {
  const elapsedMs = millisecondsBetween(
    summary.startedAt ?? summary.createdAt,
    summary.completedAt ?? summary.updatedAt,
  ) ?? 0
  return {
    id: summary.id,
    question: summary.question,
    state: mapRunState(summary.state),
    createdAt: summary.createdAt,
    ...(summary.completedAt === null ? {} : { completedAt: summary.completedAt }),
    elapsedMs,
    model: summary.compilationMode,
    executionMode: summary.executionMode,
    outputMode: summary.outputMode,
    tokens: {
      input: summary.tokenUsage.inputTokens,
      output: summary.tokenUsage.outputTokens,
    },
    stages: mapStages(summary.stages),
    sqg: emptySqg(summary),
    nodes: [],
    lineage: { sources: [], transformations: [] },
    diagnostics: mapSummaryDiagnostics(summary),
    scenario: mapScenario(summary.state),
  }
}

const mapResult = (result: BffResultSet): ResultSet => ({
  columns: result.columns.map((column) => ({
    key: column.key,
    label: column.label,
    format: column.format,
  })),
  rows: result.rows.map((row) => Object.fromEntries(
    result.columns.map((column, index) => [column.key, row[index] ?? null]),
  )),
  rowCount: result.rowCount,
  coverage: result.truncated
    ? `显示前 ${result.rows.length} 行，共 ${result.rowCount} 行。`
    : `已返回全部 ${result.rowCount} 行。`,
  truncated: result.truncated,
})

const mapLineage = (lineage: BffLineage): Lineage => ({
  sources: lineage.nodes
    .filter((node) => node.kind === 'source')
    .map((node) => ({
      id: node.id,
      name: node.sourceAlias ?? node.id,
      kind: node.sourceType ?? 'source',
      freshness: `血缘契约 ${lineage.version}`,
      contribution: node.operation ?? '为本次运行提供受控数据。',
    })),
  transformations: [
    ...lineage.nodes.flatMap((node) => node.operation ? [node.operation] : []),
    ...lineage.edges.map((edge) =>
      `${edge.source} ${edge.relation.replace(/_/g, ' ')} ${edge.target}`),
  ],
})

const assertDetailCoherent = (summary: BffRunSummary, detail: BffRunDetail) => {
  const resultAndManifestMatch = (detail.result === null) === (detail.manifest === null)
  if (detail.runId !== summary.id
    || detail.question !== summary.question
    || detail.lineage.runId !== summary.id
    || detail.diagnostics.some((diagnostic) => diagnostic.runId !== summary.id)
    || (detail.manifest !== null && detail.manifest.runId !== summary.id)
    || !resultAndManifestMatch
    || (detail.result && detail.manifest
      && detail.result.rowCount !== detail.manifest.rowCount)) {
    throw new NexusClientError(
      'invalid-response',
      'The BFF returned an internally inconsistent run detail response.',
    )
  }
}

const mapDetail = (summary: BffRunSummary, detail: BffRunDetail): Run => {
  assertDetailCoherent(summary, detail)
  const isEmpty = summary.state === 'Succeeded' && detail.result?.rowCount === 0
  return {
    ...mapSummary(summary),
    ...(isEmpty ? { state: 'empty' as const, scenario: 'empty' as const } : {}),
    sqg: clone(detail.sqg),
    nodes: clone(detail.physicalNodes),
    ...(detail.result === null ? {} : { result: mapResult(detail.result) }),
    ...(detail.manifest === null
      ? {}
      : {
          manifest: {
            uri: detail.manifest.uri,
            format: detail.manifest.storage,
            checksum: detail.manifest.checksum,
            committedAt: detail.manifest.committedAt,
          },
        }),
    lineage: mapLineage(detail.lineage),
    diagnostics: detail.diagnostics.map((diagnostic) => ({
      code: diagnostic.code,
      title: diagnostic.title,
      message: diagnostic.message,
      recovery: diagnostic.recovery,
      severity: diagnostic.severity,
    })),
  }
}

const normalizeBaseUrl = (value: string) => {
  let url: URL
  const browserOrigin = typeof window === 'undefined' ? undefined : window.location.origin
  try {
    url = browserOrigin ? new URL(value, browserOrigin) : new URL(value)
  } catch (error) {
    throw new NexusClientError(
      'configuration',
      'VITE_NEXUS_BASE_URL must be an absolute HTTP or HTTPS URL.',
      undefined,
      error,
    )
  }
  if (!['http:', 'https:'].includes(url.protocol)
    || url.username
    || url.password
    || url.search
    || url.hash) {
    throw new NexusClientError(
      'configuration',
      'VITE_NEXUS_BASE_URL must be an absolute HTTP or HTTPS URL without credentials, query, or fragment.',
    )
  }
  const isLoopback = url.hostname === 'localhost'
    || url.hostname === '127.0.0.1'
    || url.hostname === '[::1]'
  if (url.protocol === 'http:' && !isLoopback) {
    throw new NexusClientError(
      'configuration',
      'Plain HTTP is only allowed for a loopback BFF URL; remote BFF URLs must use HTTPS.',
    )
  }
  if (browserOrigin && url.origin !== browserOrigin) {
    throw new NexusClientError(
      'configuration',
      'Browser HTTP mode requires a same-origin BFF reverse proxy because the BFF does not enable CORS.',
    )
  }
  return url.toString().replace(/\/$/, '')
}

const validateLocalDevelopment = (
  baseUrl: string,
  localDevelopment: HttpSemanticNexusClientOptions['localDevelopment'],
) => {
  if (!localDevelopment?.subject && !localDevelopment?.roles) return {}
  const hostname = new URL(baseUrl).hostname
  if (hostname !== 'localhost' && hostname !== '127.0.0.1' && hostname !== '[::1]') {
    throw new NexusClientError(
      'configuration',
      'Local development identity headers are only allowed for a loopback BFF URL.',
    )
  }
  const entries = [
    ['X-Dev-Subject', localDevelopment.subject],
    ['X-Dev-Roles', localDevelopment.roles],
  ] as const
  return Object.fromEntries(entries.filter(([, value]) => {
    if (value === undefined) return false
    if (!value.trim() || /[\r\n]/.test(value) || sensitiveHeaderPattern.test(value)) {
      throw new NexusClientError(
        'configuration',
        'Local development headers must contain explicit, non-secret single-line values.',
      )
    }
    return true
  })) as Record<string, string>
}

const randomRequestId = () => {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  const bytes = crypto.getRandomValues(new Uint8Array(16))
  return [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('')
}

export class HttpSemanticNexusClient implements SemanticNexusClient {
  readonly mode = 'http' as const
  private readonly baseUrl: string
  private readonly fetch: Fetch
  private readonly tokenProvider?: NexusTokenProvider
  private readonly localDevelopmentHeaders: Record<string, string>
  private readonly requestTimeoutMs: number
  private readonly initialDelayMs: number
  private readonly maximumDelayMs: number
  private readonly deadlineMs: number
  private readonly workload: string
  private readonly compilationMode: BffCompilationMode
  private readonly evaluationTimezone: string
  private readonly now: () => Date
  private readonly requestId: () => string
  private readonly responseDeadlines = new WeakMap<
    Response,
    { deadlineAt: number; controller: AbortController }
  >()
  private readonly snapshots = new Map<string, BffRunSummary>()
  private readonly pendingAttempts = new Map<string, PendingCreateAttempt>()
  private readonly cancellations = new Map<string, Promise<Run>>()

  constructor(options: HttpSemanticNexusClientOptions) {
    this.baseUrl = normalizeBaseUrl(options.baseUrl)
    this.fetch = options.fetch ?? ((input, init) => globalThis.fetch(input, init))
    this.tokenProvider = options.tokenProvider
    this.requestTimeoutMs = options.requestTimeoutMs ?? 10_000
    this.localDevelopmentHeaders = validateLocalDevelopment(
      this.baseUrl,
      options.localDevelopment,
    )
    this.initialDelayMs = options.polling?.initialDelayMs ?? 200
    this.maximumDelayMs = options.polling?.maximumDelayMs ?? 2_000
    this.deadlineMs = options.polling?.deadlineMs ?? 30_000
    this.workload = options.defaults?.workload ?? 'regional-sales'
    this.compilationMode = options.defaults?.compilationMode ?? 'regional_quarterly_profit'
    this.evaluationTimezone = options.defaults?.evaluationTimezone
      ?? Intl.DateTimeFormat().resolvedOptions().timeZone
      ?? 'Etc/UTC'
    this.now = options.now ?? (() => new Date())
    this.requestId = options.requestId ?? randomRequestId
    this.validateOptions()
  }

  async listRuns(): Promise<Run[]> {
    const response = await this.request('/api/v1/runs')
    const payload = await this.validatedJson(response, isBffRunList, 'run list')
    return payload.items.map((summary) => mapSummary(this.acceptSummary(summary)))
  }

  async getRun(id: string): Promise<Run | undefined> {
    const path = `/api/v1/runs/${encodeURIComponent(id)}`
    const summaryResponse = await this.request(path, {}, true)
    if (summaryResponse.status === 404) return undefined
    const summary = this.acceptPathSummary(
      id,
      await this.validatedJson(summaryResponse, isBffRunSummary, 'run summary'),
      'run lookup',
    )
    if (summary.state !== 'Succeeded') return mapSummary(summary)
    const detailResponse = await this.request(`${path}/detail`)
    const detail = await this.validatedJson(detailResponse, isBffRunDetail, 'run detail')
    return mapDetail(summary, detail)
  }

  async startRun(request: AskRequest, onProgress?: (run: Run) => void): Promise<Run> {
    if (!isText(request.question)) {
      throw new NexusClientError(
        'request',
        'Questions must contain 1 to 4,000 visible characters.',
      )
    }
    const requestKey = JSON.stringify({
      question: request.question,
      workload: this.workload,
      compilationMode: this.compilationMode,
      evaluationTimezone: this.evaluationTimezone,
    })
    let attempt = this.pendingAttempts.get(requestKey)
    if (!attempt) {
      attempt = {
        requestKey,
        payload: {
          clientRequestId: this.requestId(),
          workload: this.workload,
          question: request.question,
          evaluationClock: this.now().toISOString(),
          evaluationTimezone: this.evaluationTimezone,
          compilationMode: this.compilationMode,
          executionMode: 'thread' satisfies BffExecutionMode,
          outputMode: 'normal' satisfies BffOutputMode,
        },
      }
      this.pendingAttempts.set(requestKey, attempt)
    }
    let summary: BffRunSummary
    if (attempt.runId) {
      const cached = await this.terminalSnapshotAfterCancellation(attempt.runId)
      if (cached) {
        summary = cached
      } else {
        const resumed = await this.request(
          `/api/v1/runs/${encodeURIComponent(attempt.runId)}/semantic-status`,
        )
        summary = this.acceptPathSummary(
          attempt.runId,
          await this.validatedJson(resumed, isBffRunSummary, 'resumed run'),
          'resumed run',
        )
      }
    } else {
      try {
        const created = await this.request('/api/v1/runs', {
          method: 'POST',
          body: JSON.stringify(attempt.payload),
        })
        summary = this.acceptSummary(
          await this.validatedJson(created, isBffRunSummary, 'created run'),
        )
        attempt.runId = summary.id
      } catch (error) {
        if (this.isDefinitiveCreateFailure(error)) this.clearAttempt(requestKey, attempt)
        throw error
      }
    }
    onProgress?.(mapSummary(summary))

    const deadline = Date.now() + this.deadlineMs
    let delayMs = this.initialDelayMs
    while (!terminalStates.has(summary.state)) {
      const alreadyTerminal = this.terminalSnapshot(summary.id)
      if (alreadyTerminal) {
        summary = alreadyTerminal
        onProgress?.(mapSummary(summary))
        break
      }
      if (Date.now() + delayMs > deadline) {
        const terminal = await this.terminalSnapshotAfterCancellation(summary.id)
        if (terminal) {
          summary = terminal
          onProgress?.(mapSummary(summary))
          break
        }
        throw new NexusClientError(
          'timeout',
          `Run ${summary.id} did not reach a terminal state within ${this.deadlineMs}ms.`,
        )
      }
      await new Promise((resolve) => setTimeout(resolve, delayMs))
      const cached = await this.terminalSnapshotAfterCancellation(summary.id)
      if (cached) {
        summary = cached
      } else {
        try {
          const polled = await this.request(
            `/api/v1/runs/${encodeURIComponent(summary.id)}/semantic-status`,
            {},
            false,
            deadline,
          )
          summary = this.acceptPathSummary(
            summary.id,
            await this.validatedJson(polled, isBffRunSummary, 'polled run'),
            'polled run',
          )
        } catch (error) {
          const terminal = await this.terminalSnapshotAfterCancellation(summary.id)
          if (!terminal) throw error
          summary = terminal
        }
      }
      onProgress?.(mapSummary(summary))
      delayMs = Math.min(this.maximumDelayMs, Math.ceil(delayMs * 1.6))
    }

    if (summary.state === 'Cancelled' || summary.state === 'Failed') {
      this.clearAttempt(requestKey, attempt)
      return mapSummary(summary)
    }
    const detailResponse = await this.request(
      `/api/v1/runs/${encodeURIComponent(summary.id)}/detail`,
      {},
      false,
      deadline,
    )
    const detail = await this.validatedJson(detailResponse, isBffRunDetail, 'run detail')
    const mapped = mapDetail(summary, detail)
    this.clearAttempt(requestKey, attempt)
    onProgress?.(mapped)
    return mapped
  }

  cancelRun(id: string): Promise<Run> {
    const existing = this.cancellations.get(id)
    if (existing) return existing
    const cancellation = this.performCancellation(id)
    this.cancellations.set(id, cancellation)
    void cancellation.finally(() => {
      if (this.cancellations.get(id) === cancellation) this.cancellations.delete(id)
    }).catch(() => undefined)
    return cancellation
  }

  private async performCancellation(id: string): Promise<Run> {
    const response = await this.request(
      `/api/v1/runs/${encodeURIComponent(id)}/cancel`,
      { method: 'POST' },
    )
    const summary = this.acceptPathSummary(
      id,
      await this.validatedJson(response, isBffRunSummary, 'cancelled run'),
      'cancelled run',
    )
    if (summary.state !== 'Succeeded') {
      if (terminalStates.has(summary.state)) this.clearAttemptByRunId(summary.id)
      return mapSummary(summary)
    }
    const detailResponse = await this.request(
      `/api/v1/runs/${encodeURIComponent(id)}/detail`,
    )
    const detail = await this.validatedJson(
      detailResponse,
      isBffRunDetail,
      'cancelled run detail',
    )
    const mapped = mapDetail(summary, detail)
    this.clearAttemptByRunId(summary.id)
    return mapped
  }

  async getOntology() {
    return {
      name: '语义目录不可用 · synthetic placeholder',
      version: 'unavailable',
      entities: [],
      metrics: [],
      relations: [],
      queryPolicy: {
        allowedDimensions: [],
        maxLookbackMonths: 0,
        description: 'HTTP BFF v1 没有提供 ontology 端点；此页不显示 Mock 目录。',
      },
    }
  }

  async getComponentStatus() {
    return [{
      name: '组件健康状态不可用',
      provider: 'unknown · synthetic placeholder',
      status: 'unknown' as const,
      detail: 'HTTP BFF v1 没有提供浏览器可用的健康端点；未探测或推断后端健康度。',
    }]
  }

  private acceptSummary(candidate: BffRunSummary) {
    const current = this.snapshots.get(candidate.id)
    if (current) {
      if (terminalStates.has(current.state)) return current
      if (candidate.version < current.version) return current
      if (candidate.version === current.version
        && this.statePrecedence(candidate.state) < this.statePrecedence(current.state)) {
        return current
      }
    }
    this.snapshots.set(candidate.id, candidate)
    return candidate
  }

  private acceptPathSummary(
    expectedRunId: string,
    candidate: BffRunSummary,
    responseName: string,
  ) {
    if (candidate.id !== expectedRunId) {
      throw new NexusClientError(
        'invalid-response',
        `The BFF returned a ${responseName} for a different run.`,
      )
    }
    return this.acceptSummary(candidate)
  }

  private statePrecedence(state: BffRunState) {
    return runStates.indexOf(state)
  }

  private terminalSnapshot(runId: string) {
    const snapshot = this.snapshots.get(runId)
    return snapshot && terminalStates.has(snapshot.state) ? snapshot : undefined
  }

  private async terminalSnapshotAfterCancellation(runId: string) {
    const current = this.terminalSnapshot(runId)
    if (current) return current
    await this.cancellations.get(runId)?.catch(() => undefined)
    return this.terminalSnapshot(runId)
  }

  private clearAttempt(requestKey: string, attempt: PendingCreateAttempt) {
    if (this.pendingAttempts.get(requestKey) === attempt) {
      this.pendingAttempts.delete(requestKey)
    }
  }

  private clearAttemptByRunId(runId: string) {
    for (const [requestKey, attempt] of this.pendingAttempts) {
      if (attempt.runId === runId) this.clearAttempt(requestKey, attempt)
    }
  }

  private isDefinitiveCreateFailure(error: unknown) {
    return error instanceof NexusClientError
      && (error.code === 'request'
        || error.code === 'configuration'
        || (error.code === 'http' && error.status !== undefined && error.status < 500))
  }

  private validateOptions() {
    const values = [
      ['requestTimeoutMs', this.requestTimeoutMs],
      ['polling.initialDelayMs', this.initialDelayMs],
      ['polling.maximumDelayMs', this.maximumDelayMs],
      ['polling.deadlineMs', this.deadlineMs],
    ] as const
    for (const [name, value] of values) {
      if (!Number.isSafeInteger(value) || value < 1) {
        throw new NexusClientError('configuration', `${name} must be a positive integer.`)
      }
    }
    if (this.maximumDelayMs < this.initialDelayMs) {
      throw new NexusClientError(
        'configuration',
        'polling.maximumDelayMs must be greater than or equal to polling.initialDelayMs.',
      )
    }
    if (!isLabel(this.workload) || !isLabel(this.evaluationTimezone)) {
      throw new NexusClientError(
        'configuration',
        'HTTP client defaults must be non-empty bounded values.',
      )
    }
    try {
      new Intl.DateTimeFormat('en-US', { timeZone: this.evaluationTimezone }).format()
    } catch (error) {
      throw new NexusClientError(
        'configuration',
        'The evaluation timezone must be a valid IANA timezone.',
        undefined,
        error,
      )
    }
  }

  private async request(
    path: string,
    init: RequestInit = {},
    allowNotFound = false,
    deadlineAt = Date.now() + this.requestTimeoutMs,
  ): Promise<Response> {
    const headers = new Headers({
      Accept: 'application/json',
      ...this.localDevelopmentHeaders,
    })
    if (init.body !== undefined) headers.set('Content-Type', 'application/json')
    const controller = new AbortController()
    let token: string | undefined
    try {
      token = this.tokenProvider
        ? await this.beforeDeadline(
            this.tokenProvider(controller.signal),
            deadlineAt,
            () => controller.abort(),
          )
        : undefined
    } catch (error) {
      if (error instanceof NexusClientError) throw error
      throw new NexusClientError(
        'network',
        'The Semantic Nexus access token could not be acquired.',
        undefined,
        error,
      )
    }
    if (token !== undefined) {
      if (!token.trim() || /[\r\n]/.test(token)) {
        throw new NexusClientError(
          'configuration',
          'The injected token provider returned an invalid bearer token.',
        )
      }
      headers.set('Authorization', `Bearer ${token}`)
    }

    let response: Response
    try {
      response = await this.beforeDeadline(
        this.fetch(`${this.baseUrl}${path}`, {
          ...init,
          headers,
          signal: controller.signal,
        }),
        deadlineAt,
        () => controller.abort(),
      )
    } catch (error) {
      if (error instanceof NexusClientError) throw error
      throw new NexusClientError(
        'network',
        'The Semantic Nexus service could not be reached. Check the BFF URL and network.',
        undefined,
        error,
      )
    }
    if (response.ok) {
      this.responseDeadlines.set(response, { deadlineAt, controller })
      return response
    }
    if (allowNotFound && response.status === 404) {
      controller.abort()
      void response.body?.cancel().catch(() => undefined)
      return response
    }
    throw await this.httpError(response, deadlineAt, controller)
  }

  private async validatedJson<T>(
    response: Response,
    validator: (value: unknown) => value is T,
    description: string,
  ): Promise<T> {
    let payload: unknown
    const deadline = this.responseDeadlines.get(response)
    try {
      payload = deadline
        ? await this.beforeDeadline(
            response.json(),
            deadline.deadlineAt,
            () => deadline.controller.abort(),
          )
        : await response.json()
    } catch (error) {
      if (error instanceof NexusClientError) throw error
      throw new NexusClientError(
        'invalid-response',
        `The BFF returned non-JSON content for ${description}.`,
        response.status,
        error,
      )
    } finally {
      this.responseDeadlines.delete(response)
    }
    if (!validator(payload)) {
      throw new NexusClientError(
        'invalid-response',
        `The BFF returned an invalid ${description} response.`,
        response.status,
      )
    }
    return payload
  }

  private async httpError(
    response: Response,
    deadlineAt: number,
    controller: AbortController,
  ) {
    let payload: unknown
    try {
      payload = await this.beforeDeadline(
        response.json(),
        deadlineAt,
        () => controller.abort(),
      )
    } catch (error) {
      if (error instanceof NexusClientError) throw error
      payload = undefined
    }
    if (isProblemDetails(payload)) {
      return new NexusClientError(
        'http',
        payload.detail ? `${payload.title}: ${payload.detail}` : payload.title,
        response.status,
      )
    }
    return new NexusClientError(
      'http',
      `The BFF request failed with HTTP ${response.status}.`,
      response.status,
    )
  }

  private async beforeDeadline<T>(
    operation: Promise<T>,
    deadlineAt: number,
    onTimeout?: () => void,
  ): Promise<T> {
    const remainingMs = deadlineAt - Date.now()
    if (remainingMs <= 0) {
      void operation.catch(() => undefined)
      onTimeout?.()
      throw new NexusClientError('timeout', 'The BFF request deadline was exceeded.')
    }
    return new Promise<T>((resolve, reject) => {
      let settled = false
      const timer = setTimeout(() => {
        if (settled) return
        settled = true
        reject(new NexusClientError('timeout', 'The BFF request deadline was exceeded.'))
        onTimeout?.()
      }, remainingMs)
      operation.then(
        (value) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          resolve(value)
        },
        (error: unknown) => {
          if (settled) return
          settled = true
          clearTimeout(timer)
          reject(error)
        },
      )
    })
  }
}
