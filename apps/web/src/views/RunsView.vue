<script setup lang="ts">
import { inject, onMounted, ref } from 'vue'

import { nexusClientKey } from '@/api/clientContext'
import StatusBadge from '@/components/StatusBadge.vue'
import type { Run } from '@/domain'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const runs = ref<Run[]>([])

onMounted(async () => {
  runs.value = await client.listRuns()
})

const formatTime = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
</script>

<template>
  <div class="page-shell">
    <header class="page-heading">
      <div>
        <h1>运行记录</h1>
        <p>每一次提问都保留阶段、计划、结果与诊断，不把执行藏在聊天记录里。</p>
      </div>
      <RouterLink class="primary-button" to="/ask">提出新问题</RouterLink>
    </header>

    <section class="runs-ledger" aria-labelledby="runs-table-heading">
      <h2 id="runs-table-heading" class="sr-only">运行列表</h2>
      <div class="runs-ledger-head" aria-hidden="true">
        <span>问题 / 运行 ID</span><span>状态</span><span>创建时间</span><span>耗时</span>
      </div>
      <RouterLink v-for="run in runs" :key="run.id" :to="`/runs/${run.id}`" class="run-row">
        <span class="run-question">
          <strong>{{ run.question }}</strong>
          <small>{{ run.id }}</small>
        </span>
        <StatusBadge :state="run.state" />
        <time :datetime="run.createdAt">{{ formatTime(run.createdAt) }}</time>
        <span>{{ (run.elapsedMs / 1000).toFixed(2) }}s</span>
      </RouterLink>
      <div v-if="!runs.length" class="empty-row">还没有运行记录。</div>
    </section>
  </div>
</template>
