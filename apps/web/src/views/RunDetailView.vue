<script setup lang="ts">
import { inject, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { nexusClientKey } from '@/api/clientContext'
import {
  RUN_STORAGE_CHANGE_EVENT,
  RUN_STORAGE_KEY,
  runStorageKey,
} from '@/api/runStorage'
import RunDetailContent from '@/components/RunDetailContent.vue'
import type { Run } from '@/domain'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const route = useRoute()
const run = ref<Run>()
const loading = ref(true)
const errorMessage = ref('')

const load = async (showLoading = true) => {
  if (showLoading) loading.value = true
  try {
    run.value = await client.getRun(String(route.params.id))
    errorMessage.value = ''
  } catch (error) {
    run.value = undefined
    errorMessage.value = error instanceof Error
      ? error.message
      : '无法读取运行详情。'
  } finally {
    if (showLoading) loading.value = false
  }
}

const handleStorage = (event: StorageEvent) => {
  const id = String(route.params.id)
  if (event.key === RUN_STORAGE_KEY || event.key === runStorageKey(id)) {
    void load(false)
  }
}

const handleLocalStorage = (event: Event) => {
  const detail = (event as CustomEvent<{ id?: string }>).detail
  if (!detail?.id || detail.id === String(route.params.id)) void load(false)
}

onMounted(() => {
  void load()
  window.addEventListener('storage', handleStorage)
  window.addEventListener(RUN_STORAGE_CHANGE_EVENT, handleLocalStorage)
})
onUnmounted(() => {
  window.removeEventListener('storage', handleStorage)
  window.removeEventListener(RUN_STORAGE_CHANGE_EVENT, handleLocalStorage)
})
watch(() => route.params.id, () => load())
</script>

<template>
  <div v-if="loading" class="page-shell" aria-live="polite">正在读取运行记录…</div>
  <RunDetailContent v-else-if="run" :run="run" />
  <div v-else class="page-shell not-found" role="alert">
    <h1>{{ errorMessage ? '无法读取运行详情' : '找不到这条运行记录' }}</h1>
    <p>{{ errorMessage || '这条运行记录可能不存在或已经被清除。' }}</p>
    <RouterLink class="primary-button" to="/runs">返回运行记录</RouterLink>
  </div>
</template>
