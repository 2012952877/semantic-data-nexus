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
  parseStoredRuns,
  RUN_STORAGE_KEY,
  RUN_STORAGE_QUARANTINE_KEY,
  RUN_STORAGE_VERSION,
  type StoredRuns,
} from './runStorage'
import type { AskRequest, Run, StageState } from '@/domain'

export class RunHistoryStorageError extends Error {
  readonly storageCause: unknown

  constructor(cause: unknown) {
    super('无法保存本地运行历史。请释放浏览器存储空间或允许本地存储后重试。')
    this.name = 'RunHistoryStorageError'
    this.storageCause = cause
  }
}

const clone = <T>(value: T): T => JSON.parse(JSON.stringify(value)) as T

export class MockSemanticNexusClient implements SemanticNexusClient {
  private runs = new Map<string, Run>()
  private persistedSnapshots = new Map<string, Run>()
  private canceled = new Set<string>()
  private sequence = 1003

  constructor(private readonly stageDelayMs = 360, seedHistory = true) {
    const stored = this.readStoredRuns()
    if (stored.length > 0) {
      stored.forEach((run) => this.runs.set(run.id, run))
      this.capturePersistedSnapshots()
    } else if (seedHistory) {
      const seeds = [
        createSeedRun('run-syn-1001', '比较各区域第二季度净销售额、目标达成率和同比', 'succeeded', 48),
        createSeedRun('run-syn-1002', '查看 2022 年第一季度西部区域表现', 'empty', 132),
      ]
      seeds.forEach((run) => this.runs.set(run.id, run))
      this.persistBestEffort()
    }
    this.terminalizeInterruptedRuns()
    this.sequence = this.nextPersistedSequence()
  }

  async listRuns(): Promise<Run[]> {
    return clone([...this.runs.values()].sort((a, b) => b.createdAt.localeCompare(a.createdAt)))
  }

  async getRun(id: string): Promise<Run | undefined> {
    const run = this.runs.get(id)
    return run ? clone(run) : undefined
  }

  async startRun(request: AskRequest, onProgress?: (run: Run) => void): Promise<Run> {
    const id = this.nextRunId()
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
    }
    this.runs.set(id, run)
    try {
      this.persist()
    } catch (error) {
      this.runs.delete(id)
      throw error
    }

    for (let index = 0; index < run.stages.length; index += 1) {
      if (this.canceled.has(id)) {
        return this.finishCanceled(run, onProgress)
      }
      const stage = run.stages[index]
      if (!stage) continue
      stage.state = 'running'
      onProgress?.(clone(run))
      await this.delay(this.stageDelayMs)

      if (this.canceled.has(id)) {
        return this.finishCanceled(run, onProgress)
      }

      if (request.scenario === 'failure' && stage.key === 'execute') {
        stage.state = 'failed'
        stage.durationMs = this.stageDelayMs
        run.state = 'failed'
        run.elapsedMs = this.stageDelayMs * (index + 1)
        run.completedAt = new Date().toISOString()
        run.tokens = { input: 412, output: 188 }
        run.diagnostics = [{
          code: 'MOCK_SOURCE_UNAVAILABLE',
          title: '销售快照暂不可用',
          message: '受控执行引擎未找到 2025-Q3 的合成快照。',
          recovery: '改用 2025-Q2，或稍后重试当前问题。',
          severity: 'error',
        }]
        this.saveAndNotify(run, onProgress)
        return clone(run)
      }

      stage.state = 'succeeded'
      stage.durationMs = this.stageDelayMs
      run.elapsedMs = this.stageDelayMs * (index + 1)
      run.tokens = {
        input: 320 + index * 18,
        output: index < 1 ? 0 : 74 + index * 29,
      }
      this.saveAndNotify(run, onProgress)
    }

    run.result = request.scenario === 'empty' ? emptyResult : successResult
    run.state = request.scenario === 'empty' ? 'empty' : 'succeeded'
    run.completedAt = new Date().toISOString()
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
    run.stages.forEach((stage) => {
      if (stage.state === 'running' || stage.state === 'pending') {
        stage.state = 'canceled' as StageState
      }
    })
    this.saveAndNotify(run, onProgress)
    return clone(run)
  }

  private saveAndNotify(run: Run, onProgress?: (run: Run) => void) {
    const persisted = this.persistedSnapshots.get(run.id)
    this.runs.set(run.id, run)
    try {
      this.persist()
    } catch (error) {
      if (persisted) this.runs.set(run.id, clone(persisted))
      else this.runs.delete(run.id)
      throw error
    }
    onProgress?.(clone(run))
  }

  private delay(milliseconds: number) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
  }

  private readStoredRuns(): Run[] {
    let raw: string | null
    try {
      raw = window.localStorage.getItem(RUN_STORAGE_KEY)
    } catch (error) {
      console.warn('Unable to read mock run history.', error)
      return []
    }
    if (!raw) return []

    const parsed = parseStoredRuns(raw)
    if (parsed.rejected) {
      this.quarantine(raw, parsed.reason ?? 'invalid-payload')
      try {
        if (parsed.runs.length === 0) {
          window.localStorage.removeItem(RUN_STORAGE_KEY)
        } else {
          this.writeStoredRuns(parsed.runs)
        }
      } catch (error) {
        console.warn('Unable to persist repaired mock run history.', error)
      }
    }
    return parsed.runs
  }

  private persist() {
    try {
      this.writeStoredRuns([...this.runs.values()])
      this.capturePersistedSnapshots()
    } catch (error) {
      throw new RunHistoryStorageError(error)
    }
  }

  private persistBestEffort() {
    try {
      this.writeStoredRuns([...this.runs.values()])
      this.capturePersistedSnapshots()
    } catch (error) {
      console.warn('Unable to persist hydrated mock run history.', error)
    }
  }

  private writeStoredRuns(runs: Run[]) {
    const payload: StoredRuns = { version: RUN_STORAGE_VERSION, runs }
    window.localStorage.setItem(RUN_STORAGE_KEY, JSON.stringify(payload))
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
    let changed = false
    this.runs.forEach((run) => {
      if (run.state !== 'running' && run.state !== 'queued') return
      changed = true
      run.state = 'failed'
      run.completedAt = new Date().toISOString()
      const interruptedStage = run.stages.find((stage) => stage.state === 'running')
        ?? run.stages.find((stage) => stage.state === 'pending')
      run.stages.forEach((stage) => {
        if (stage === interruptedStage) stage.state = 'failed'
        else if (stage.state === 'running' || stage.state === 'pending') stage.state = 'canceled'
      })
      run.diagnostics.push({
        code: 'MOCK_RUN_INTERRUPTED',
        title: '运行因页面关闭而中断',
        message: '浏览器在受控运行完成前关闭或重新加载。',
        recovery: '从原问题重新发起一次运行。',
        severity: 'warning',
      })
    })
    if (changed) this.persistBestEffort()
  }

  private nextPersistedSequence() {
    return [...this.runs.keys()].reduce((next, id) => {
      const match = /^run-syn-(\d+)$/.exec(id)
      return match ? Math.max(next, Number(match[1]) + 1) : next
    }, 1003)
  }

  private nextRunId() {
    let id = `run-syn-${this.sequence++}`
    while (this.runs.has(id)) id = `run-syn-${this.sequence++}`
    return id
  }

  private capturePersistedSnapshots() {
    this.persistedSnapshots = new Map(
      [...this.runs.entries()].map(([id, run]) => [id, clone(run)]),
    )
  }
}
