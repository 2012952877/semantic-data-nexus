<script setup lang="ts">
import type { ResultColumn, ResultSet } from '@/domain'

defineProps<{ result: ResultSet }>()

const formatValue = (value: string | number, column: ResultColumn) => {
  if (column.format === 'currency' && typeof value === 'number') {
    return new Intl.NumberFormat('zh-CN', {
      style: 'currency',
      currency: 'CNY',
      maximumFractionDigits: 0,
    }).format(value)
  }
  if (column.format === 'percent' && typeof value === 'number') {
    return new Intl.NumberFormat('zh-CN', {
      style: 'percent',
      minimumFractionDigits: 1,
      maximumFractionDigits: 1,
    }).format(value)
  }
  return String(value)
}
</script>

<template>
  <div class="table-wrap">
    <table>
      <caption class="sr-only">查询结果，共 {{ result.rowCount }} 行</caption>
      <thead>
        <tr>
          <th v-for="column in result.columns" :key="column.key" scope="col">
            {{ column.label }}
          </th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(row, index) in result.rows" :key="index">
          <td v-for="column in result.columns" :key="column.key">
            {{ formatValue(row[column.key] ?? '—', column) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
