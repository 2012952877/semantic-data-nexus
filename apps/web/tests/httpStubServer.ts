import { createServer, type IncomingMessage, type ServerResponse } from 'node:http'
import { readFileSync } from 'node:fs'
import { pathToFileURL } from 'node:url'

type StubState =
  | 'Queued'
  | 'Running'
  | 'CancelRequested'
  | 'Succeeded'
  | 'Failed'
  | 'Cancelled'
type StubOutcome = 'success' | 'empty' | 'failed'

interface StubRun {
  id: string
  request: Record<string, unknown>
  state: StubState
  terminalState: Extract<StubState, 'Succeeded' | 'Failed'>
  outcome: StubOutcome
  polls: number
  version: number
  cancellationRequested?: boolean
  staleAfterCancellationSent?: boolean
}

export interface StubRequest {
  method: string
  path: string
  headers: IncomingMessage['headers']
  body?: unknown
}

const stages = [
  ['initialize', 'Initialize'],
  ['compile', 'Compile'],
  ['optimize', 'Optimize'],
  ['execute', 'Execute'],
  ['generate', 'Generate'],
] as const

const terminal = new Set<StubState>(['Succeeded', 'Failed', 'Cancelled'])
const createdAt = '2026-09-04T05:15:50.492Z'
const completedAt = '2026-09-04T05:15:52.332Z'
const runId = (sequence: number) => `run_${sequence.toString(16).padStart(32, '0')}`
const backendRunDetail = JSON.parse(readFileSync(
  new URL('./fixtures/backend-run-detail.json', import.meta.url),
  'utf8',
)) as { runId: string; question: string }

const stateForStage = (run: StubRun, index: number): StubState => {
  if (run.state === 'Queued') return 'Queued'
  if (run.state === 'Running') return index === 0 ? 'Running' : 'Queued'
  if (run.state === 'CancelRequested') return 'CancelRequested'
  if (run.state === 'Cancelled') return 'Cancelled'
  if (run.state === 'Failed') {
    if (index < 3) return 'Succeeded'
    return index === 3 ? 'Failed' : 'Cancelled'
  }
  return 'Succeeded'
}

const summaryDiagnostics = (run: StubRun) => run.state === 'Failed'
  ? [{
      code: 'STUB_SOURCE_UNAVAILABLE',
      message: 'The controlled synthetic snapshot is unavailable.',
      stage: 'execute',
      occurredAt: completedAt,
    }]
  : []

const summaryFor = (run: StubRun) => ({
  id: run.id,
  clientRequestId: String(run.request.clientRequestId),
  workload: String(run.request.workload),
  question: String(run.request.question),
  evaluationClock: String(run.request.evaluationClock),
  evaluationTimezone: String(run.request.evaluationTimezone),
  compilationMode: String(run.request.compilationMode),
  executionMode: String(run.request.executionMode),
  outputMode: String(run.request.outputMode),
  createdBy: 'playwright-user',
  state: run.state,
  cancellationDelivery: run.state === 'Cancelled'
    ? 'Delivered'
    : run.state === 'CancelRequested' ? 'Pending' : 'NotRequested',
  cancellationGeneration: run.state === 'Cancelled' || run.state === 'CancelRequested' ? 1 : 0,
  createdAt,
  updatedAt: terminal.has(run.state) ? completedAt : createdAt,
  startedAt: run.state === 'Queued' ? null : createdAt,
  completedAt: terminal.has(run.state) ? completedAt : null,
  duration: terminal.has(run.state) ? '00:00:01.8400000' : null,
  version: run.version,
  stages: stages.map(([stageId, name], index) => {
    const state = stateForStage(run, index)
    const done = terminal.has(state)
    return {
      stageId,
      name,
      state,
      startedAt: state === 'Queued' ? null : createdAt,
      completedAt: done ? completedAt : null,
      duration: done ? '00:00:00.3680000' : null,
      nodes: run.id === runId(1) && index === 0
        ? Array.from({ length: 101 }, (_, nodeIndex) => ({
            nodeId: `node-${nodeIndex.toString().padStart(3, '0')}`,
            kind: 'synthetic',
            state: 'Succeeded',
            startedAt: createdAt,
            completedAt,
            duration: '00:00:00.0010000',
          }))
        : [],
    }
  }),
  tokenUsage: {
    inputTokens: terminal.has(run.state) ? 386 : 120,
    outputTokens: run.state === 'Queued' ? 0 : 214,
    totalTokens: terminal.has(run.state) ? 600 : run.state === 'Queued' ? 120 : 334,
  },
  diagnostics: summaryDiagnostics(run),
})

