<script setup lang="ts">
import ResultTable from './ResultTable.vue'
import StageProgress from './StageProgress.vue'
import StatusBadge from './StatusBadge.vue'
import type { Run } from '@/domain'

defineProps<{ run: Run; synthetic?: boolean }>()

const formatDuration = (milliseconds: number) =>
  milliseconds < 1000 ? `${milliseconds}ms` : `${(milliseconds / 1000).toFixed(2)}s`
</script>

<template>
  <div class="run-detail">
    <header class="run-header">
      <div>
        <RouterLink class="back-link" to="/runs">← 返回运行记录</RouterLink>
        <div class="run-title-line">
          <h1>{{ run.question }}</h1>
          <StatusBadge :state="run.state" />
        </div>
        <p class="run-id">
          {{ run.id }}<template v-if="synthetic"> · 合成数据</template>
        </p>
      </div>
      <dl class="run-facts">
        <div><dt>耗时</dt><dd>{{ formatDuration(run.elapsedMs) }}</dd></div>
        <div><dt>Token</dt><dd>{{ run.tokens.input + run.tokens.output }}</dd></div>
        <div><dt>模型</dt><dd>{{ run.model }}</dd></div>
      </dl>
    </header>

    <section class="ledger-section" aria-labelledby="stage-heading">
      <div class="section-heading">
        <div>
          <h2 id="stage-heading">执行账本</h2>
          <p>每一步都有明确输入、状态和耗时。</p>
        </div>
      </div>
      <StageProgress :stages="run.stages" />
    </section>

    <section v-if="run.result?.rowCount" class="ledger-section" aria-labelledby="result-heading">
      <div class="section-heading">
        <div>
          <h2 id="result-heading">已提交结果</h2>
          <p>{{ run.result.coverage }}</p>
        </div>
        <strong>{{ run.result.rowCount }} 行</strong>
      </div>
      <ResultTable :result="run.result" />
    </section>

    <section v-else-if="run.state === 'empty'" class="empty-ledger" role="status">
      <span class="empty-symbol" aria-hidden="true">0</span>
      <div>
        <h2>{{ run.diagnostics[0]?.title ?? '查询正确完成，但没有匹配行' }}</h2>
        <p>{{ run.diagnostics[0]?.message ?? run.result?.coverage }}</p>
        <p>{{ run.diagnostics[0]?.recovery ?? '请检查筛选条件或选择其他期间。' }}</p>
      </div>
    </section>

    <section v-if="run.diagnostics.length" class="diagnostic-panel" aria-labelledby="diagnostics-heading">
      <div class="section-heading">
        <div>
          <h2 id="diagnostics-heading">诊断</h2>
          <p>问题与恢复建议，不隐藏失败原因。</p>
        </div>
      </div>
      <article v-for="diagnostic in run.diagnostics" :key="diagnostic.code">
        <code>{{ diagnostic.code }}</code>
        <h3>{{ diagnostic.title }}</h3>
        <p>{{ diagnostic.message }}</p>
        <strong>{{ diagnostic.recovery }}</strong>
      </article>
    </section>

    <div class="detail-grid">
      <section class="ledger-section" aria-labelledby="plan-heading">
        <div class="section-heading">
          <div>
            <h2 id="plan-heading">节点计划</h2>
            <p>数据如何从来源变成最终字段。</p>
          </div>
        </div>
        <ol class="node-list">
          <li v-for="(node, index) in run.nodes" :key="node.id">
            <span class="node-sequence">{{ String(index + 1).padStart(2, '0') }}</span>
            <div>
              <code>{{ node.kind }}</code>
              <h3>{{ node.label }}</h3>
              <p>{{ node.plainLanguage }}</p>
              <small>输出：{{ node.outputFields.join(' · ') }}</small>
            </div>
          </li>
        </ol>
      </section>

      <div class="detail-stack">
        <section class="ledger-section" aria-labelledby="lineage-heading">
          <div class="section-heading">
            <div>
              <h2 id="lineage-heading">来源与血缘</h2>
              <p>结果使用了哪些数据。</p>
            </div>
          </div>
          <div class="source-list">
            <article v-for="source in run.lineage.sources" :key="source.id">
              <span>{{ source.kind }}</span>
              <h3>{{ source.name }}</h3>
              <p>{{ source.contribution }}</p>
              <small>{{ source.freshness }}</small>
            </article>
          </div>
        </section>

        <section v-if="run.manifest" class="manifest" aria-labelledby="manifest-heading">
          <h2 id="manifest-heading">提交清单</h2>
          <dl>
            <div><dt>格式</dt><dd>{{ run.manifest.format }}</dd></div>
            <div><dt>校验</dt><dd>{{ run.manifest.checksum }}</dd></div>
            <div><dt>位置</dt><dd><code>{{ run.manifest.uri }}</code></dd></div>
          </dl>
        </section>

        <details class="technical-details">
          <summary>查看类型化 SQG 摘要</summary>
          <dl>
            <div><dt>意图</dt><dd>{{ run.sqg.intent }}</dd></div>
            <div><dt>指标</dt><dd>{{ run.sqg.metrics.join(' · ') }}</dd></div>
            <div><dt>维度</dt><dd>{{ run.sqg.dimensions.join(' · ') }}</dd></div>
            <div><dt>策略</dt><dd>{{ run.sqg.policyChecks.join('；') }}</dd></div>
          </dl>
        </details>
      </div>
    </div>
  </div>
</template>
