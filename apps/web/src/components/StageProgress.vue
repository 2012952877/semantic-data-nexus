<script setup lang="ts">
import { computed } from 'vue'

import StatusBadge from './StatusBadge.vue'
import type { Stage } from '@/domain'

const props = defineProps<{ stages: Stage[] }>()

const formatDuration = (duration?: number) =>
  duration === undefined ? '—' : duration < 1000 ? `${duration}ms` : `${(duration / 1000).toFixed(1)}s`

const liveMessage = computed(() => {
  const active = props.stages.find((stage) => stage.state === 'running')
  if (active) return `当前阶段：${active.label}`
  const failed = props.stages.find((stage) => stage.state === 'failed')
  if (failed) return `运行失败于：${failed.label}`
  if (props.stages.every((stage) => stage.state === 'succeeded')) return '全部五个阶段已完成'
  if (props.stages.some((stage) => stage.state === 'canceled')) return '运行已取消'
  return '运行尚未开始'
})
</script>

<template>
  <p class="sr-only" role="status" aria-live="polite">{{ liveMessage }}</p>
  <ol class="stage-register" aria-label="执行阶段">
    <li v-for="(stage, index) in stages" :key="stage.key" :class="`stage-${stage.state}`">
      <span class="stage-index">{{ String(index + 1).padStart(2, '0') }}</span>
      <span class="stage-copy">
        <strong>{{ stage.label }}</strong>
        <small>{{ stage.description }}</small>
      </span>
      <span class="stage-time">{{ formatDuration(stage.durationMs) }}</span>
      <StatusBadge :state="stage.state" />
    </li>
  </ol>
</template>
