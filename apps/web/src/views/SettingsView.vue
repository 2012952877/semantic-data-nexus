<script setup lang="ts">
import { inject, onMounted, ref } from 'vue'

import { nexusClientKey } from '@/api/clientContext'
import type { ComponentStatus } from '@/domain'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const statuses = ref<ComponentStatus[]>([])

onMounted(async () => {
  statuses.value = await client.getComponentStatus()
})
</script>

<template>
  <div class="page-shell">
    <header class="page-heading">
      <div>
        <h1>组件状态</h1>
        <p>只显示提供方、连接方式与健康度。此页面不接收、不展示任何密钥。</p>
      </div>
      <span class="read-only-stamp">只读</span>
    </header>

    <section class="status-board" aria-label="组件健康状态">
      <article v-for="(item, index) in statuses" :key="item.name">
        <span class="status-sequence">{{ String(index + 1).padStart(2, '0') }}</span>
        <div class="health-mark" :class="item.status" aria-hidden="true" />
        <div>
          <h2>{{ item.name }}</h2>
          <p>{{ item.provider }}</p>
        </div>
        <strong>{{ item.status === 'healthy' ? '正常' : '隔离模拟' }}</strong>
        <small>{{ item.detail }}</small>
      </article>
    </section>

    <aside class="security-note">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M12 3 5 6v5c0 4.6 2.9 8.3 7 10 4.1-1.7 7-5.4 7-10V6l-7-3Z" />
        <path d="m9 12 2 2 4-4" />
      </svg>
      <div>
        <h2>严格 Mock 边界</h2>
        <p>所有状态均为确定性本地夹具；不会调用 Azure、DuckDB 服务或任何外部模型端点。</p>
      </div>
    </aside>
  </div>
</template>
