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
  parseStoredRun,
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

const withoutColumnDataTypes = (run: Run) => {
  const legacy = JSON.parse(JSON.stringify(run)) as Run
  legacy.result?.columns.forEach((column) => {
    delete (column as Partial<typeof column>).dataType
  })
  return legacy
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

  it('migrates origin/main-shaped aggregate v1 columns before validation', async () => {
    const legacy = withoutColumnDataTypes(
      createSeedRun('run-syn-legacy-aggregate', '旧聚合记录', 'succeeded', 10),
    )
    store([legacy])

    const client = new MockSemanticNexusClient(0, false)
    const [migrated] = await client.listRuns()

    expect(migrated?.id).toBe(legacy.id)
    expect(migrated?.result?.columns.map((column) => column.dataType)).toEqual([
      'string',
      'integer',
      'float',
      'float',
    ])
    await vi.waitFor(() => {
      expect(window.localStorage.getItem(RUN_STORAGE_KEY)).toBeNull()
      expect(parseStoredRun(
        window.localStorage.getItem(runStorageKey(legacy.id)) ?? '',
      )?.result?.columns.every((column) => Boolean(column.dataType))).toBe(true)
    })
    expect(window.localStorage.getItem(RUN_STORAGE_QUARANTINE_KEY)).toBeNull()
  })

  it('migrates and rewrites origin/main-shaped per-run v1 columns', async () => {
    const legacy = withoutColumnDataTypes(
      createSeedRun('run-syn-legacy-record', '旧逐运行记录', 'succeeded', 10),
    )
    const key = runStorageKey(legacy.id)
    window.localStorage.setItem(key, JSON.stringify({
      version: RUN_STORAGE_VERSION,
      run: legacy,
    }))

    const client = new MockSemanticNexusClient(0, false)
    const migrated = await client.getRun(legacy.id)

    expect(migrated?.result?.columns.map((column) => column.dataType)).toEqual([
      'string',
      'integer',
      'float',
      'float',
    ])
    await vi.waitFor(() => {
      const rewritten = JSON.parse(window.localStorage.getItem(key) ?? '{}') as {
        run?: Run
      }
      expect(rewritten.run?.result?.columns.every((column) => Boolean(column.dataType)))
        .toBe(true)
    })
    expect(window.localStorage.getItem(RUN_STORAGE_QUARANTINE_KEY)).toBeNull()
  })

  it.each([
    ['aggregate', 'mixed-presence'],
    ['aggregate', 'mixed-cells'],
    ['aggregate', 'ambiguous-empty'],
    ['aggregate', 'boolean-cell'],
    ['aggregate', 'null-cell'],
    ['aggregate', 'date-format'],
    ['aggregate', 'sqg-version'],
    ['aggregate', 'source-node'],
    ['aggregate', 'truncated-row-count'],
    ['per-run', 'mixed-presence'],
    ['per-run', 'mixed-cells'],
    ['per-run', 'ambiguous-empty'],
    ['per-run', 'boolean-cell'],
    ['per-run', 'null-cell'],
    ['per-run', 'date-format'],
    ['per-run', 'sqg-version'],
    ['per-run', 'source-node'],
    ['per-run', 'truncated-row-count'],
  ] as const)(
    'rejects malformed legacy %s v1 records with %s',
    async (storageKind, corruption) => {
      const current = createSeedRun(
        `run-syn-invalid-${storageKind}-${corruption}`,
        '损坏旧记录',
        corruption === 'ambiguous-empty' ? 'empty' : 'succeeded',
        10,
      )
      const legacy = withoutColumnDataTypes(current)
      if (corruption === 'mixed-presence') {
        const firstColumn = legacy.result?.columns[0]
        const currentFirstColumn = current.result?.columns[0]
        if (!firstColumn || !currentFirstColumn) throw new Error('Column fixture is missing')
        firstColumn.dataType = currentFirstColumn.dataType
      } else if (corruption === 'mixed-cells') {
        const secondRow = legacy.result?.rows[1]
        if (!secondRow) throw new Error('Row fixture is missing')
        secondRow.region = 42
      } else if (corruption === 'boolean-cell' || corruption === 'null-cell') {
        const firstRow = legacy.result?.rows[0]
        if (!firstRow) throw new Error('Row fixture is missing')
        firstRow.region = corruption === 'boolean-cell' ? true : null
      } else if (corruption === 'date-format') {
        const firstColumn = legacy.result?.columns[0]
        if (!firstColumn) throw new Error('Column fixture is missing')
        firstColumn.format = 'date'
      } else if (corruption === 'sqg-version') {
        legacy.sqg.version = 'sqg.v0'
      } else if (corruption === 'source-node') {
        const firstNode = legacy.nodes[0]
        if (!firstNode) throw new Error('Node fixture is missing')
        firstNode.kind = 'SOURCE'
      } else if (corruption === 'truncated-row-count') {
        if (!legacy.result) throw new Error('Result fixture is missing')
        legacy.result.rowCount += 1
        legacy.result.truncated = true
      }

      const key = runStorageKey(legacy.id)
      if (storageKind === 'aggregate') {
        store([legacy])
      } else {
        window.localStorage.setItem(key, JSON.stringify({
          version: RUN_STORAGE_VERSION,
          run: legacy,
        }))
      }

      const client = new MockSemanticNexusClient(0, false)

      await expect(client.listRuns()).resolves.toEqual([])
      expect(window.localStorage.getItem(RUN_STORAGE_QUARANTINE_KEY)).not.toBeNull()
      if (storageKind === 'per-run') {
        await vi.waitFor(() => expect(window.localStorage.getItem(key)).toBeNull())
      }
    },
  )

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
      generation: 'lease-active-tab',
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
      generation: 'lease-current-tab',
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
      generation: 'lease-stale-later',
      heartbeatAt: new Date().toISOString(),
    }
    store([active])
    const observer = new MockSemanticNexusClient(0, false, 'client-b')

    await vi.advanceTimersByTimeAsync(30_001)
    const persisted = parseStoredRun(
      window.localStorage.getItem(runStorageKey(active.id)) ?? '',
    )

    expect(persisted?.state).toBe('failed')
    const expired = await observer.getRun(active.id)

    expect(expired?.state).toBe('failed')
    expect(expired?.diagnostics).toContainEqual(expect.objectContaining({
      code: 'MOCK_RUN_INTERRUPTED',
    }))
    vi.useRealTimers()
  })

  it('fences a stale owner after an observer terminalizes its persisted lease', async () => {
    vi.useFakeTimers()
    const owner = new MockSemanticNexusClient(100, false, 'client-a')
    const runPromise = owner.startRun(request)
    const [active] = await owner.listRuns()
    if (!active) throw new Error('Active run fixture is missing')
    const terminal = createSeedRun(active.id, active.question, 'failed', 1)
    terminal.createdAt = active.createdAt
    terminal.stages = createStages()
    terminal.stages[0]!.state = 'failed'
    terminal.stages.slice(1).forEach((stage) => {
      stage.state = 'canceled'
    })
    terminal.diagnostics = [{
      code: 'MOCK_RUN_INTERRUPTED',
      title: '观察者已终止过期运行',
      message: '执行租约已失效。',
      recovery: '重新发起运行。',
      severity: 'warning',
    }]
    window.localStorage.setItem(runStorageKey(active.id), serializeStoredRun(terminal))

    await vi.advanceTimersByTimeAsync(500)
    const result = await runPromise
    const persisted = parseStoredRun(
      window.localStorage.getItem(runStorageKey(active.id)) ?? '',
    )

    expect(result.state).toBe('failed')
    expect(persisted?.state).toBe('failed')
    expect(persisted?.executionLease).toBeUndefined()
    vi.useRealTimers()
  })

  it('fences a stale owner when the same owner id has a newer lease generation', async () => {
    vi.useFakeTimers()
    const owner = new MockSemanticNexusClient(100, false, 'client-a')
    const runPromise = owner.startRun(request)
    const [active] = await owner.listRuns()
    if (!active?.executionLease) throw new Error('Active lease fixture is missing')
    const replacement = JSON.parse(JSON.stringify(active)) as Run
    replacement.executionLease = {
      ...active.executionLease,
      generation: 'replacement-generation',
    }
    window.localStorage.setItem(runStorageKey(active.id), serializeStoredRun(replacement))

    await vi.advanceTimersByTimeAsync(500)
    const result = await runPromise
    const persisted = parseStoredRun(
      window.localStorage.getItem(runStorageKey(active.id)) ?? '',
    )

    expect(result.executionLease?.generation).toBe('replacement-generation')
    expect(persisted?.executionLease?.generation).toBe('replacement-generation')
    expect(persisted?.elapsedMs).toBe(0)
    vi.useRealTimers()
  })

  it('reports a terminal stale owner state before startRun resolves', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-03T12:00:00Z'))
    const progress: Run[] = []
    const owner = new MockSemanticNexusClient(31_000, false, 'client-a')
    const runPromise = owner.startRun(request, (run) => progress.push(run))

    await vi.advanceTimersByTimeAsync(31_000)
    const result = await runPromise

    expect(result.state).toBe('failed')
    expect(progress.at(-1)?.state).toBe('failed')
    expect(parseStoredRun(
      window.localStorage.getItem(runStorageKey(result.id)) ?? '',
    )?.state).toBe('failed')
    vi.useRealTimers()
  })

  it('reschedules observer expiry when the owner renews its heartbeat', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-09-03T12:00:00Z'))
    const active = createSeedRun('run-syn-renewed', '续期运行', 'succeeded', 1)
    active.state = 'running'
    active.completedAt = undefined
    active.result = undefined
    active.manifest = undefined
    active.stages = createStages()
    active.stages[0]!.state = 'running'
    active.executionLease = {
      ownerId: 'client-a',
      generation: 'lease-renewed',
      heartbeatAt: new Date().toISOString(),
    }
    store([active])
    new MockSemanticNexusClient(0, false, 'client-b')

    await vi.advanceTimersByTimeAsync(20_000)
    active.executionLease.heartbeatAt = new Date().toISOString()
    const serialized = serializeStoredRun(active)
    window.localStorage.setItem(runStorageKey(active.id), serialized)
    window.dispatchEvent(new CustomEvent(RUN_STORAGE_CHANGE_EVENT, {
      detail: { id: active.id, newValue: serialized, sourceId: 'client-a' },
    }))
    await vi.advanceTimersByTimeAsync(10_001)

    expect(parseStoredRun(
      window.localStorage.getItem(runStorageKey(active.id)) ?? '',
    )?.state).toBe('running')

    await vi.advanceTimersByTimeAsync(20_000)
    expect(parseStoredRun(
      window.localStorage.getItem(runStorageKey(active.id)) ?? '',
    )?.state).toBe('failed')
    vi.useRealTimers()
  })

  it('fails closed when the browser cannot provide an exclusive lock', async () => {
    const locks = navigator.locks
    Object.defineProperty(navigator, 'locks', { configurable: true, value: undefined })
    const client = new MockSemanticNexusClient(0, false)

    await expect(client.startRun(request)).rejects.toBeInstanceOf(RunHistoryStorageError)
    await expect(client.listRuns()).resolves.toEqual([])

    Object.defineProperty(navigator, 'locks', { configurable: true, value: locks })
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
      {
        executionLease: {
          ownerId: 'client-a',
          heartbeatAt: new Date().toISOString(),
        } as unknown as Run['executionLease'],
      },
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
