import type {
  CommittedManifest,
  Diagnostic,
  Lineage,
  LineageSource,
  PlanNode,
  ResultColumn,
  ResultSet,
  Run,
  SqgSummary,
  Stage,
} from '@/domain'

export const RUN_STORAGE_KEY = 'semantic-nexus:runs'
export const RUN_STORAGE_QUARANTINE_KEY = 'semantic-nexus:runs:quarantine'
export const RUN_STORAGE_RECORD_PREFIX = 'semantic-nexus:run:v1:'
export const RUN_STORAGE_CHANGE_EVENT = 'semantic-nexus:run-storage-change'
export const RUN_STORAGE_VERSION = 1

export interface StoredRuns {
  version: number
  runs: Run[]
}

interface StoredRun {
  version: number
  run: Run
}

interface ParsedStoredRuns {
  runs: Run[]
  rejected: boolean
  reason?: string
}

const runStates = ['queued', 'running', 'succeeded', 'empty', 'failed', 'canceled'] as const
const scenarios = ['success', 'empty', 'failure'] as const
const stageKeys = ['initialize', 'compile', 'optimize', 'execute', 'generate'] as const
const stageStates = ['pending', 'running', 'succeeded', 'failed', 'canceled'] as const
const nodeKinds = [
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
const columnFormats = ['text', 'currency', 'percent', 'number', 'date', 'timestamp'] as const
const diagnosticSeverities = ['info', 'warning', 'error'] as const

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)

const isString = (value: unknown): value is string => typeof value === 'string'
const isDateString = (value: unknown): value is string =>
  isString(value) && !Number.isNaN(Date.parse(value))
const isFiniteNumber = (value: unknown): value is number =>
  typeof value === 'number' && Number.isFinite(value)
const isNumber = (value: unknown): value is number =>
  isFiniteNumber(value) && value >= 0
const isStringArray = (value: unknown): value is string[] =>
  Array.isArray(value) && value.every(isString)
const isOptionalString = (value: unknown): value is string | undefined =>
  value === undefined || isDateString(value)

const isEnumValue = <T extends string>(
  value: unknown,
  allowed: readonly T[],
): value is T => isString(value) && allowed.includes(value as T)

const isStage = (value: unknown): value is Stage =>
  isRecord(value)
  && isEnumValue(value.key, stageKeys)
  && isString(value.label)
  && isString(value.description)
  && isEnumValue(value.state, stageStates)
  && (value.durationMs === undefined || isNumber(value.durationMs))

const isSqg = (value: unknown): value is SqgSummary =>
  isRecord(value)
  && isString(value.version)
  && isString(value.intent)
  && isString(value.ontology)
  && isStringArray(value.resolvedMembers)
  && isStringArray(value.metrics)
  && isStringArray(value.dimensions)
  && Array.isArray(value.filters)
  && value.filters.every((filter) =>
    isRecord(filter)
    && isString(filter.field)
    && isString(filter.operator)
    && isString(filter.value))
  && isStringArray(value.policyChecks)

const isPlanNode = (value: unknown): value is PlanNode =>
  isRecord(value)
  && isString(value.id)
  && isEnumValue(value.kind, nodeKinds)
  && isString(value.label)
  && isString(value.plainLanguage)
  && isStringArray(value.inputs)
  && isStringArray(value.outputFields)

const isResultColumn = (value: unknown): value is ResultColumn =>
  isRecord(value)
  && isString(value.key)
  && isString(value.label)
  && isEnumValue(value.format, columnFormats)

const isResultRow = (value: unknown): value is ResultSet['rows'][number] =>
  isRecord(value)
  && Object.values(value).every((cell) =>
    cell === null || typeof cell === 'boolean' || isString(cell) || isFiniteNumber(cell))

