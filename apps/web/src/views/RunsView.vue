<script setup lang="ts">
import { inject, onMounted, onUnmounted, ref } from 'vue'

import { nexusClientKey } from '@/api/clientContext'
import {
  RUN_STORAGE_CHANGE_EVENT,
  RUN_STORAGE_KEY,
  RUN_STORAGE_RECORD_PREFIX,
} from '@/api/runStorage'
import StatusBadge from '@/components/StatusBadge.vue'
import type { Run } from '@/domain'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const runs = ref<Run[]>([])
const errorMessage = ref('')

const loadRuns = async () => {
  try {
    runs.value = await client.listRuns()
    errorMessage.value = ''
  } catch (error) {
    runs.value = []
    errorMessage.value = error instanceof Error
      ? error.message
      : '无法读取运行记录。'
  }
}

const handleStorage = (event: StorageEvent) => {
 if (event.key === RUN_STORAGE_KEY || event.key?.startsWith(RUN_STORAGE_RECORD_PREFIX)) {
   void loadRuns()
 }
}

const handleLocalStorage = () => {
 void loadRuns()
}

onMounted(() => {
 void loadRuns()
 window.addEventListener('storage', handleStorage)
 window.addEventListener(RUN_STORAGE_CHANGE_EVENT, handleLocalStorage)
})

onUnmounted(() => {
 window.removeEventListener('storage', handleStorage)
 window.removeEventListener(RUN_STORAGE_CHANGE_EVENT, handleLocalStorage)
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

    <section aria-labelledby="runs-table-heading">
      <h2 id="runs-table-heading" class="sr-only">运行列表</h2>
      <div
        class="runs-ledger"
        role="table"
        aria-labelledby="runs-table-heading"
        aria-colcount="4"
      >
        <div role="rowgroup">
          <div class="runs-ledger-head" role="row">
            <span id="runs-question-heading" role="columnheader">问题 / 运行 ID</span>
            <span id="runs-status-heading" role="columnheader">状态</span>
            <span id="runs-created-heading" role="columnheader">创建时间</span>
            <span id="runs-elapsed-heading" role="columnheader">耗时</span>
          </div>
        </div>
        <div role="rowgroup">
          <div v-for="run in runs" :key="run.id" class="run-row" role="row">
            <span class="run-question" role="cell">
              <RouterLink :to="`/runs/${run.id}`">
                <strong>{{ run.question }}</strong>
                <small>{{ run.id }}</small>
              </RouterLink>
            </span>
            <span role="cell">
              <StatusBadge :state="run.state" />
            </span>
            <time role="cell" :datetime="run.createdAt">
              {{ formatTime(run.createdAt) }}
            </time>
            <span role="cell">
              {{ (run.elapsedMs / 1000).toFixed(2) }}s
            </span>
          </div>
          <div v-if="!runs.length" class="empty-row" role="row">
            <span role="cell" aria-colspan="4">
              {{ errorMessage || '还没有运行记录。' }}
            </span>
          </div>
        </div>
      </div>
    </section>
  </div>
</template>
