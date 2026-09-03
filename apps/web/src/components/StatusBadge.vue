<script setup lang="ts">
import type { RunState, StageState } from '@/domain'

defineProps<{ state: RunState | StageState }>()

const labels: Record<RunState | StageState, string> = {
  queued: '排队中',
  pending: '待开始',
  running: '运行中',
  succeeded: '已完成',
  empty: '0 行',
  failed: '失败',
  canceled: '已取消',
}
</script>

<template>
  <span class="status-badge" :class="`state-${state}`">
    <span class="status-badge-mark" aria-hidden="true" />
    {{ labels[state] }}
  </span>
</template>