const isResult = (value: unknown): value is ResultSet =>
  isRecord(value)
  && Array.isArray(value.columns)
  && value.columns.every(isResultColumn)
  && Array.isArray(value.rows)
  && value.rows.every(isResultRow)
  && isNumber(value.rowCount)
  && value.rowCount >= value.rows.length
  && isString(value.coverage)
  && (value.truncated === undefined || typeof value.truncated === 'boolean')
  && (value.truncated === true
    ? value.rowCount > value.rows.length
    : value.rowCount === value.rows.length)

const isLineageSource = (value: unknown): value is LineageSource =>
  isRecord(value)
  && isString(value.id)
  && isString(value.name)
  && isString(value.kind)
  && isString(value.freshness)
  && isString(value.contribution)

const isLineage = (value: unknown): value is Lineage =>
  isRecord(value)
  && Array.isArray(value.sources)
  && value.sources.every(isLineageSource)
  && isStringArray(value.transformations)

const isDiagnostic = (value: unknown): value is Diagnostic =>
  isRecord(value)
  && isString(value.code)
  && isString(value.title)
  && isString(value.message)
  && isString(value.recovery)
  && isEnumValue(value.severity, diagnosticSeverities)

const isManifest = (value: unknown): value is CommittedManifest =>
  isRecord(value)
  && isString(value.uri)
  && isString(value.format)
  && isString(value.checksum)
  && isDateString(value.committedAt)

export const isRun = (value: unknown): value is Run =>
  isRecord(value)
  && isString(value.id)
  && isString(value.question)
  && isEnumValue(value.state, runStates)
  && isDateString(value.createdAt)
  && isOptionalString(value.completedAt)
  && isNumber(value.elapsedMs)
  && (value.workload === undefined || isString(value.workload))
  && (value.ontology === undefined || isString(value.ontology))
  && (value.compilationMode === undefined || isString(value.compilationMode))
  && isString(value.model)
  && isString(value.executionMode)
  && isString(value.outputMode)
  && isRecord(value.tokens)
  && isNumber(value.tokens.input)
  && isNumber(value.tokens.output)
  && Array.isArray(value.stages)
  && value.stages.length === stageKeys.length
  && value.stages.every(isStage)
  && isSqg(value.sqg)
  && Array.isArray(value.nodes)
  && value.nodes.every(isPlanNode)
  && (value.result === undefined || isResult(value.result))
  && isLineage(value.lineage)
  && Array.isArray(value.diagnostics)
  && value.diagnostics.every(isDiagnostic)
  && (value.manifest === undefined || isManifest(value.manifest))
  && isEnumValue(value.scenario, scenarios)
  && (value.executionLease === undefined
    || (isRecord(value.executionLease)
      && isString(value.executionLease.ownerId)
      && isString(value.executionLease.generation)
      && isDateString(value.executionLease.heartbeatAt)))

export const parseStoredRuns = (raw: string): ParsedStoredRuns => {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return { runs: [], rejected: true, reason: 'invalid-json' }
  }

  if (!isRecord(parsed) || parsed.version !== RUN_STORAGE_VERSION || !Array.isArray(parsed.runs)) {
    return { runs: [], rejected: true, reason: 'schema-mismatch' }
  }

  const runs = parsed.runs.filter(isRun)
  return {
    runs,
    rejected: runs.length !== parsed.runs.length,
    reason: runs.length !== parsed.runs.length ? 'invalid-run-entry' : undefined,
  }
}

export const parseStoredRun = (raw: string): Run | undefined => {
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return undefined
  }
  if (!isRecord(parsed) || parsed.version !== RUN_STORAGE_VERSION || !isRun(parsed.run)) {
    return undefined
  }
  return parsed.run
}

export const serializeStoredRun = (run: Run) =>
  JSON.stringify({ version: RUN_STORAGE_VERSION, run } satisfies StoredRun)

export const runStorageKey = (id: string) =>
  `${RUN_STORAGE_RECORD_PREFIX}${encodeURIComponent(id)}`
