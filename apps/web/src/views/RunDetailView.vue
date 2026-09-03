<script setup lang="ts">
import { inject, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { nexusClientKey } from '@/api/clientContext'
import RunDetailContent from '@/components/RunDetailContent.vue'
import type { Run } from '@/domain'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const route = useRoute()
const run = ref<Run>()
const loading = ref(true)

const load = async () => {
  loading.value = true
  run.value = await client.getRun(String(route.params.id))
  loading.value = false
}

onMounted(load)
watch(() => route.params.id, load)
</script>

<template>
  <div v-if="loading" class="page-shell" aria-live="polite">正在读取运行记录…</div>
  <RunDetailContent v-else-if="run" :run="run" />
  <div v-else class="page-shell not-found" role="alert">
    <h1>找不到这条运行记录</h1>
    <p>它可能来自其他浏览器，或本地 Mock 历史已被清除。</p>
    <RouterLink class="primary-button" to="/runs">返回运行记录</RouterLink>
  </div>
</template>
