import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'

import App from '@/App.vue'
import { nexusClientKey } from '@/api/clientContext'
import { createSeedRun } from '@/api/mockFixtures'
import { MockSemanticNexusClient } from '@/api/mockSemanticNexusClient'
import {
  RUN_STORAGE_CHANGE_EVENT,
  runStorageKey,
} from '@/api/runStorage'
import type { SemanticNexusClient } from '@/api/semanticNexusClient'
import { createNexusRouter } from '@/router'

describe('route smoke tests', () => {
  it.each([
    ['/ask', '把业务问题编排为可信结果'],
    ['/runs', '运行记录'],
    ['/runs/run-syn-1001', '比较各区域第二季度净销售额、目标达成率和同比'],
    ['/ontology', '区域销售语义模型'],
    ['/settings', '组件状态'],
  ])('renders %s', async (path, expectedHeading) => {
    const router = createNexusRouter(createMemoryHistory())
    await router.push(path)
    await router.isReady()

    const wrapper = mount(App, {
      global: {
        plugins: [router],
        provide: {
          [nexusClientKey as symbol]: new MockSemanticNexusClient(1),
        },
      },
    })
    await flushPromises()

    expect(wrapper.get('main').text()).toContain(expectedHeading)
    wrapper.unmount()
  })

  it('exposes run history with table, header, row, and cell semantics', async () => {
    const router = createNexusRouter(createMemoryHistory())
    await router.push('/runs')
    await router.isReady()
    const wrapper = mount(App, {
      global: {
        plugins: [router],
        provide: {
          [nexusClientKey as symbol]: new MockSemanticNexusClient(1),
        },
      },
    })
    await flushPromises()

    expect(wrapper.get('[role="table"]').attributes('aria-colcount')).toBe('4')
    expect(wrapper.findAll('[role="columnheader"]')).toHaveLength(4)
    const dataRow = wrapper.findAll('[role="row"]').find((row) =>
      row.text().includes('run-syn-1001'))
    expect(dataRow?.findAll('[role="cell"]')).toHaveLength(4)
    expect(dataRow?.findAll('[role="cell"]')[1]?.text()).toContain('已完成')
    wrapper.unmount()
  })

  it('refreshes run detail for local and cross-tab persistence events', async () => {
    const first = createSeedRun('run-syn-live-detail', '实时详情', 'succeeded', 1)
    const second = { ...first, state: 'failed' as const }
    const third = { ...first, state: 'canceled' as const }
    const getRun = vi.fn()
      .mockResolvedValueOnce(first)
      .mockResolvedValueOnce(second)
      .mockResolvedValueOnce(third)
    const client: SemanticNexusClient = {
      listRuns: vi.fn(),
      getRun,
      startRun: vi.fn(),
      cancelRun: vi.fn(),
      getOntology: vi.fn(),
      getComponentStatus: vi.fn(),
    }
    const router = createNexusRouter(createMemoryHistory())
    await router.push('/runs/run-syn-live-detail')
    await router.isReady()
    const wrapper = mount(App, {
      global: {
        plugins: [router],
        provide: { [nexusClientKey as symbol]: client },
      },
    })
    await flushPromises()

    window.dispatchEvent(new CustomEvent(RUN_STORAGE_CHANGE_EVENT, {
      detail: { id: first.id, newValue: '{}', sourceId: 'client-a' },
    }))
    await flushPromises()
    expect(wrapper.text()).toContain('失败')

    window.dispatchEvent(new StorageEvent('storage', {
      key: runStorageKey(first.id),
      newValue: '{}',
    }))
    await flushPromises()
    expect(wrapper.text()).toContain('已取消')
    expect(getRun).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })
})
