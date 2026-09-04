import { flushPromises, mount } from '@vue/test-utils'
import { vi } from 'vitest'

import AskView from '@/views/AskView.vue'
import { nexusClientKey } from '@/api/clientContext'
import { MockSemanticNexusClient } from '@/api/mockSemanticNexusClient'
import type { SemanticNexusClient } from '@/api/semanticNexusClient'
import type { Run } from '@/domain'

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

describe('ask workflow', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('progresses through governed stages and returns a result', async () => {
    const wrapper = mountAsk()
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
    expect(outcome.text()).toContain('超出合成数据覆盖范围')
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
  })
})