const detailDiagnostics = (run: StubRun) => {
  if (run.state === 'Failed') {
    return [{
      sequence: 0,
      runId: run.id,
      scope: 'stage',
      scopeId: 'execute',
      code: 'STUB_SOURCE_UNAVAILABLE',
      title: '销售快照暂不可用',
      message: '受控执行引擎未找到请求的合成快照。',
      recovery: '调整期间，或稍后重试当前问题。',
      severity: 'error',
      occurredAt: completedAt,
    }]
  }
  if (run.outcome === 'empty' && run.state === 'Succeeded') {
    return [{
      sequence: 0,
      runId: run.id,
      scope: 'run',
      scopeId: run.id,
      code: 'DATA_COVERAGE_GAP',
      title: '该期间没有可用数据',
      message: '所选期间早于当前合成数据覆盖范围。',
      recovery: '将期间调整为 2024 年之后再试。',
      severity: 'info',
      occurredAt: completedAt,
    }]
  }
  return []
}

const resultFor = (run: StubRun) => ({
  columns: [
    {
      key: 'region',
      label: '区域',
      dataType: 'string',
      format: 'text',
      nullable: false,
    },
    {
      key: 'revenue',
      label: '净销售额',
      dataType: 'decimal',
      format: 'currency',
      nullable: false,
    },
    {
      key: 'attainment',
      label: '目标达成率',
      dataType: 'float',
      format: 'percent',
      nullable: false,
    },
    {
      key: 'governed',
      label: '已治理',
      dataType: 'boolean',
      format: 'text',
      nullable: false,
    },
    {
      key: 'closedOn',
      label: '关闭日期',
      dataType: 'date',
      format: 'date',
      nullable: true,
    },
  ],
  rows: run.outcome === 'empty' ? [] : [['华东', 4_286_000, 1.08, true, null]],
  rowCount: run.outcome === 'empty' ? 0 : 1,
  truncated: false,
})

const detailFor = (run: StubRun) => {
  const hasResult = run.state === 'Succeeded'
  const lineageNodes = [
    {
      id: 'source-sales',
      kind: 'source',
      operation: 'Read governed regional sales',
      sourceAlias: 'regional_sales',
      sourceType: 'synthetic',
      resultId: null,
      parameters: [],
    },
    {
      id: 'result-sales',
      kind: 'result',
      operation: 'Commit inline result',
      sourceAlias: null,
      sourceType: null,
      resultId: 'result-synthetic',
      parameters: [],
    },
  ]
  return {
    runId: run.id,
    question: String(run.request.question),
    sqg: {
      version: 'sqg.v0',
      intent: [...String(run.request.question)].slice(0, 512).join(''),
      ontology: 'regional-sales',
      resolvedMembers: ['sales.region', 'sales.net_revenue'],
      metrics: ['net_revenue', 'target_attainment'],
      dimensions: ['sales.region'],
      filters: [{ field: 'targets.quarter', operator: 'equals', value: '2025-Q2' }],
      policyChecks: ['governed'],
    },
    physicalNodes: [{
      id: 'aggregate-region',
      kind: 'AGGREGATE',
      label: '按区域汇总',
      plainLanguage: '把每个区域的订单收入分别加总。',
      inputs: ['regional-sales'],
      outputFields: ['region', 'revenue', 'attainment', 'governed', 'closedOn'],
    }],
    result: hasResult ? resultFor(run) : null,
    manifest: hasResult
      ? {
          resultId: 'result-synthetic',
          runId: run.id,
          nodeId: 'aggregate-region',
          storage: 'inline',
          uri: `results/${run.id}/result-synthetic`,
          rowCount: run.outcome === 'empty' ? 0 : 1,
          byteCount: run.outcome === 'empty' ? 0 : 128,
          checksum: 'sha256:synthetic',
          committedAt: completedAt,
        }
      : null,
    lineage: {
      version: 'query-runtime/v0',
      runId: run.id,
      nodes: lineageNodes,
      edges: [{
        source: 'source-sales',
        target: 'result-sales',
        relation: 'produces',
      }],
    },
    diagnostics: detailDiagnostics(run),
  }
}

