import type { SemanticNexusClient } from './semanticNexusClient'
import {
  componentStatus,
  createSeedRun,
  createSqg,
  createStages,
  emptyResult,
  lineage,
  nodes,
  ontology,
  successResult,
} from './mockFixtures'
import {
  parseStoredRun,
  parseStoredRuns,
  RUN_STORAGE_KEY,
  RUN_STORAGE_CHANGE_EVENT,
  RUN_STORAGE_QUARANTINE_KEY,
  RUN_STORAGE_RECORD_PREFIX,
  runStorageKey,
  serializeStoredRun,
} from './runStorage'
import type { AskRequest, Run, StageKey } from '@/domain'

const INTERRUPTED_HEARTBEAT_MS = 30_000

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T

const createUuid = () => {
  const bytes = window.crypto.getRandomValues(new Uint8Array(16))
  bytes[6] = ((bytes[6] ?? 0) & 0x0f) | 0x40
  bytes[8] = ((bytes[8] ?? 0) & 0x3f) | 0x80
  const hex = [...bytes].map((value) => value.toString(16).padStart(2, '0'))
  return [
    hex.slice(0, 4).join(''),
    hex.slice(4, 6).join(''),
    hex.slice(6, 8).join(''),
    hex.slice(8, 10).join(''),
    hex.slice(10).join(''),
  ].join('-')
}

export class RunHistoryStorageError extends Error {
  readonly storageCause: unknown

  constructor(cause: unknown) {
    super('无法保存本地运行历史。请释放浏览器存储空间或允许本地存储后重试。')
    this.name = 'RunHistoryStorageError'
    this.storageCause = cause
  }
}

export class MockSemanticNexusClient implements SemanticNexusClient {
  private runs = new Map<string, Run>()
  private canceled = new Set<string>()

  constructor(
    private readonly stageDelayMs = 360,
    seedHistory = true,
    private readonly clientId = createUuid(),
  ) {
    this.readStoredRuns().forEach((run) => this.runs.set(run.id, run))
    if (this.runs.size === 0 && seedHistory) {
      const seeds = [
        createSeedRun('run-syn-1001', '比较各区域第二季度净销售额、目标达成率和同比', 'succeeded', 48),
        createSeedRun('run-syn-1002', '查看 2022 年第一季度西部区域表现', 'empty', 132),
      ]
      seeds.forEach((run) => {
        this.runs.set(run.id, run)
        this.persistRunBestEffort(run)
      })
    }
    this.terminalizeInterruptedRuns()
    window.addEventListener('storage', this.handleStorageEvent)
    window.addEventListener(RUN_STORAGE_CHANGE_EVENT, this.handleLocalStorageEvent)
  }

