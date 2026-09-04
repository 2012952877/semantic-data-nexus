// @vitest-environment node

import { HttpSemanticNexusClient, NexusClientError } from '@/api/httpSemanticNexusClient'
import { startHttpStubServer, type StubRequest } from './httpStubServer'

let stub: Awaited<ReturnType<typeof startHttpStubServer>>

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
  requestId: () => 'request-unit-001',
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

    const failed = await client.startRun({ ...request, question: '触发执行失败' })
    expect(failed.state).toBe('failed')
    expect(failed.diagnostics[0]?.code).toBe('STUB_SOURCE_UNAVAILABLE')
  })

  it('delivers cancellation and returns the terminal detail', async () => {
    const client = createClient()
    let releaseId: ((id: string) => void) | undefined
    const id = new Promise<string>((resolve) => {
      releaseId = resolve
    })
    const active = client.startRun(request, (run) => releaseId?.(run.id))
    const cancelled = await client.cancelRun(await id)

    expect(cancelled.state).toBe('canceled')
    expect((await active).state).toBe('canceled')
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