const parseBody = async (request: IncomingMessage) => {
  const chunks: Buffer[] = []
  for await (const chunk of request) chunks.push(Buffer.from(chunk))
  if (chunks.length === 0) return undefined
  return JSON.parse(Buffer.concat(chunks).toString('utf8')) as unknown
}

const send = (response: ServerResponse, status: number, body?: unknown) => {
  response.statusCode = status
  if (body === undefined) {
    response.end()
    return
  }
  response.setHeader('Content-Type', 'application/json')
  response.end(JSON.stringify(body))
}

const isCreateRequest = (value: unknown): value is Record<string, unknown> => {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) return false
  const request = value as Record<string, unknown>
  const strings = [
    'clientRequestId',
    'workload',
    'question',
    'evaluationClock',
    'evaluationTimezone',
  ]
  return strings.every((field) =>
    typeof request[field] === 'string' && String(request[field]).length > 0)
    && /(?:Z|[+-]\d{2}:\d{2})$/.test(String(request.evaluationClock))
    && ['regional_quarterly_profit', 'monthly_regional_comparison']
      .includes(String(request.compilationMode))
    && request.executionMode === 'thread'
    && ['normal', 'stream'].includes(String(request.outputMode))
}

export const startHttpStubServer = async (port = 4310) => {
  const requests: StubRequest[] = []
  const runs = new Map<string, StubRun>()
  const runsByClientRequestId = new Map<string, StubRun>()
  const ambiguousCreateFailures = new Set<string>()
  const ambiguousPollFailures = new Set<string>()
  const ambiguousDetailFailures = new Set<string>()
  let sequence = 2
  const seed: StubRun = {
    id: runId(1),
    request: {
      clientRequestId: 'seed-request',
      workload: 'regional-sales',
      question: '比较各区域第二季度净销售额、目标达成率和同比',
      evaluationClock: createdAt,
      evaluationTimezone: 'Asia/Shanghai',
      compilationMode: 'regional_quarterly_profit',
      executionMode: 'thread',
      outputMode: 'normal',
    },
    state: 'Succeeded',
    terminalState: 'Succeeded',
    outcome: 'success',
    polls: 2,
    version: 3,
  }
  runs.set(seed.id, seed)
  runsByClientRequestId.set(String(seed.request.clientRequestId), seed)
  const fixtureRun: StubRun = {
    id: backendRunDetail.runId,
    request: {
      clientRequestId: 'fixture-request',
      workload: 'synthetic-workload',
      question: backendRunDetail.question,
      evaluationClock: '2026-08-15T09:00:00+08:00',
      evaluationTimezone: 'Asia/Shanghai',
      compilationMode: 'regional_quarterly_profit',
      executionMode: 'thread',
      outputMode: 'normal',
    },
    state: 'Succeeded',
    terminalState: 'Succeeded',
    outcome: 'success',
    polls: 2,
    version: 3,
  }
  runs.set(fixtureRun.id, fixtureRun)
  runsByClientRequestId.set(String(fixtureRun.request.clientRequestId), fixtureRun)

  const server = createServer(async (request, response) => {
    const url = new URL(request.url ?? '/', 'http://127.0.0.1')
    if (request.method === 'OPTIONS') {
      send(response, 204)
      return
    }
    let body: unknown
    try {
      body = await parseBody(request)
    } catch {
      send(response, 400, { title: 'Invalid JSON', code: 'invalid_json', status: 400 })
      return
    }
    requests.push({
      method: request.method ?? 'GET',
      path: url.pathname,
      headers: request.headers,
      ...(body === undefined ? {} : { body }),
    })

    if (request.method === 'GET' && url.pathname === '/api/v1/runs') {
      const items = [...runs.values()].map(summaryFor)
      send(response, 200, { items, count: items.length })
      return
    }
    if (request.method === 'POST' && url.pathname === '/api/v1/runs') {
      if (!isCreateRequest(body)) {
        send(response, 400, {
          title: 'Invalid run request',
          detail: 'All typed v1 run fields are required.',
          code: 'invalid_run_request',
          status: 400,
        })
        return
      }
      const clientRequestId = String(body.clientRequestId)
      const existing = runsByClientRequestId.get(clientRequestId)
      if (existing) {
        send(response, 200, summaryFor(existing))
        return
      }
      const question = String(body.question)
      const run: StubRun = {
        id: runId(sequence),
        request: body,
        state: 'Queued',
        terminalState: question.includes('执行失败') ? 'Failed' : 'Succeeded',
        outcome: question.includes('0 行') || question.includes('2022')
          ? 'empty'
          : question.includes('执行失败')
            ? 'failed'
            : 'success',
        polls: 0,
        version: 1,
      }
      sequence += 1
      runs.set(run.id, run)
      runsByClientRequestId.set(clientRequestId, run)
      if (question.includes('[ambiguous-create]')
        && !ambiguousCreateFailures.has(clientRequestId)) {
        ambiguousCreateFailures.add(clientRequestId)
        response.destroy()
        return
      }
      send(response, 202, summaryFor(run))
      return
    }

    const match = url.pathname.match(
      /^\/api\/v1\/runs\/([^/]+)(\/detail|\/cancel|\/semantic-status)?$/,
    )
    const run = match ? runs.get(decodeURIComponent(match[1] ?? '')) : undefined
    if (!run) {
      send(response, 404, { title: 'Run not found', status: 404 })
      return
    }
    if (request.method === 'POST' && match?.[2] === '/cancel') {
      run.version += 1
      if (String(run.request.question).includes('[delayed-cancel]')) {
        run.state = 'CancelRequested'
        run.cancellationRequested = true
      } else {
        run.state = 'Cancelled'
      }
      send(response, 200, summaryFor(run))
      return
    }
    if (request.method === 'GET' && match?.[2] === '/detail') {
      if (run.id === backendRunDetail.runId) {
        send(response, 200, backendRunDetail)
        return
      }
      const requestKey = String(run.request.clientRequestId)
      if (String(run.request.question).includes('[ambiguous-detail]')
        && !ambiguousDetailFailures.has(requestKey)) {
        ambiguousDetailFailures.add(requestKey)
        response.destroy()
        return
      }
      if (String(run.request.question).includes('[invalid-detail]')) {
        send(response, 200, { runId: run.id, question: run.request.question })
        return
      }
      send(response, 200, detailFor(run))
      return
    }
    if (request.method === 'GET' && match?.[2] === '/semantic-status') {
      if (!terminal.has(run.state)) {
        const requestKey = String(run.request.clientRequestId)
        if (String(run.request.question).includes('[ambiguous-poll]')
          && !ambiguousPollFailures.has(requestKey)) {
          ambiguousPollFailures.add(requestKey)
          response.destroy()
          return
        }
        if (run.cancellationRequested && !run.staleAfterCancellationSent) {
          run.staleAfterCancellationSent = true
          send(response, 200, {
            ...summaryFor(run),
            state: 'Running',
            version: Math.max(1, run.version - 1),
          })
          return
        }
        if (run.cancellationRequested) {
          run.state = 'Cancelled'
          run.version += 1
          send(response, 200, summaryFor(run))
          return
        }
        run.polls += 1
        if (String(run.request.question).includes('[timeout]')) {
          run.state = 'Running'
        } else {
          run.state = run.polls === 1 ? 'Running' : run.terminalState
        }
        run.version += 1
      }
      send(response, 200, summaryFor(run))
      return
    }
    if (request.method === 'GET' && match?.[2] === undefined) {
      send(response, 200, summaryFor(run))
      return
    }
    send(response, 405, { title: 'Method not allowed', status: 405 })
  })

  await new Promise<void>((resolve, reject) => {
    server.once('error', reject)
    server.listen(port, '127.0.0.1', () => resolve())
  })
  const address = server.address()
  if (!address || typeof address === 'string') throw new Error('Stub server address is unavailable.')
  return {
    baseUrl: `http://127.0.0.1:${address.port}`,
    requests,
    close: () => new Promise<void>((resolve, reject) => {
      server.close((error) => error ? reject(error) : resolve())
      server.closeAllConnections()
    }),
  }
}

if (process.argv[1]
  && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const instance = await startHttpStubServer()
  console.log(`Semantic Nexus HTTP stub listening at ${instance.baseUrl}`)
}
