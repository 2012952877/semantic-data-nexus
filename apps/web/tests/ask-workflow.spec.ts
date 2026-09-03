import { flushPromises, mount } from '@vue/test-utils'
import { vi } from 'vitest'

import AskView from '@/views/AskView.vue'
import { nexusClientKey } from '@/api/clientContext'
import { MockSemanticNexusClient } from '@/api/mockSemanticNexusClient'

const mountAsk = () =>
  mount(AskView, {
    attachTo: document.body,
    global: {
      provide: {
        [nexusClientKey as symbol]: new MockSemanticNexusClient(20, false),
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
})
