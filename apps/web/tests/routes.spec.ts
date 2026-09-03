import { flushPromises, mount } from '@vue/test-utils'
import { createMemoryHistory } from 'vue-router'

import App from '@/App.vue'
import { nexusClientKey } from '@/api/clientContext'
import { MockSemanticNexusClient } from '@/api/mockSemanticNexusClient'
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
})
