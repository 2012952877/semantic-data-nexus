// @vitest-environment node

import { HttpSemanticNexusClient, NexusClientError } from '@/api/httpSemanticNexusClient'
import { startHttpStubServer, type StubRequest } from './httpStubServer'

let stub: Awaited<ReturnType<typeof startHttpStubServer>>
let requestSequence = 0

beforeAll(async () => {
  stub = await startHttpStubServer(0)
})

afterAll(async () => {
  await stub.close()
})

type ClientOptions = ConstructorParameters<typeof HttpSemanticNexusClient>[0]
const createClient = (
  overrides: Omit<ClientOptions, 'baseUrl'> & { baseUrl?: string } = {},
) => new HttpSemanticNexusClient({
  baseUrl: stub.baseUrl,
  polling: {
    initialDelayMs: 1,
    maximumDelayMs: 2,
    deadlineMs: 100,
  },
  now: () => new Date('2026-09-04T13:15:50.492+08:00'),
  requestId: () => `request-unit-${++requestSequence}`,
  ...overrides,
})

const request = {
  question: '比较各区域第二季度净销售额与目标',
  scenario: 'success' as const,
  model: 'Nexus Planner Small',
  executionMode: '受控执行',
  outputMode: '表格',
}

const latest = (path: string, method = 'GET'): StubRequest | undefined =>
  [...stub.requests].reverse().find((item) => item.path === path && item.method === method)

