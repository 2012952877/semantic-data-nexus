export type RunState =
  | 'queued'
  | 'running'
  | 'succeeded'
  | 'empty'
  | 'failed'
  | 'canceled'

export type StageKey = 'initialize' | 'compile' | 'optimize' | 'execute' | 'generate'
export type StageState = 'pending' | 'running' | 'succeeded' | 'failed' | 'canceled'

export interface SqgSummary {
  version: '0.1'
  intent: string
  ontology: string
  resolvedMembers: string[]
  metrics: string[]
  dimensions: string[]
  filters: Array<{ field: string; operator: string; value: string }>
  policyChecks: string[]
}

export interface Stage {
  key: StageKey
  label: string
  description: string
  state: StageState
  durationMs?: number
}

export type NodeKind = 'AGGREGATE' | 'PIVOT' | 'DERIVE' | 'PROJECT'

export interface PlanNode {
  id: string
  kind: NodeKind
  label: string
  plainLanguage: string
  inputs: string[]
  outputFields: string[]
}

export interface ResultColumn {
  key: string
  label: string
  format: 'text' | 'currency' | 'percent' | 'number'
}

export interface ResultSet {
  columns: ResultColumn[]
  rows: Array<Record<string, string | number>>
  rowCount: number
  coverage: string
}

export interface LineageSource {
  id: string
  name: string
  kind: string
  freshness: string
  contribution: string
}

export interface Lineage {
  sources: LineageSource[]
  transformations: string[]
}

export interface Diagnostic {
  code: string
  title: string
  message: string
  recovery: string
  severity: 'info' | 'warning' | 'error'
}

export interface CommittedManifest {
  uri: string
  format: string
  checksum: string
  committedAt: string
}

export interface Run {
  id: string
  question: string
  state: RunState
  createdAt: string
  completedAt?: string
  elapsedMs: number
  model: string
  executionMode: string
  outputMode: string
  tokens: { input: number; output: number }
  stages: Stage[]
  sqg: SqgSummary
  nodes: PlanNode[]
  result?: ResultSet
  lineage: Lineage
  diagnostics: Diagnostic[]
  manifest?: CommittedManifest
  scenario: MockScenario
  executionLease?: {
    ownerId: string
    heartbeatAt: string
  }
}

export interface OntologyField {
  name: string
  type: string
  description: string
}

export interface OntologyEntity {
  name: string
  label: string
  description: string
  fields: OntologyField[]
}

export interface OntologyMetric {
  name: string
  label: string
  expression: string
  description: string
}

export interface OntologyRelation {
  from: string
  to: string
  cardinality: string
  description: string
}

export interface Ontology {
  name: string
  version: string
  entities: OntologyEntity[]
  metrics: OntologyMetric[]
  relations: OntologyRelation[]
  queryPolicy: {
    allowedDimensions: string[]
    maxLookbackMonths: number
    description: string
  }
}

export type MockScenario = 'success' | 'empty' | 'failure'

export interface AskRequest {
  question: string
  scenario: MockScenario
  model: string
  executionMode: string
  outputMode: string
}

export interface ComponentStatus {
  name: string
  provider: string
  status: 'healthy' | 'degraded'
  detail: string
}
