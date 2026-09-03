import { createSeedRun, createStages } from '@/api/mockFixtures'
import {
  MockSemanticNexusClient,
  RunHistoryStorageError,
} from '@/api/mockSemanticNexusClient'
import {
  RUN_STORAGE_KEY,
  RUN_STORAGE_CHANGE_EVENT,
  RUN_STORAGE_QUARANTINE_KEY,
  RUN_STORAGE_RECORD_PREFIX,
  RUN_STORAGE_VERSION,
  runStorageKey,
  serializeStoredRun,
} from '@/api/runStorage'
import type { AskRequest, Run } from '@/domain'

const request: AskRequest = {
  question: '比较区域销售表现',
  scenario: 'success',
  model: 'Nexus Planner Small',
  executionMode: '受控执行',
  outputMode: '表格',
}

const store = (runs: unknown[], version = RUN_STORAGE_VERSION) => {
  window.localStorage.setItem(RUN_STORAGE_KEY, JSON.stringify({ version, runs }))
}

describe('run history storage', () => {
  it('uses collision-resistant IDs and merges stale cross-tab clients', async () => {
    const firstClient = new MockSemanticNexusClient(0, false, 'client-a')
    const secondClient = new MockSemanticNexusClient(0, false, 'client-b')
    const first = await firstClient.startRun(request)
    const second = await secondClient.startRun(request)

    expect(first.id).toMatch(/^run-syn-[0-9a-f-]{36}$/)
    expect(second.id).toMatch(/^run-syn-[0-9a-f-]{36}$/)
    expect(second.id).not.toBe(first.id)
    expect((await firstClient.listRuns()).map((run) => run.id)).toEqual(
      expect.arrayContaining([first.id, second.id]),
    )
  })

  it.each([
    ['null payload', 'null'],
    ['malformed JSON', '{'],
    ['version mismatch', JSON.stringify({ version: 99, runs: [] })],
  ])('quarantines %s without crashing hydration', async (_name, raw) => {
    window.localStorage.setItem(RUN_STORAGE_KEY, raw)

    const client = new MockSemanticNexusClient(0, false)

    await expect(client.listRuns()).resolves.toEqual([])
    expect(window.localStorage.getItem(RUN_STORAGE_QUARANTINE_KEY)).not.toBeNull()
  })

  it('keeps valid runs and discards nested malformed entries', async () => {
    const valid = createSeedRun('run-syn-1010', '有效运行', 'succeeded', 10)
    const malformed = { ...valid, id: 'run-syn-1011', sqg: null }
    store([valid, malformed])

    const client = new MockSemanticNexusClient(0, false)
    const runs = await client.listRuns()

    expect(runs).toHaveLength(1)
    expect(runs[0]?.id).toBe('run-syn-1010')
    expect(window.localStorage.getItem(RUN_STORAGE_QUARANTINE_KEY)).not.toBeNull()
  })

  it('keeps valid runs in memory when repairing storage exceeds quota', async () => {
    const valid = createSeedRun('run-syn-1012', '可恢复运行', 'succeeded', 10)
    store([valid, { ...valid, id: 'run-syn-1013', stages: null }])
    const originalSetItem = Storage.prototype.setItem
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key,
      value,
    ) {
      if (key.startsWith(RUN_STORAGE_RECORD_PREFIX)) {
        throw new DOMException('Quota exceeded', 'QuotaExceededError')
      }
      originalSetItem.call(this, key, value)
    })

    const client = new MockSemanticNexusClient(0, false)

    await expect(client.listRuns()).resolves.toEqual([
      expect.objectContaining({ id: 'run-syn-1012' }),
    ])
    setItem.mockRestore()
  })

  it('terminalizes a persisted running run with an interruption diagnostic', async () => {
    const interrupted = createSeedRun('run-syn-1020', '中断运行', 'succeeded', 10)
    interrupted.state = 'running'
    interrupted.completedAt = undefined
    interrupted.result = undefined
    interrupted.manifest = undefined
    interrupted.stages = createStages()
    const compile = interrupted.stages[1]
    if (!compile) throw new Error('Compile stage fixture is missing')
    interrupted.stages[0]!.state = 'succeeded'
    compile.state = 'running'
    store([interrupted])

    const client = new MockSemanticNexusClient(0, false)
    const hydrated = await client.getRun(interrupted.id)

    expect(hydrated?.state).toBe('failed')
    expect(hydrated?.stages[1]?.state).toBe('failed')
    expect(hydrated?.stages[2]?.state).toBe('canceled')
    expect(hydrated?.diagnostics).toContainEqual(expect.objectContaining({
      code: 'MOCK_RUN_INTERRUPTED',
    }))
  })

  it('does not terminalize another client active under a fresh heartbeat', async () => {
    const active = createSeedRun('run-syn-active-tab', '其他标签页运行', 'succeeded', 1)
    active.state = 'running'
    active.completedAt = undefined
    active.result = undefined
    active.manifest = undefined
    active.stages = createStages()
    active.stages[0]!.state = 'running'
    active.executionLease = {
      ownerId: 'client-a',
      heartbeatAt: new Date().toISOString(),
    }
    store([active])

    const otherClient = new MockSemanticNexusClient(0, false, 'client-b')

    await expect(otherClient.getRun(active.id)).resolves.toEqual(
      expect.objectContaining({ state: 'running' }),
    )
  })

  it('does not terminalize the current client under a fresh heartbeat', async () => {
    const active = createSeedRun('run-syn-current-tab', '当前标签页运行', 'succeeded', 1)
    active.state = 'running'
    active.completedAt = undefined
    active.result = undefined
    active.manifest = undefined
    active.stages = createStages()
    active.stages[0]!.state = 'running'
    active.executionLease = {
      ownerId: 'client-a',
      heartbeatAt: new Date().toISOString(),
    }
    store([active])

    const currentClient = new MockSemanticNexusClient(0, false, 'client-a')

    await expect(currentClient.getRun(active.id)).resolves.toEqual(
      expect.objectContaining({ state: 'running' }),
    )
  })

  it('synchronizes same-document consumers without relying on storage events', async () => {
    const consumer = new MockSemanticNexusClient(0, false, 'client-b')
    const run = createSeedRun('run-syn-same-document', '同页运行', 'succeeded', 1)

    window.dispatchEvent(new CustomEvent(RUN_STORAGE_CHANGE_EVENT, {
      detail: {
        id: run.id,
        newValue: serializeStoredRun(run),
        sourceId: 'client-a',
      },
    }))

    await expect(consumer.getRun(run.id)).resolves.toEqual(
      expect.objectContaining({ question: '同页运行' }),
    )
  })

  it('terminalizes another client run after its heartbeat becomes stale', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-03T12:00:00Z'))
    const active = createSeedRun('run-syn-stale-later', '稍后过期运行', 'succeeded', 1)
    active.state = 'running'
    active.completedAt = undefined
    active.result = undefined
    active.manifest = undefined
    active.stages = createStages()
    active.stages[0]!.state = 'running'
    active.executionLease = {
      ownerId: 'client-a',
      heartbeatAt: new Date().toISOString(),
    }
    store([active])
    const observer = new MockSemanticNexusClient(0, false, 'client-b')

    vi.advanceTimersByTime(30_001)
    const expired = await observer.getRun(active.id)

    expect(expired?.state).toBe('failed')
    expect(expired?.diagnostics).toContainEqual(expect.objectContaining({
      code: 'MOCK_RUN_INTERRUPTED',
    }))
    vi.useRealTimers()
  })

  it('surfaces quota failures and does not retain a phantom run', async () => {
    const client = new MockSemanticNexusClient(0, false)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError')
    })

    await expect(client.startRun(request)).rejects.toBeInstanceOf(RunHistoryStorageError)
    await expect(client.listRuns()).resolves.toEqual([])
    setItem.mockRestore()
  })

  it('terminalizes and removes a run when a later persistence write fails', async () => {
    const client = new MockSemanticNexusClient(0, false)
    const originalSetItem = Storage.prototype.setItem
    let writes = 0
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key,
      value,
    ) {
      writes += 1
      if (writes === 2) {
        throw new DOMException('Quota exceeded', 'QuotaExceededError')
      }
      originalSetItem.call(this, key, value)
    })

    await expect(client.startRun(request)).rejects.toBeInstanceOf(RunHistoryStorageError)
    const [aborted] = await client.listRuns()

    expect(aborted?.state).toBe('failed')
    expect(aborted?.stages[0]?.state).toBe('failed')
    expect(aborted?.diagnostics).toContainEqual(expect.objectContaining({
      code: 'RUN_PROGRESS_NOT_PERSISTED',
    }))
    expect(aborted?.stages.filter((stage) => stage.state === 'failed')).toHaveLength(1)
    expect(aborted?.stages.some((stage) =>
      stage.state === 'running' || stage.state === 'pending')).toBe(false)
    expect(window.localStorage.getItem(runStorageKey(aborted!.id))).toBeNull()
    setItem.mockRestore()
  })

  it('fails Generate and removes an uncommitted result when the final write fails', async () => {
    const client = new MockSemanticNexusClient(0, false)
    const originalSetItem = Storage.prototype.setItem
    let writes = 0
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key,
      value,
    ) {
      writes += 1
      if (writes === 7) {
        throw new DOMException('Quota exceeded', 'QuotaExceededError')
      }
      originalSetItem.call(this, key, value)
    })

    await expect(client.startRun(request)).rejects.toBeInstanceOf(RunHistoryStorageError)
    const [aborted] = await client.listRuns()

    expect(aborted?.state).toBe('failed')
    expect(aborted?.stages.find((stage) => stage.key === 'generate')?.state).toBe('failed')
    expect(aborted?.result).toBeUndefined()
    expect(aborted?.manifest).toBeUndefined()
    expect(aborted?.diagnostics).toContainEqual(expect.objectContaining({
      code: 'RESULT_COMMIT_NOT_PERSISTED',
    }))
    expect(aborted?.stages.filter((stage) => stage.state === 'failed')).toHaveLength(1)
    expect(aborted?.stages.some((stage) =>
      stage.state === 'running' || stage.state === 'pending')).toBe(false)
    expect(window.localStorage.getItem(runStorageKey(aborted!.id))).toBeNull()
    setItem.mockRestore()
  })

  it('preserves Execute as the failed stage when failure persistence aborts', async () => {
    const client = new MockSemanticNexusClient(0, false)
    const originalSetItem = Storage.prototype.setItem
    let writes = 0
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key,
      value,
    ) {
      writes += 1
      if (writes === 5) {
        throw new DOMException('Quota exceeded', 'QuotaExceededError')
      }
      originalSetItem.call(this, key, value)
    })

    await expect(client.startRun({ ...request, scenario: 'failure' }))
      .rejects.toBeInstanceOf(RunHistoryStorageError)
    const [aborted] = await client.listRuns()

    expect(aborted?.stages.map((stage) => stage.state)).toEqual([
      'succeeded',
      'succeeded',
      'succeeded',
      'failed',
      'canceled',
    ])
    expect(aborted?.diagnostics).toContainEqual(expect.objectContaining({
      code: 'RUN_PROGRESS_NOT_PERSISTED',
    }))
    setItem.mockRestore()
  })

  it('marks the active stage failed when cancellation persistence aborts', async () => {
    vi.useFakeTimers()
    const client = new MockSemanticNexusClient(100, false)
    const originalSetItem = Storage.prototype.setItem
    let writes = 0
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (
      this: Storage,
      key,
      value,
    ) {
      writes += 1
      if (writes === 2) {
        throw new DOMException('Quota exceeded', 'QuotaExceededError')
      }
      originalSetItem.call(this, key, value)
    })
    const runPromise = client.startRun(request)
    const rejection = expect(runPromise).rejects.toBeInstanceOf(RunHistoryStorageError)
    const [active] = await client.listRuns()
    if (!active) throw new Error('Active run fixture is missing')

    await client.cancelRun(active.id)
    await vi.runAllTimersAsync()
    await rejection
    const [aborted] = await client.listRuns()

    expect(aborted?.stages.map((stage) => stage.state)).toEqual([
      'failed',
      'canceled',
      'canceled',
      'canceled',
      'canceled',
    ])
    expect(aborted?.state).toBe('failed')
    setItem.mockRestore()
  })

  it('rejects malformed values at every major nested contract boundary', async () => {
    const valid = createSeedRun('run-syn-1030', '基准运行', 'succeeded', 10)
    const mutations: Array<Partial<Run>> = [
      { stages: [null] as unknown as Run['stages'] },
      { nodes: [{ ...valid.nodes[0], outputFields: null }] as unknown as Run['nodes'] },
      { result: { ...valid.result!, rows: [null] } as unknown as Run['result'] },
      { lineage: { ...valid.lineage, sources: [null] } as unknown as Run['lineage'] },
      { diagnostics: [null] as unknown as Run['diagnostics'] },
      { manifest: { ...valid.manifest!, committedAt: 'not-a-date' } },
    ]
    store(mutations.map((mutation, index) => ({
      ...valid,
      id: `run-syn-${1100 + index}`,
      ...mutation,
    })))

    const client = new MockSemanticNexusClient(0, false)

    await expect(client.listRuns()).resolves.toEqual([])
    expect(window.localStorage.getItem(RUN_STORAGE_QUARANTINE_KEY)).not.toBeNull()
  })
})