describe('HttpSemanticNexusClient', () => {
  it('posts the complete BFF contract and maps terminal detail', async () => {
    const progress: string[] = []
    const run = await createClient({
      baseUrl: stub.baseUrl,
      tokenProvider: async () => 'unit-bearer',
      localDevelopment: { subject: 'unit-user', roles: 'Reader,Contributor' },
      polling: { initialDelayMs: 1, maximumDelayMs: 2, deadlineMs: 100 },
      now: () => new Date('2026-09-04T13:15:50.492+08:00'),
      requestId: () => 'request-unit-contract',
    }).startRun(request, (current) => progress.push(current.state))

    expect(run.state).toBe('succeeded')
    expect(run.result?.rows[0]?.region).toBe('华东')
    expect(run.result?.rows[0]?.revenue).toBe('4286000.00')
    expect(run.result?.rows[0]?.governed).toBe(true)
    expect(run.result?.rows[0]?.closedOn).toBeNull()
    expect(run.nodes[0]?.kind).toBe('AGGREGATE')
    expect(progress).toEqual(expect.arrayContaining(['queued', 'running', 'succeeded']))
    expect(latest(`/api/v1/runs/${run.id}/semantic-status`)).toBeDefined()

    const posted = latest('/api/v1/runs', 'POST')
    expect(posted?.body).toEqual({
      clientRequestId: 'request-unit-contract',
      workload: 'regional-sales',
      question: request.question,
      evaluationClock: '2026-09-04T05:15:50.492Z',
      evaluationTimezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      compilationMode: 'regional_quarterly_profit',
      executionMode: 'thread',
      outputMode: 'normal',
    })
    expect(posted?.headers.authorization).toBe('Bearer unit-bearer')
    expect(posted?.headers['x-dev-subject']).toBe('unit-user')
  })

  it('validates list, get, empty, and failed responses', async () => {
    const client = createClient()
    const runs = await client.listRuns()
    const seedId = 'run_00000000000000000000000000000001'
    expect(runs.some((run) => run.id === seedId)).toBe(true)
    expect((await client.getRun(seedId))?.manifest?.uri).toContain('results/')
    expect(await client.getRun('missing')).toBeUndefined()

    const empty = await client.startRun({ ...request, question: '查询 2022 年的 0 行结果' })
    expect(empty.state).toBe('empty')
    expect(empty.result?.rows).toEqual([])
    expect(empty.diagnostics[0]).toMatchObject({
      title: '查询完成，但没有匹配行',
      recovery: '请检查筛选条件或选择其他期间。',
    })
    expect(JSON.stringify(empty.diagnostics)).not.toMatch(/2024|合成数据/)

    const failed = await client.startRun({ ...request, question: '触发执行失败' })
    expect(failed.state).toBe('failed')
    expect(failed.diagnostics[0]?.code).toBe('STUB_SOURCE_UNAVAILABLE')
  })

  it('validates and maps the real PR #22 backend detail fixture', async () => {
    const run = await createClient().getRun('run_0123456789abcdef0123456789abcdef')

    expect(run?.sqg.version).toBe('sqg.v0')
    expect(run?.result?.rows).toEqual([{ region: '北辰区', profit: '2334.00' }])
    expect(run?.manifest?.uri).toContain('inline://run_0123456789abcdef')
    expect(run?.lineage.sources[0]?.name).toBe('synthetic_sales')
  })

  it('reuses the complete create payload and resumes a known run after ambiguous failures', async () => {
    const clientRequestId = `request-ambiguous-${++requestSequence}`
    const client = createClient({ requestId: () => clientRequestId })
    const ambiguousRequest = {
      ...request,
      question: 'recover [ambiguous-create] [ambiguous-poll] [ambiguous-detail]',
    }

    await expect(client.startRun(ambiguousRequest))
      .rejects.toMatchObject({ code: 'network' } satisfies Partial<NexusClientError>)
    await expect(client.startRun(ambiguousRequest))
      .rejects.toMatchObject({ code: 'network' } satisfies Partial<NexusClientError>)
    await expect(client.startRun(ambiguousRequest))
      .rejects.toMatchObject({ code: 'network' } satisfies Partial<NexusClientError>)
    const recovered = await client.startRun(ambiguousRequest)

    expect(recovered.state).toBe('succeeded')
    const creates = stub.requests.filter((item) =>
      item.method === 'POST'
      && item.path === '/api/v1/runs'
      && (item.body as { clientRequestId?: string })?.clientRequestId === clientRequestId)
    expect(creates).toHaveLength(2)
    expect(creates[1]?.body).toEqual(creates[0]?.body)
    const resumedStatuses = stub.requests.filter((item) =>
      item.method === 'GET'
      && item.path === `/api/v1/runs/${recovered.id}/semantic-status`)
    expect(resumedStatuses.length).toBeGreaterThanOrEqual(3)
  })

  it('keeps ambiguous retry state isolated across overlapping requests', async () => {
    const ids = [
      `request-overlap-a-${++requestSequence}`,
      `request-overlap-b-${++requestSequence}`,
    ]
    let idIndex = 0
    const client = createClient({ requestId: () => ids[idIndex++] ?? 'unexpected-request-id' })
    const firstRequest = { ...request, question: 'first overlap [ambiguous-create]' }

    await expect(client.startRun(firstRequest))
      .rejects.toMatchObject({ code: 'network' } satisfies Partial<NexusClientError>)
    await expect(client.startRun({ ...request, question: 'second overlap' }))
      .resolves.toMatchObject({ state: 'succeeded' })
    await expect(client.startRun(firstRequest))
      .resolves.toMatchObject({ state: 'succeeded' })

    const firstCreates = stub.requests.filter((item) =>
      item.method === 'POST'
      && item.path === '/api/v1/runs'
      && (item.body as { clientRequestId?: string })?.clientRequestId === ids[0])
    expect(firstCreates).toHaveLength(2)
    expect(firstCreates[1]?.body).toEqual(firstCreates[0]?.body)
    expect(idIndex).toBe(2)
  })

  it('preserves valid fixed-point decimals and rejects non-canonical decimal cells', async () => {
    const valid = [
      '1234567890123456.1200',
      '10000000000000000000000000000',
      '0.0000000000000000000000000001',
      '9.9999999999999999999999999999',
    ]
    for (const value of valid) {
      const run = await createClient().startRun({
        ...request,
        question: `decimal precision [decimal:${value}]`,
      })

      expect(run.result?.rows[0]?.revenue).toBe(value)
    }

    const invalid = [
      '+1.0',
      '01.0',
      '1.',
      '1e2',
      '-0.00',
      '10000000000000000000000000001',
      '0.00000000000000000000000000001',
    ]
    for (const value of invalid) {
      await expect(createClient().startRun({
        ...request,
        question: `invalid decimal [decimal:${value}]`,
      })).rejects.toMatchObject(
        { code: 'invalid-response' } satisfies Partial<NexusClientError>,
      )
    }
    await expect(createClient().startRun({
      ...request,
      question: 'decimal must not be a JSON number [decimal-number]',
    })).rejects.toMatchObject(
      { code: 'invalid-response' } satisfies Partial<NexusClientError>,
    )
  })

  it('enforces the BFF float magnitude and safe-integer rules', async () => {
    for (const value of ['123.5', '9007199254740991']) {
      const run = await createClient().startRun({
        ...request,
        question: `valid float [float:${value}]`,
      })
      expect(run.result?.rows[0]?.attainment).toBe(Number(value))
    }

    for (const value of ['9007199254740992', '1e28', '1.1e28', '-1.1e28']) {
      await expect(createClient().startRun({
        ...request,
        question: `invalid float [float:${value}]`,
      })).rejects.toMatchObject(
        { code: 'invalid-response' } satisfies Partial<NexusClientError>,
      )
    }
  })

  it('renders definitively rejected runs from their persisted failed summary', async () => {
    const client = createClient()
    const failed = await client.startRun({
      ...request,
      question: 'reject before dispatch [definitive-reject]',
    })

    expect(failed.state).toBe('failed')
    expect(failed.diagnostics[0]?.code).toBe('STUB_SOURCE_UNAVAILABLE')
    expect(await client.getRun(failed.id)).toMatchObject({
      state: 'failed',
      diagnostics: [expect.objectContaining({ code: 'STUB_SOURCE_UNAVAILABLE' })],
    })
    expect(latest(`/api/v1/runs/${failed.id}/semantic-status`)).toBeUndefined()
    expect(latest(`/api/v1/runs/${failed.id}/detail`)).toBeUndefined()
  })

  it('delivers cancellation and returns the terminal detail', async () => {
    const client = createClient()
    let releaseId: ((id: string) => void) | undefined
    const id = new Promise<string>((resolve) => {
      releaseId = resolve
    })
    const active = client.startRun(
      { ...request, question: 'cancel deterministically [held-running-until-cancel]' },
      (run) => releaseId?.(run.id),
    )
    const cancelled = await client.cancelRun(await id)

    expect(cancelled.state).toBe('canceled')
    expect((await active).state).toBe('canceled')
  })

  it('awaits an in-flight cancellation when its poll fails before terminal persistence', async () => {
    let observePollRejection: (() => void) | undefined
    const pollRejected = new Promise<void>((resolve) => {
      observePollRejection = resolve
    })
    const observedFetch: typeof fetch = async (input, init) => {
      try {
        return await fetch(input, init)
      } catch (error) {
        if (String(input).includes('/semantic-status')) observePollRejection?.()
        throw error
      }
    }
    const client = createClient({ fetch: observedFetch })
    let releaseId: ((id: string) => void) | undefined
    const id = new Promise<string>((resolve) => {
      releaseId = resolve
    })
    const active = client.startRun(
      { ...request, question: 'cancel during failed poll [poll-fails-before-cancel-terminal]' },
      (run) => releaseId?.(run.id),
    )
    const runId = await id
    await vi.waitFor(() => {
      expect(latest(`/api/v1/runs/${runId}/semantic-status`)).toBeDefined()
    })

    const cancellation = client.cancelRun(runId)
    expect(client.cancelRun(runId)).toBe(cancellation)
    await pollRejected
    await vi.waitFor(() => expect(stub.isCancellationHeld(runId)).toBe(true))
    let activeSettled = false
    void active.then(
      () => { activeSettled = true },
      () => { activeSettled = true },
    )
    try {
      await Promise.resolve()
      await Promise.resolve()
      expect(activeSettled).toBe(false)
    } finally {
      stub.releaseCancellation(runId)
    }
    expect((await cancellation).state).toBe('canceled')
    await expect(active).resolves.toMatchObject({ state: 'canceled' })
    expect(latest(`/api/v1/runs/${runId}/detail`)).toBeUndefined()
  })

  it('rejects path-bound summaries that identify a different run', async () => {
    await expect(createClient().startRun({
      ...request,
      question: 'mismatched status [mismatch-status]',
    })).rejects.toMatchObject(
      { code: 'invalid-response' } satisfies Partial<NexusClientError>,
    )

    const lookupClient = createClient()
    const lookup = await lookupClient.startRun({
      ...request,
      question: 'mismatched lookup [mismatch-lookup]',
    })
    await expect(lookupClient.getRun(lookup.id)).rejects.toMatchObject(
      { code: 'invalid-response' } satisfies Partial<NexusClientError>,
    )

    const cancelClient = createClient()
    const cancel = await cancelClient.startRun({
      ...request,
      question: 'mismatched cancel [mismatch-cancel]',
    })
    await expect(cancelClient.cancelRun(cancel.id)).rejects.toMatchObject(
      { code: 'invalid-response' } satisfies Partial<NexusClientError>,
    )
  })

  it('keeps terminal cancellation absorbing when a delayed poll returns an older version', async () => {
      const client = createClient()
      let releaseId: ((id: string) => void) | undefined
      const id = new Promise<string>((resolve) => {
        releaseId = resolve
      })
      const states: string[] = []
      const active = client.startRun(
        { ...request, question: 'cancel race [delayed-cancel]' },
        (run) => {
          states.push(run.state)
          releaseId?.(run.id)
        },
      )
      const requested = await client.cancelRun(await id)
      const cancelled = await active

      expect(requested.state).toBe('running')
      expect(cancelled.state).toBe('canceled')
      expect(states.at(-1)).toBe('canceled')
      expect(latest(`/api/v1/runs/${cancelled.id}/detail`)).toBeUndefined()
  })

  it('uses Unicode code points and rejects every Unicode category-C character', async () => {
      const client = createClient()
      const astralQuestion = '😀'.repeat(2_001)
      const accepted = await client.startRun({ ...request, question: astralQuestion })
      expect(accepted.state).toBe('succeeded')

      const postCount = stub.requests.filter((item) =>
        item.method === 'POST' && item.path === '/api/v1/runs').length
      await expect(client.startRun({ ...request, question: '😀'.repeat(4_001) }))
        .rejects.toMatchObject({ code: 'request' } satisfies Partial<NexusClientError>)
      await expect(client.startRun({ ...request, question: 'hidden\u200Bformat' }))
        .rejects.toMatchObject({ code: 'request' } satisfies Partial<NexusClientError>)
      expect(stub.requests.filter((item) =>
        item.method === 'POST' && item.path === '/api/v1/runs')).toHaveLength(postCount)
  })

  it('marks HTTP-only ontology and health placeholders as synthetic and unavailable', async () => {
      const noFetch: typeof fetch = async () => {
        throw new Error('HTTP metadata placeholders must not probe undocumented endpoints')
      }
      const client = createClient({ fetch: noFetch })

      await expect(client.getOntology()).resolves.toMatchObject({
        version: 'unavailable',
        entities: [],
        metrics: [],
      })
      await expect(client.getComponentStatus()).resolves.toEqual([
        expect.objectContaining({
          provider: expect.stringContaining('synthetic'),
          status: 'unknown',
          detail: expect.stringContaining('未探测'),
        }),
      ])
  })

  it('fails closed on invalid detail and bounded polling timeout', async () => {
    await expect(createClient().startRun({
      ...request,
      question: 'return [invalid-detail]',
    })).rejects.toMatchObject({ code: 'invalid-response' } satisfies Partial<NexusClientError>)

    await expect(createClient({
      baseUrl: stub.baseUrl,
      polling: { initialDelayMs: 1, maximumDelayMs: 1, deadlineMs: 4 },
    }).startRun({
      ...request,
      question: 'keep running [timeout]',
    })).rejects.toMatchObject({ code: 'timeout' } satisfies Partial<NexusClientError>)
  })

  it('rejects unsafe HTTP configuration and reports network failures', async () => {
    expect(() => createClient({ baseUrl: 'https://user:secret@example.test' }))
      .toThrowError(NexusClientError)
    expect(() => createClient({ baseUrl: 'http://example.test' }))
      .toThrow(/remote BFF URLs must use HTTPS/)
    expect(() => createClient({
      baseUrl: 'https://example.test',
      localDevelopment: { subject: 'developer' },
    })).toThrow(/loopback/)
    const offlineFetch: typeof fetch = async () => {
      throw new TypeError('synthetic offline transport')
    }
    await expect(createClient({
      baseUrl: stub.baseUrl,
      fetch: offlineFetch,
    }).listRuns()).rejects.toMatchObject({ code: 'network' } satisfies Partial<NexusClientError>)
  })

  it('bounds stalled token acquisition and fetch calls', async () => {
    let tokenAborted = false
    await expect(createClient({
      baseUrl: stub.baseUrl,
      requestTimeoutMs: 5,
      tokenProvider: (signal) => new Promise<string | undefined>(() => {
        signal.addEventListener('abort', () => {
          tokenAborted = true
        })
      }),
    }).listRuns()).rejects.toMatchObject(
      { code: 'timeout' } satisfies Partial<NexusClientError>,
    )
    expect(tokenAborted).toBe(true)

    const stalledFetch: typeof fetch = () => new Promise<Response>(() => {})
    await expect(createClient({
      baseUrl: stub.baseUrl,
      requestTimeoutMs: 5,
      fetch: stalledFetch,
    }).listRuns()).rejects.toMatchObject(
      { code: 'timeout' } satisfies Partial<NexusClientError>,
    )

    const stalledBodyFetch: typeof fetch = async () => new Response(
      new ReadableStream({ start: () => undefined }),
      { status: 200, headers: { 'Content-Type': 'application/json' } },
    )
    await expect(createClient({
      baseUrl: stub.baseUrl,
      requestTimeoutMs: 5,
      fetch: stalledBodyFetch,
    }).listRuns()).rejects.toMatchObject(
      { code: 'timeout' } satisfies Partial<NexusClientError>,
    )
  })

  it('aborts and discards an allowed not-found body', async () => {
    let bodyCancelled = false
    const notFoundFetch: typeof fetch = async () => new Response(
      new ReadableStream({
        cancel: () => {
          bodyCancelled = true
        },
      }),
      { status: 404, headers: { 'Content-Type': 'application/problem+json' } },
    )
    const client = createClient({ baseUrl: stub.baseUrl, fetch: notFoundFetch })

    expect(await client.getRun('missing')).toBeUndefined()
    await Promise.resolve()
    expect(bodyCancelled).toBe(true)
  })
})
