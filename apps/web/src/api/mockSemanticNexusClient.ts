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
type ExecutionLease = NonNullable<Run['executionLease']>
type LeaseFence = ExecutionLease | null

const isActive = (run: Run) => run.state === 'running' || run.state === 'queued'

const sameLease = (left: ExecutionLease | undefined, right: ExecutionLease) =>
  left?.ownerId === right.ownerId
  && left.generation === right.generation
  && left.heartbeatAt === right.heartbeatAt

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

class RunLeaseLostError extends Error {
  constructor(readonly currentRun: Run | undefined) {
    super('运行租约已由另一个页面终止或接管。')
    this.name = 'RunLeaseLostError'
  }
}

export class MockSemanticNexusClient implements SemanticNexusClient {
  private runs = new Map<string, Run>()
  private canceled = new Set<string>()
  private leaseExpiryTimers = new Map<string, number>()

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
        void this.persistRunBestEffort(run)
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
    const leaseGeneration = createUuid()
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
      executionLease: this.currentLease(leaseGeneration),
    }
    this.runs.set(id, run)
    try {
      await this.persistRun(run)
    } catch (error) {
      this.runs.delete(id)
      throw error
    }

    let committingResult = false
    let persistenceStage: StageKey | undefined
    let persistenceLease: ExecutionLease | undefined
    try {
      for (let index = 0; index < run.stages.length; index += 1) {
        if (this.canceled.has(id)) {
          persistenceStage = run.stages.find((stage) => stage.state === 'pending')?.key
          const persistedLease = this.readOwnedActiveLease(id, leaseGeneration)
          if (!persistedLease) return await this.reconcileLeaseLoss(run, onProgress)
          persistenceLease = persistedLease
          return await this.finishCanceled(run, persistedLease, onProgress)
        }
        const stage = run.stages[index]
        if (!stage) continue
        stage.state = 'running'
        run.executionLease = this.currentLease(leaseGeneration)
        onProgress?.(clone(run))
        await this.delay(this.stageDelayMs)

        const persistedLease = this.readOwnedActiveLease(id, leaseGeneration)
        if (!persistedLease) return await this.reconcileLeaseLoss(run, onProgress)
        persistenceLease = persistedLease

        if (this.canceled.has(id)) {
          persistenceStage = stage.key
          return await this.finishCanceled(run, persistedLease, onProgress)
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
          await this.saveAndNotify(run, persistedLease, onProgress)
          return clone(run)
        }

        stage.state = 'succeeded'
        stage.durationMs = this.stageDelayMs
        run.elapsedMs = this.stageDelayMs * (index + 1)
        run.executionLease = this.currentLease(leaseGeneration)
        run.tokens = {
          input: 320 + index * 18,
          output: index < 1 ? 0 : 74 + index * 29,
        }
        persistenceStage = stage.key
        await this.saveAndNotify(run, persistedLease, onProgress)
      }

      committingResult = true
      persistenceStage = 'generate'
      const finalLease = run.executionLease
      if (!finalLease) return await this.reconcileLeaseLoss(run, onProgress)
      persistenceLease = finalLease
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
      await this.saveAndNotify(run, finalLease, onProgress)
      return clone(run)
    } catch (error) {
      if (error instanceof RunLeaseLostError) {
        return await this.reconcileLeaseLoss(run, onProgress, error.currentRun)
      }
      if (error instanceof RunHistoryStorageError) {
        await this.abortAfterPersistenceFailure(
          run,
          persistenceStage,
          persistenceLease,
          committingResult,
          error,
        )
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

  private async finishCanceled(
    run: Run,
    expectedLease: ExecutionLease,
    onProgress?: (run: Run) => void,
  ): Promise<Run> {
    run.state = 'canceled'
    run.completedAt = new Date().toISOString()
    run.executionLease = undefined
    run.stages.forEach((stage) => {
      if (stage.state === 'running' || stage.state === 'pending') {
        stage.state = 'canceled'
      }
    })
    await this.saveAndNotify(run, expectedLease, onProgress)
    return clone(run)
  }

  private async saveAndNotify(
    run: Run,
    expectedLease: ExecutionLease,
    onProgress?: (run: Run) => void,
  ) {
    this.runs.set(run.id, run)
    await this.persistRun(run, expectedLease)
    onProgress?.(clone(run))
  }

  private async reconcileLeaseLoss(
    staleRun: Run,
    onProgress?: (run: Run) => void,
    currentRun = this.readRunRecord(staleRun.id),
  ): Promise<Run> {
    const durableRun = currentRun ?? this.readRunRecord(staleRun.id)
    if (durableRun && isActive(durableRun)) {
      const heartbeat = durableRun.executionLease
        ? Date.parse(durableRun.executionLease.heartbeatAt)
        : Number.NEGATIVE_INFINITY
      if (heartbeat < Date.now() - INTERRUPTED_HEARTBEAT_MS) {
        const expectedLease = durableRun.executionLease
          ? clone(durableRun.executionLease)
          : null
        const terminal = this.interruptedClone(durableRun)
        this.runs.set(terminal.id, terminal)
        this.clearLeaseExpiry(terminal.id)
        onProgress?.(clone(terminal))
        try {
          await this.persistRun(terminal, expectedLease)
        } catch (error) {
          if (error instanceof RunLeaseLostError) {
            return this.reconcileLeaseLoss(staleRun, onProgress, error.currentRun)
          }
          throw error
        }
        return clone(terminal)
      }
    }
    const reconciled = durableRun ?? this.interruptedClone(staleRun)
    this.runs.set(reconciled.id, reconciled)
    this.scheduleLeaseExpiry(reconciled)
    onProgress?.(clone(reconciled))
    return clone(reconciled)
  }

  private async abortAfterPersistenceFailure(
    run: Run,
    persistenceStage: StageKey | undefined,
    expectedLease: ExecutionLease | undefined,
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
    this.clearLeaseExpiry(run.id)
    try {
      const current = await this.withRunLock(run.id, () => {
        const persisted = this.readRunRecord(run.id)
        if (!expectedLease || !persisted || !isActive(persisted)
          || !sameLease(persisted.executionLease, expectedLease)) return persisted
        window.localStorage.removeItem(runStorageKey(run.id))
        this.dispatchRunStorageChange(run.id, null)
        return undefined
      })
      if (current) {
        this.runs.set(current.id, current)
        this.scheduleLeaseExpiry(current)
      }
    } catch (removeError) {
      if (expectedLease) this.scheduleLeaseRetry(run, expectedLease)
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
    if (legacy.length > 0) void this.migrateLegacyRuns(legacy)
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
          void this.removeInvalidRunRecord(key, raw)
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
    return parsed.runs
  }

  private async migrateLegacyRuns(runs: Run[]) {
    try {
      await Promise.all(runs.map((run) => this.withRunLock(run.id, () => {
        if (window.localStorage.getItem(runStorageKey(run.id)) === null) {
          const serialized = serializeStoredRun(run)
          window.localStorage.setItem(runStorageKey(run.id), serialized)
          this.dispatchRunStorageChange(run.id, serialized)
        }
      })))
      window.localStorage.removeItem(RUN_STORAGE_KEY)
    } catch (error) {
      console.warn('Unable to migrate legacy mock run history.', error)
    }
  }

  private async removeInvalidRunRecord(key: string, rejectedRaw: string) {
    const encodedId = key.slice(RUN_STORAGE_RECORD_PREFIX.length)
    const id = decodeURIComponent(encodedId)
    try {
      await this.withRunLock(id, () => {
        if (window.localStorage.getItem(key) !== rejectedRaw) return
        window.localStorage.removeItem(key)
        this.dispatchRunStorageChange(id, null)
      })
    } catch (error) {
      console.warn('Unable to remove an invalid mock run record.', error)
    }
  }

  private syncRunRecords() {
    this.readRunRecords().forEach((run) => this.runs.set(run.id, run))
    this.terminalizeInterruptedRuns()
  }

  private async persistRun(run: Run, expectedLease?: LeaseFence) {
    try {
      await this.withRunLock(run.id, () => {
        if (expectedLease !== undefined) {
          const current = this.readRunRecord(run.id)
          const leaseMatches = expectedLease === null
            ? current?.executionLease === undefined
            : sameLease(current?.executionLease, expectedLease)
          if (!current || !isActive(current) || !leaseMatches) {
            throw new RunLeaseLostError(current)
          }
        }
        const serialized = serializeStoredRun(run)
        window.localStorage.setItem(runStorageKey(run.id), serialized)
        this.dispatchRunStorageChange(run.id, serialized)
        this.scheduleLeaseExpiry(run)
      })
    } catch (error) {
      if (error instanceof RunLeaseLostError) throw error
      if (error instanceof RunHistoryStorageError) throw error
      throw new RunHistoryStorageError(error)
    }
  }

  private async persistRunBestEffort(run: Run, expectedLease?: LeaseFence) {
    try {
      await this.persistRun(run, expectedLease)
    } catch (error) {
      if (error instanceof RunLeaseLostError) {
        await this.reconcileLeaseLoss(run, undefined, error.currentRun)
        return
      }
      if (error instanceof RunHistoryStorageError && expectedLease !== undefined) {
        try {
          const current = this.readRunRecord(run.id)
          const leaseMatches = expectedLease === null
            ? current?.executionLease === undefined
            : sameLease(current?.executionLease, expectedLease)
          if (current && isActive(current) && leaseMatches) {
            this.runs.set(current.id, current)
            this.scheduleLeaseRetry(current, expectedLease)
          }
        } catch {
          this.scheduleLeaseRetry(run, expectedLease)
        }
      }
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
      if (!isActive(run)) {
        this.clearLeaseExpiry(run.id)
        return
      }
      const heartbeat = run.executionLease
        ? Date.parse(run.executionLease.heartbeatAt)
        : Number.NEGATIVE_INFINITY
      const heartbeatIsStale = heartbeat < Date.now() - INTERRUPTED_HEARTBEAT_MS
      if (!heartbeatIsStale) {
        this.scheduleLeaseExpiry(run)
        return
      }
      void this.terminalizeInterruptedRun(run)
    })
  }

  private async terminalizeInterruptedRun(run: Run) {
    const expiredLease = run.executionLease ? clone(run.executionLease) : null
    const terminal = this.interruptedClone(run)
    this.runs.set(terminal.id, terminal)
    await this.persistRunBestEffort(terminal, expiredLease)
  }

  private interruptedClone(run: Run): Run {
    const terminal = clone(run)
    terminal.state = 'failed'
    terminal.completedAt = new Date().toISOString()
    terminal.executionLease = undefined
    terminal.result = undefined
    terminal.manifest = undefined
    const interruptedStage = terminal.stages.find((stage) => stage.state === 'running')
      ?? terminal.stages.find((stage) => stage.state === 'pending')
    terminal.stages.forEach((stage) => {
      if (stage === interruptedStage) stage.state = 'failed'
      else if (stage.state === 'running' || stage.state === 'pending') stage.state = 'canceled'
    })
    if (!terminal.diagnostics.some((diagnostic) => diagnostic.code === 'MOCK_RUN_INTERRUPTED')) {
      terminal.diagnostics.push({
        code: 'MOCK_RUN_INTERRUPTED',
        title: '运行因页面关闭而中断',
        message: '执行租约已失效，受控运行没有继续提交。',
        recovery: '从原问题重新发起一次运行。',
        severity: 'warning',
      })
    }
    return terminal
  }

  private currentLease(generation: string): ExecutionLease {
    return {
      ownerId: this.clientId,
      generation,
      heartbeatAt: new Date().toISOString(),
    }
  }

  private readRunRecord(id: string): Run | undefined {
    try {
      const raw = window.localStorage.getItem(runStorageKey(id))
      return raw ? parseStoredRun(raw) : undefined
    } catch (error) {
      throw new RunHistoryStorageError(error)
    }
  }

  private readOwnedActiveLease(id: string, generation: string): ExecutionLease | undefined {
    const current = this.readRunRecord(id)
    if (!current || !isActive(current)) return undefined
    const lease = current.executionLease
    if (lease?.ownerId !== this.clientId || lease.generation !== generation) return undefined
    if (Date.parse(lease.heartbeatAt) < Date.now() - INTERRUPTED_HEARTBEAT_MS) return undefined
    return clone(lease)
  }

  private scheduleLeaseExpiry(run: Run) {
    this.clearLeaseExpiry(run.id)
    if (!isActive(run)) return
    const expiresAt = run.executionLease
      ? Date.parse(run.executionLease.heartbeatAt) + INTERRUPTED_HEARTBEAT_MS
      : Date.now()
    const delay = Math.max(0, expiresAt - Date.now() + 1)
    const expectedLease = run.executionLease ? clone(run.executionLease) : null
    const timer = window.setTimeout(
      () => this.expireObservedLease(run.id, expectedLease),
      delay,
    )
    this.leaseExpiryTimers.set(run.id, timer)
  }

  private clearLeaseExpiry(id: string) {
    const timer = this.leaseExpiryTimers.get(id)
    if (timer !== undefined) window.clearTimeout(timer)
    this.leaseExpiryTimers.delete(id)
  }

  private scheduleLeaseRetry(run: Run, expectedLease: LeaseFence) {
    this.clearLeaseExpiry(run.id)
    const timer = window.setTimeout(
      () => this.expireObservedLease(run.id, expectedLease),
      1_000,
    )
    this.leaseExpiryTimers.set(run.id, timer)
  }

  private expireObservedLease(id: string, expectedLease: LeaseFence) {
    this.leaseExpiryTimers.delete(id)
    let current: Run | undefined
    try {
      current = this.readRunRecord(id)
    } catch {
      if (expectedLease) {
        const fallback = this.runs.get(id)
        if (fallback) this.scheduleLeaseRetry(fallback, expectedLease)
      }
      return
    }
    if (!current) {
      this.runs.delete(id)
      return
    }
    this.runs.set(id, current)
    if (!isActive(current)) {
      this.clearLeaseExpiry(id)
      return
    }
    const leaseMatches = expectedLease === null
      ? current.executionLease === undefined
      : sameLease(current.executionLease, expectedLease)
    if (!leaseMatches) {
      this.scheduleLeaseExpiry(current)
      return
    }
    const heartbeat = current.executionLease
      ? Date.parse(current.executionLease.heartbeatAt)
      : Number.NEGATIVE_INFINITY
    if (heartbeat >= Date.now() - INTERRUPTED_HEARTBEAT_MS) {
      this.scheduleLeaseExpiry(current)
      return
    }
    void this.terminalizeInterruptedRun(current)
  }

  private async withRunLock<T>(id: string, action: () => T | Promise<T>): Promise<T> {
    if (!navigator.locks?.request) {
      throw new RunHistoryStorageError(new Error('Web Locks API is unavailable'))
    }
    return navigator.locks.request(`semantic-nexus:run:${id}`, { mode: 'exclusive' }, action)
  }

  private handleStorageEvent = (event: StorageEvent) => {
    if (!event.key?.startsWith(RUN_STORAGE_RECORD_PREFIX)) return
    if (!event.newValue) {
      const encodedId = event.key.slice(RUN_STORAGE_RECORD_PREFIX.length)
      const id = decodeURIComponent(encodedId)
      this.runs.delete(id)
      this.clearLeaseExpiry(id)
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
      this.clearLeaseExpiry(detail.id)
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
