import { createSeedRun, createStages } from '@/api/mockFixtures'
import {
  MockSemanticNexusClient,
  RunHistoryStorageError,
} from '@/api/mockSemanticNexusClient'
import {
  RUN_STORAGE_KEY,
  RUN_STORAGE_QUARANTINE_KEY,
  RUN_STORAGE_VERSION,
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
  it('continues IDs after the highest persisted synthetic ID', async () => {
    const firstClient = new MockSemanticNexusClient(0, false)
    const first = await firstClient.startRun(request)
    const reloadedClient = new MockSemanticNexusClient(0, false)
    const second = await reloadedClient.startRun(request)

    expect(first.id).toBe('run-syn-1003')
    expect(second.id).toBe('run-syn-1004')
    expect((await reloadedClient.listRuns()).map((run) => run.id)).toEqual(
      expect.arrayContaining(['run-syn-1003', 'run-syn-1004']),
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
      if (key === RUN_STORAGE_KEY) {
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

  it('surfaces quota failures and does not retain a phantom run', async () => {
    const client = new MockSemanticNexusClient(0, false)
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('Quota exceeded', 'QuotaExceededError')
    })

    await expect(client.startRun(request)).rejects.toBeInstanceOf(RunHistoryStorageError)
    await expect(client.listRuns()).resolves.toEqual([])
    setItem.mockRestore()
  })

  it('rolls back stage progress when a later persistence write fails', async () => {
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
    const [rolledBack] = await client.listRuns()

    expect(rolledBack?.state).toBe('running')
    expect(rolledBack?.stages.every((stage) => stage.state === 'pending')).toBe(true)
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
