<script setup lang="ts">
import type { ResultSet } from '@/domain'
import { formatResultValue } from './resultFormatting'

defineProps<{ result: ResultSet }>()
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
            {{ formatResultValue(row[column.key] ?? null, column) }}
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
