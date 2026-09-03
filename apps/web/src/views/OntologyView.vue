<script setup lang="ts">
import { inject, onMounted, ref } from 'vue'

import { nexusClientKey } from '@/api/clientContext'
import type { Ontology } from '@/domain'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const ontology = ref<Ontology>()
const selectedEntity = ref('sales')

onMounted(async () => {
  ontology.value = await client.getOntology()
})
</script>

<template>
  <div v-if="ontology" class="page-shell ontology-page">
    <header class="page-heading">
      <div>
        <h1>{{ ontology.name }}</h1>
        <p>语义模型把业务词汇、计算口径和允许查询的边界放在同一个可读契约里。</p>
      </div>
      <span class="version-stamp">v{{ ontology.version }}</span>
    </header>

    <div class="ontology-layout">
      <nav class="entity-index" aria-label="实体列表">
        <strong>实体</strong>
        <button
          v-for="entity in ontology.entities"
          :key="entity.name"
          type="button"
          :class="{ active: selectedEntity === entity.name }"
          @click="selectedEntity = entity.name"
        >
          <span>{{ entity.label }}</span>
          <small>{{ entity.name }}</small>
        </button>
      </nav>

      <div class="ontology-content">
        <section
          v-for="entity in ontology.entities.filter((item) => item.name === selectedEntity)"
          :key="entity.name"
          class="ledger-section entity-sheet"
        >
          <div class="section-heading">
            <div>
              <h2>{{ entity.label }}</h2>
              <p>{{ entity.description }}</p>
            </div>
            <code>{{ entity.name }}</code>
          </div>
          <div class="field-list">
            <article v-for="field in entity.fields" :key="field.name">
              <div><strong>{{ field.name }}</strong><code>{{ field.type }}</code></div>
              <p>{{ field.description }}</p>
            </article>
          </div>
        </section>

        <section class="ledger-section">
          <div class="section-heading">
            <div>
              <h2>已发布指标</h2>
              <p>指标是经过治理的计算口径，不是临时拼出的公式。</p>
            </div>
          </div>
          <div class="metric-list">
            <article v-for="metric in ontology.metrics" :key="metric.name">
              <h3>{{ metric.label }}</h3>
              <p>{{ metric.description }}</p>
              <code>{{ metric.expression }}</code>
            </article>
          </div>
        </section>

        <section class="ontology-bottom">
          <article>
            <h2>关系</h2>
            <p>关系说明不同实体怎样可靠地连接，避免重复计算或错误匹配。</p>
            <dl v-for="relation in ontology.relations" :key="relation.from">
              <div><dt>连接</dt><dd>{{ relation.from }} → {{ relation.to }}</dd></div>
              <div><dt>基数</dt><dd>{{ relation.cardinality }}</dd></div>
              <div><dt>含义</dt><dd>{{ relation.description }}</dd></div>
            </dl>
          </article>
          <article>
            <h2>查询策略</h2>
            <p>{{ ontology.queryPolicy.description }}</p>
            <dl>
              <div><dt>可用维度</dt><dd>{{ ontology.queryPolicy.allowedDimensions.join(' · ') }}</dd></div>
              <div><dt>最大回溯</dt><dd>{{ ontology.queryPolicy.maxLookbackMonths }} 个月</dd></div>
            </dl>
          </article>
        </section>
      </div>
    </div>
  </div>
</template>
