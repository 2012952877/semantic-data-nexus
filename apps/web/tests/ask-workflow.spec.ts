import { flushPromises, mount } from '@vue/test-utils'
import { vi } from 'vitest'

import AskView from '@/views/AskView.vue'
import { nexusClientKey } from '@/api/clientContext'
import { MockSemanticNexusClient } from '@/api/mockSemanticNexusClient'
import { createSeedRun } from '@/api/mockFixtures'
import type { SemanticNexusClient } from '@/api/semanticNexusClient'
import type { AskRequest, Run } from '@/domain'

const mountAsk = (
  client: SemanticNexusClient = new MockSemanticNexusClient(20, false),
) =>
  mount(AskView, {
    attachTo: document.body,
    global: {
      provide: {
        [nexusClientKey as symbol]: client,
      },
      stubs: {
        RouterLink: {
          props: ['to'],
          template: '<a :href="to"><slot /></a>',
        },
      },
    },
  })

const askQuestion = async (wrapper: ReturnType<typeof mountAsk>) => {
  await wrapper.get('textarea').setValue('比较各区域第二季度净销售额与目标')
  await wrapper.get('form').trigger('submit')
  await flushPromises()
}

const deferred = <T>() => {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise
    reject = rejectPromise
  })
  return { promise, resolve, reject }
}