  async listRuns(): Promise<Run[]> {
    this.syncRunRecords()
    return clone([...this.runs.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt)))
  }

  async getRun(id: string): Promise<Run | undefined> {
    this.syncRunRecords()
    const run = this.runs.get(id)
    return run ? clone(run) : undefined
  }

  async startRun(request: AskRequest, onProgress?: (run: Run) => void): Promise<Run> {
    const id = `run-syn-${createUuid()}`
    const run: Run = {
      id,
      question: request.question,
      state: 'running',
      createdAt: new Date().toISOString(),
      elapsedMs: 0,
      model: request.model,
      executionMode: request.executionMode,
      outputMode: request.outputMode,
      tokens: { input: 0, output: 0 },
      stages: createStages(),
      sqg: createSqg(request),
      nodes,
      lineage,
      diagnostics: [],
      scenario: request.scenario,
      executionLease: this.currentLease(),
    }
    this.runs.set(id, run)
    try {
      this.persistRun(run)
    } catch (error) {
      this.runs.delete(id)
      throw error
    }

    let committingResult = false
    let persistenceStage: StageKey | undefined
    try {
      for (let index = 0; index < run.stages.length; index += 1) {
        if (this.canceled.has(id)) {
          persistenceStage = run.stages.find((stage) => stage.state === 'pending')?.key
          return this.finishCanceled(run, onProgress)
        }
        const stage = run.stages[index]
        if (!stage) continue
        stage.state = 'running'
        run.executionLease = this.currentLease()
        onProgress?.(clone(run))
        await this.delay(this.stageDelayMs)

        if (this.canceled.has(id)) {
          persistenceStage = stage.key
          return this.finishCanceled(run, onProgress)
        }

        if (request.scenario === 'failure' && stage.key === 'execute') {
          stage.state = 'failed'
          stage.durationMs = this.stageDelayMs
          run.state = 'failed'
          run.elapsedMs = this.stageDelayMs * (index + 1)
          run.completedAt = new Date().toISOString()
          run.executionLease = undefined
          run.tokens = { input: 412, output: 188 }
          run.diagnostics = [{
            code: 'MOCK_SOURCE_UNAVAILABLE',
            title: '销售快照暂不可用',
            message: '受控执行引擎未找到 2025-Q3 的合成快照。',
            recovery: '改用 2025-Q2，或稍后重试当前问题。',
            severity: 'error',
          }]
          persistenceStage = stage.key
          this.saveAndNotify(run, onProgress)
          return clone(run)
        }

        stage.state = 'succeeded'
        stage.durationMs = this.stageDelayMs
        run.elapsedMs = this.stageDelayMs * (index + 1)
        run.executionLease = this.currentLease()
        run.tokens = {
          input: 320 + index * 18,
          output: index < 1 ? 0 : 74 + index * 29,
        }
        persistenceStage = stage.key
        this.saveAndNotify(run, onProgress)
      }

      committingResult = true
      persistenceStage = 'generate'
      run.result = request.scenario === 'empty' ? emptyResult : successResult
      run.state = request.scenario === 'empty' ? 'empty' : 'succeeded'
      run.completedAt = new Date().toISOString()
      run.executionLease = undefined
      run.manifest = {
        uri: `mock://result-store/${id}/manifest.json`,
        format: 'Arrow + JSON manifest',
        checksum: 'sha256:7ba2…c14e',
        committedAt: run.completedAt,
      }
      if (request.scenario === 'empty') {
        run.diagnostics = [{
          code: 'DATA_COVERAGE_GAP',
          title: '该期间没有可用数据',
          message: emptyResult.coverage,
          recovery: '将期间调整为 2024 年之后再试。',
          severity: 'info',
        }]
      }
      this.saveAndNotify(run, onProgress)
      return clone(run)
    } catch (error) {
      if (error instanceof RunHistoryStorageError) {
        this.abortAfterPersistenceFailure(run, persistenceStage, committingResult, error)
      }
      throw error
    }
  }

  async cancelRun(id: string): Promise<Run> {
    const run = this.runs.get(id)
    if (!run) throw new Error(`Run ${id} not found`)
    this.canceled.add(id)
    return clone(run)
  }

  async getOntology() {
    return clone(ontology)
  }

  async getComponentStatus() {
    return clone(componentStatus)
  }

  private finishCanceled(run: Run, onProgress?: (run: Run) => void): Run {
    run.state = 'canceled'
    run.completedAt = new Date().toISOString()
    run.executionLease = undefined
    run.stages.forEach((stage) => {
      if (stage.state === 'running' || stage.state === 'pending') {
        stage.state = 'canceled'
      }
    })
    this.saveAndNotify(run, onProgress)
    return clone(run)
  }

  private saveAndNotify(run: Run, onProgress?: (run: Run) => void) {
    this.runs.set(run.id, run)
    this.persistRun(run)
    onProgress?.(clone(run))
  }

  private abortAfterPersistenceFailure(
    run: Run,
    persistenceStage: StageKey | undefined,
    finalCommit: boolean,
    error: RunHistoryStorageError,
  ) {
    run.state = 'failed'
    run.completedAt = new Date().toISOString()
    run.executionLease = undefined
    run.result = undefined
    run.manifest = undefined
    const failedIndex = Math.max(
      0,
      run.stages.findIndex((stage) => stage.key === persistenceStage),
    )
    run.stages.forEach((stage, index) => {
      if (index === failedIndex) stage.state = 'failed'
      else if (index > failedIndex || stage.state !== 'succeeded') stage.state = 'canceled'
    })
    run.diagnostics = [
      ...run.diagnostics,
      {
        code: finalCommit ? 'RESULT_COMMIT_NOT_PERSISTED' : 'RUN_PROGRESS_NOT_PERSISTED',
        title: finalCommit ? '结果未能提交' : '运行记录未能保存',
        message: error.message,
        recovery: '释放浏览器存储空间后，从原问题重新发起运行。',
        severity: 'error',
      },
    ]
    this.runs.set(run.id, clone(run))
    try {
      window.localStorage.removeItem(runStorageKey(run.id))
      this.dispatchRunStorageChange(run.id, null)
    } catch (removeError) {
      console.warn('Unable to remove aborted mock run record.', removeError)
    }
  }

  private delay(milliseconds: number) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
  }

  private readStoredRuns(): Run[] {
    const records = this.readRunRecords()
    const legacy = this.readLegacyRuns()
    legacy.forEach((run) => {
      if (!records.has(run.id)) records.set(run.id, run)
    })
    return [...records.values()]
  }

  private readRunRecords() {
    const records = new Map<string, Run>()
    let keys: string[]
    try {
      keys = Array.from({ length: window.localStorage.length }, (_, index) =>
        window.localStorage.key(index)).filter((key): key is string =>
          Boolean(key?.startsWith(RUN_STORAGE_RECORD_PREFIX)))
    } catch (error) {
      console.warn('Unable to enumerate mock run history.', error)
      return records
    }

    keys.forEach((key) => {
      try {
        const raw = window.localStorage.getItem(key)
        if (!raw) return
        const run = parseStoredRun(raw)
        if (run) {
          records.set(run.id, run)
        } else {
          this.quarantine(raw, 'invalid-run-record')
          window.localStorage.removeItem(key)
        }
      } catch (error) {
        console.warn('Unable to read a mock run record.', error)
      }
    })
    return records
  }

  private readLegacyRuns() {
    let raw: string | null
    try {
      raw = window.localStorage.getItem(RUN_STORAGE_KEY)
    } catch (error) {
      console.warn('Unable to read legacy mock run history.', error)
      return []
    }
    if (!raw) return []

    const parsed = parseStoredRuns(raw)
    if (parsed.rejected) this.quarantine(raw, parsed.reason ?? 'invalid-legacy-payload')
    let migrated = true
    parsed.runs.forEach((run) => {
      try {
        window.localStorage.setItem(runStorageKey(run.id), serializeStoredRun(run))
      } catch (error) {
        migrated = false
        console.warn('Unable to migrate a legacy mock run record.', error)
      }
    })
    if (migrated) {
      try {
        window.localStorage.removeItem(RUN_STORAGE_KEY)
      } catch (error) {
        console.warn('Unable to remove migrated legacy mock run history.', error)
      }
    }
    return parsed.runs
  }

  private syncRunRecords() {
    this.readRunRecords().forEach((run) => this.runs.set(run.id, run))
    this.terminalizeInterruptedRuns()
  }

  private persistRun(run: Run) {
    try {
      const serialized = serializeStoredRun(run)
      window.localStorage.setItem(runStorageKey(run.id), serialized)
      this.dispatchRunStorageChange(run.id, serialized)
    } catch (error) {
      throw new RunHistoryStorageError(error)
    }
  }

  private persistRunBestEffort(run: Run) {
    try {
      this.persistRun(run)
    } catch (error) {
      console.warn('Unable to persist hydrated mock run history.', error)
    }
  }

  private quarantine(payload: string, reason: string) {
    try {
      window.localStorage.setItem(RUN_STORAGE_QUARANTINE_KEY, JSON.stringify({
        quarantinedAt: new Date().toISOString(),
        reason,
        payload,
      }))
    } catch (error) {
      console.warn('Unable to quarantine invalid mock run history.', error)
    }
  }

  private terminalizeInterruptedRuns() {
    this.runs.forEach((run) => {
      if (run.state !== 'running' && run.state !== 'queued') return
      const heartbeat = run.executionLease
        ? Date.parse(run.executionLease.heartbeatAt)
        : Number.NEGATIVE_INFINITY
      const heartbeatIsStale = heartbeat < Date.now() - INTERRUPTED_HEARTBEAT_MS
      if (!heartbeatIsStale) return

      run.state = 'failed'
      run.completedAt = new Date().toISOString()
      run.executionLease = undefined
      const interruptedStage = run.stages.find((stage) => stage.state === 'running')
        ?? run.stages.find((stage) => stage.state === 'pending')
      run.stages.forEach((stage) => {
        if (stage === interruptedStage) stage.state = 'failed'
        else if (stage.state === 'running' || stage.state === 'pending') stage.state = 'canceled'
      })
      run.diagnostics.push({
        code: 'MOCK_RUN_INTERRUPTED',
        title: '运行因页面关闭而中断',
        message: '执行租约已失效，受控运行没有继续提交。',
        recovery: '从原问题重新发起一次运行。',
        severity: 'warning',
      })
      this.persistRunBestEffort(run)
    })
  }

  private currentLease() {
    return {
      ownerId: this.clientId,
      heartbeatAt: new Date().toISOString(),
    }
  }

  private handleStorageEvent = (event: StorageEvent) => {
    if (!event.key?.startsWith(RUN_STORAGE_RECORD_PREFIX)) return
    if (!event.newValue) {
      const encodedId = event.key.slice(RUN_STORAGE_RECORD_PREFIX.length)
      this.runs.delete(decodeURIComponent(encodedId))
      return
    }
    const run = parseStoredRun(event.newValue)
    if (run) {
      this.runs.set(run.id, run)
      this.terminalizeInterruptedRuns()
    }
  }

  private handleLocalStorageEvent = (event: Event) => {
    const detail = (event as CustomEvent<{
      id: string
      newValue: string | null
      sourceId: string
    }>).detail
    if (!detail || detail.sourceId === this.clientId) return
    if (!detail.newValue) {
      this.runs.delete(detail.id)
      return
    }
    const run = parseStoredRun(detail.newValue)
    if (run) {
      this.runs.set(run.id, run)
      this.terminalizeInterruptedRuns()
    }
  }

  private dispatchRunStorageChange(id: string, newValue: string | null) {
    window.dispatchEvent(new CustomEvent(RUN_STORAGE_CHANGE_EVENT, {
      detail: { id, newValue, sourceId: this.clientId },
    }))
  }
}