describe('ask workflow', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('progresses through governed stages and returns a result', async () => {
    const wrapper = mountAsk()
    expect(wrapper.text()).toContain('区域销售 · v1.4')
    expect(wrapper.text()).toContain('Nexus Planner Small')
    expect(wrapper.text()).toContain('Mock 受控编译')
    await askQuestion(wrapper)

    expect(wrapper.text()).toContain('运行中')
    await vi.runAllTimersAsync()
    await flushPromises()

    expect(wrapper.text()).toContain('结果已提交')
    expect(wrapper.get('table').text()).toContain('华东')
    expect(wrapper.get('table').text()).toContain('目标达成率')
  })

  it('cancels an active run without committing remaining stages', async () => {
    const wrapper = mountAsk()
    await askQuestion(wrapper)
    await wrapper.get('button.secondary-button').trigger('click')
    await vi.runAllTimersAsync()
    await flushPromises()

    expect(wrapper.text()).toContain('运行已停止')
    expect(wrapper.text()).toContain('没有执行或提交剩余阶段')
  })

  it('explains an empty result and moves focus to the outcome', async () => {
    const wrapper = mountAsk()
    await wrapper.get('input[value="empty"]').setValue()
    await askQuestion(wrapper)
    await vi.runAllTimersAsync()
    await flushPromises()

    const outcome = wrapper.get('[role="status"][tabindex="-1"]')
    expect(outcome.text()).toContain('合成数据覆盖范围')
    expect(document.activeElement).toBe(outcome.element)
  })

  it('shows an actionable failure and retry control', async () => {
    const wrapper = mountAsk()
    await wrapper.get('input[value="failure"]').setValue()
    await askQuestion(wrapper)
    await vi.runAllTimersAsync()
    await flushPromises()

    const alert = wrapper.get('[role="alert"]')
    expect(alert.text()).toContain('销售快照暂不可用')
    expect(alert.text()).toContain('改用 2025-Q2')
    expect(alert.get('button').text()).toContain('重试')
  })

  it('provides a programmatic label for the question input', () => {
    const wrapper = mountAsk()
    const label = wrapper.get('label[for="question"]')
    const textarea = wrapper.get('#question')

    expect(label.text()).toBe('你想了解什么？')
    expect(textarea.attributes('required')).toBeDefined()
  })

  it('offers only the supported query example and states HTTP demo boundaries', async () => {
    const client: SemanticNexusClient = {
      mode: 'http',
      listRuns: vi.fn(),
      getRun: vi.fn(),
      startRun: vi.fn(),
      cancelRun: vi.fn(),
      getOntology: vi.fn(),
      getComponentStatus: vi.fn(),
    }
    const wrapper = mountAsk(client)
    const examples = wrapper.findAll('.example-strip button')

    expect(examples.map((example) => example.text())).toEqual(['按区域和季度汇总利润'])
    expect(wrapper.get('textarea').attributes('placeholder')).toContain('区域和季度')
    expect(wrapper.get('[role="note"]').text()).toContain('合成业务数据')
    expect(wrapper.get('[role="note"]').text()).toContain('不在本版范围内')
    await examples[0]!.trigger('click')
    expect(wrapper.get<HTMLTextAreaElement>('textarea').element.value).toBe('按区域和季度汇总利润')
    expect(client.startRun).not.toHaveBeenCalled()
  })

  it('recovers controls and focuses an actionable client error', async () => {
    const client = new MockSemanticNexusClient(20, false)
    vi.spyOn(client, 'startRun').mockRejectedValue(new Error(
      '无法保存本地运行历史。请释放浏览器存储空间或允许本地存储后重试。',
    ))
    const wrapper = mountAsk(client)

    await askQuestion(wrapper)

    const alert = wrapper.get('[role="alert"]')
    const submit = wrapper.get('button.primary-button')
    expect(alert.text()).toContain('无法保存本地运行记录')
    expect(alert.text()).toContain('释放浏览器存储空间')
    expect(submit.attributes('disabled')).toBeUndefined()
    expect(document.activeElement).toBe(alert.element)
  })

  it('keeps an active run visible when cancellation delivery fails', async () => {
    const client = new MockSemanticNexusClient(20, false)
    vi.spyOn(client, 'cancelRun').mockRejectedValue(new Error('取消端点暂不可用。'))
    const wrapper = mountAsk(client)
    await askQuestion(wrapper)
    expect(wrapper.find('button.secondary-button').exists()).toBe(true)

    await wrapper.get('button.secondary-button').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('原运行仍在继续')
    expect(wrapper.text()).toContain('取消端点暂不可用')
    expect(wrapper.get('button.secondary-button').text()).toContain('取消运行')

    await vi.runAllTimersAsync()
    await flushPromises()
    expect(wrapper.text()).toContain('结果已提交')
    expect(wrapper.text()).toContain('取消端点暂不可用')
    expect(wrapper.text()).toContain('运行已自行结束')
    expect(wrapper.text()).not.toContain('原运行仍在继续')
  })

  it.each(['success', 'error'] as const)(
    'ignores delayed old-run cancellation %s after a new run starts',
    async (outcome) => {
      const question = '比较各区域第二季度净销售额与目标'
      const oldActive: Run = {
        ...createSeedRun('run_old', question, 'succeeded', 0),
        state: 'running',
        completedAt: undefined,
        result: undefined,
        manifest: undefined,
      }
      const oldCompleted = createSeedRun('run_old', question, 'succeeded', 0)
      const newRun = createSeedRun('run_new', '新的运行问题', 'succeeded', 0)
      const oldRunCompletion = deferred<Run>()
      const cancellation = deferred<Run>()
      let startCalls = 0
      const client: SemanticNexusClient = {
        mode: 'http',
        listRuns: vi.fn(),
        getRun: vi.fn(),
        startRun: vi.fn(async (_request, onProgress) => {
          startCalls += 1
          if (startCalls === 1) {
            onProgress?.(oldActive)
            return oldRunCompletion.promise
          }
          return newRun
        }),
        cancelRun: vi.fn(() => cancellation.promise),
        getOntology: vi.fn(),
        getComponentStatus: vi.fn(),
      }
      const wrapper = mountAsk(client)
      await askQuestion(wrapper)
      await wrapper.get('button.secondary-button').trigger('click')

      oldRunCompletion.resolve(oldCompleted)
      await flushPromises()
      await wrapper.get('textarea').setValue('新的运行问题')
      await wrapper.get('form').trigger('submit')
      await flushPromises()
      expect(wrapper.text()).toContain(newRun.id)

      if (outcome === 'success') {
        cancellation.resolve({
          ...oldCompleted,
          state: 'canceled',
        })
      } else {
        cancellation.reject(new Error('旧运行取消失败'))
      }
      await flushPromises()

      expect(wrapper.text()).toContain(newRun.id)
      expect(wrapper.text()).not.toContain('旧运行取消失败')
      expect(wrapper.text()).not.toContain('运行已停止')
    },
  )

  it('does not offer cancellation before the BFF returns a run ID', async () => {
    const client: SemanticNexusClient = {
      mode: 'http',
      listRuns: vi.fn(),
      getRun: vi.fn(),
      startRun: vi.fn(() => new Promise<Run>(() => undefined)),
      cancelRun: vi.fn(),
      getOntology: vi.fn(),
      getComponentStatus: vi.fn(),
    }
    const wrapper = mountAsk(client)
    await askQuestion(wrapper)

    expect(wrapper.text()).toContain('正在编排')
    expect(wrapper.find('button.secondary-button').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('区域销售 · v1.4')
    expect(wrapper.text()).not.toContain('Nexus Planner Small')
    expect(wrapper.findAll('.scope-ledger dd').map((item) => item.text())).toEqual([
      'unknown',
      'unknown',
      'unknown',
      'unknown',
      '受控执行',
      '表格',
    ])
  })

  it('keeps a known HTTP run actionable and resumes it after status loss', async () => {
    const question = '比较各区域第二季度净销售额与目标'
    const active: Run = {
      ...createSeedRun('run_known_active', question, 'succeeded', 0),
      state: 'running',
      completedAt: undefined,
      result: undefined,
      manifest: undefined,
    }
    const completed = createSeedRun('run_known_active', question, 'succeeded', 0)
    let calls = 0
    const startRun: SemanticNexusClient['startRun'] = vi.fn(async (
      _request: AskRequest,
      onProgress?: (run: Run) => void,
    ) => {
      calls += 1
      if (calls === 1) {
        onProgress?.(active)
        throw new Error('轮询连接中断。')
      }
      return completed
    })
    const client: SemanticNexusClient = {
      mode: 'http',
      listRuns: vi.fn(),
      getRun: vi.fn(),
      startRun,
      cancelRun: vi.fn(),
      getOntology: vi.fn(),
      getComponentStatus: vi.fn(),
    }
    const wrapper = mountAsk(client)
    await askQuestion(wrapper)

    expect(wrapper.text()).toContain(active.id)
    expect(wrapper.text()).toContain('状态暂不可用')
    expect(wrapper.text()).toContain('运行可能继续')
    expect(wrapper.get('button.secondary-button').text()).toContain('取消运行')
    await wrapper.get('.inline-outcome button').trigger('click')
    await flushPromises()

    expect(startRun).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('结果已提交')
  })

  it('uses BFF zero-row diagnostics without Mock coverage claims', async () => {
    const empty: Run = {
      ...createSeedRun('run_http_empty', '比较各区域第二季度净销售额与目标', 'empty', 0),
      diagnostics: [{
        code: 'NO_MATCHES',
        title: '查询完成，但没有匹配行',
        message: '当前筛选条件没有返回数据。',
        recovery: '请检查筛选条件或选择其他期间。',
        severity: 'info',
      }],
      result: {
        columns: [],
        rows: [],
        rowCount: 0,
        coverage: '已返回全部 0 行。',
      },
    }
    const client: SemanticNexusClient = {
      mode: 'http',
      listRuns: vi.fn(),
      getRun: vi.fn(),
      startRun: vi.fn().mockResolvedValue(empty),
      cancelRun: vi.fn(),
      getOntology: vi.fn(),
      getComponentStatus: vi.fn(),
    }
    const wrapper = mountAsk(client)
    await askQuestion(wrapper)

    expect(wrapper.text()).toContain('当前筛选条件没有返回数据')
    expect(wrapper.text()).not.toContain('2024')
    expect(wrapper.text()).not.toContain('合成数据')
  })
})
