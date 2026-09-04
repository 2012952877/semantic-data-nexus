<script setup lang="ts">
import { inject, nextTick, ref } from 'vue'

import { nexusClientKey } from '@/api/clientContext'
import ResultTable from '@/components/ResultTable.vue'
import StageProgress from '@/components/StageProgress.vue'
import type { AskRequest, MockScenario, Run } from '@/domain'
import { createStages } from '@/api/mockFixtures'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const examples = [
  '比较各区域第二季度净销售额、目标达成率和同比',
  '哪些区域的销售额低于目标，但同比仍在增长？',
  '按区域汇总 2025 年上半年的净销售额',
]

const question = ref('')
const scenario = ref<MockScenario>('success')
const currentRun = ref<Run>()
const submitting = ref(false)
const alertPanel = ref<HTMLElement>()
const formError = ref('')
const cancelError = ref('')
const isMock = client.mode === 'mock'

const requestForCurrentInput = (): AskRequest => ({
  question: question.value.trim(),
  scenario: scenario.value,
  model: 'Nexus Planner Small',
  executionMode: '受控执行',
  outputMode: '表格',
})

const submit = async () => {
  if (!question.value.trim() || submitting.value) return
  submitting.value = true
  currentRun.value = undefined
  formError.value = ''
  cancelError.value = ''
  try {
    const result = await client.startRun(requestForCurrentInput(), (run) => {
      currentRun.value = run
      cancelError.value = ''
    })
    currentRun.value = result
    if (result.state === 'failed' || result.state === 'empty') {
      await nextTick()
      alertPanel.value?.focus()
    }
  } catch (error) {
    currentRun.value = undefined
    formError.value = error instanceof Error
      ? error.message
      : '运行未能启动。请检查客户端配置和服务连接后重试。'
    await nextTick()
    alertPanel.value?.focus()
  } finally {
    submitting.value = false
  }
}

const cancel = async () => {
  if (!currentRun.value) return
  try {
    currentRun.value = await client.cancelRun(currentRun.value.id)
  } catch (error) {
    if (submitting.value
      && (currentRun.value?.state === 'queued' || currentRun.value?.state === 'running')) {
      cancelError.value = error instanceof Error
        ? error.message
        : '取消请求未能送达。请稍后重试。'
    }
  }
}

const retry = () => submit()

const useExample = (example: string) => {
  question.value = example
}
</script>

<template>
  <div class="ask-page">
    <section class="ask-composer" aria-labelledby="ask-heading">
      <div class="ask-intro">
        <h1 id="ask-heading">把业务问题编排为可信结果</h1>
        <p>自然语言负责表达意图；类型、策略与执行边界由 Semantic Nexus 接管。</p>
      </div>

      <form class="question-form" @submit.prevent="submit">
        <label for="question">你想了解什么？</label>
        <textarea
          id="question"
          v-model="question"
          rows="5"
          placeholder="例如：比较各区域第二季度净销售额、目标达成率和同比"
          :disabled="submitting"
          required
        />
        <div class="example-strip" aria-label="示例问题">
          <span>安全示例</span>
          <button
            v-for="example in examples"
            :key="example"
            type="button"
            :disabled="submitting"
            @click="useExample(example)"
          >
            {{ example }}
          </button>
        </div>

        <div class="scope-ledger">
          <dl>
            <div><dt>语义范围</dt><dd>区域销售 · v1.4</dd></div>
            <div><dt>成员解析</dt><dd>已发布成员优先</dd></div>
            <div><dt>模型</dt><dd>Nexus Planner Small</dd></div>
            <div><dt>执行方式</dt><dd>受控执行</dd></div>
            <div><dt>输出</dt><dd>表格</dd></div>
          </dl>
        </div>

        <details v-if="isMock" class="scenario-settings">
          <summary>Mock 场景与执行设置</summary>
          <fieldset>
            <legend>模拟场景</legend>
            <label><input v-model="scenario" type="radio" value="success" /> 有结果</label>
            <label><input v-model="scenario" type="radio" value="empty" /> 0 行</label>
            <label><input v-model="scenario" type="radio" value="failure" /> 执行失败</label>
          </fieldset>
        </details>

        <div class="composer-actions">
          <p>
            <strong>治理边界：</strong>
            模型只提出类型化 SQG，绝不直接执行模型生成的 SQL。
          </p>
          <div>
            <button
              v-if="submitting"
              class="secondary-button"
              type="button"
              @click="cancel"
            >
              取消运行
            </button>
            <button class="primary-button" type="submit" :disabled="submitting || !question.trim()">
              <span v-if="submitting" class="button-loader" aria-hidden="true" />
              {{ submitting ? '正在编排' : '开始受控运行' }}
            </button>
          </div>
        </div>
      </form>
    </section>

    <aside class="execution-pane" aria-labelledby="execution-heading">
      <div class="execution-heading">
        <div>
          <h2 id="execution-heading">执行脊柱</h2>
          <p>{{ currentRun ? currentRun.id : '等待问题进入编译器' }}</p>
        </div>
        <span class="live-label" :class="{ active: submitting }">
          {{ submitting ? 'LIVE' : 'READY' }}
        </span>
      </div>

      <StageProgress :stages="currentRun?.stages ?? createStages()" />

      <div v-if="cancelError" class="inline-outcome outcome-error" role="alert">
        <span>取消未送达</span>
        <h3>原运行仍在继续</h3>
        <p>{{ cancelError }}</p>
        <strong>可以再次取消，或等待当前运行完成。</strong>
      </div>

      <div
        v-if="formError"
        ref="alertPanel"
        class="inline-outcome outcome-error"
        role="alert"
        tabindex="-1"
      >
        <span>运行未启动</span>
        <h3>{{ isMock ? '无法保存本地运行记录' : '无法连接运行服务' }}</h3>
        <p>{{ formError }}</p>
        <strong v-if="isMock">释放浏览器存储空间或允许本地存储，然后重试。</strong>
        <strong v-else>检查客户端模式、BFF 地址和网络连接，然后重试。</strong>
        <button class="secondary-button" type="button" @click="retry">重试当前问题</button>
      </div>

      <div v-else-if="!currentRun" class="run-placeholder">
        <svg viewBox="0 0 120 90" aria-hidden="true">
          <path d="M10 15h100M10 45h100M10 75h100M28 15v60M60 15v60M92 15v60" />
          <circle cx="28" cy="15" r="5" /><circle cx="60" cy="45" r="5" /><circle cx="92" cy="75" r="5" />
        </svg>
        <h3>一条可审查的运行记录会出现在这里</h3>
        <p>初始化、编译、优化、执行和生成不会被合并成一个不透明的加载状态。</p>
      </div>

      <div
        v-else-if="currentRun.state === 'failed'"
        ref="alertPanel"
        class="inline-outcome outcome-error"
        role="alert"
        tabindex="-1"
      >
        <span>执行未提交</span>
        <h3>{{ currentRun.diagnostics[0]?.title }}</h3>
        <p>{{ currentRun.diagnostics[0]?.message }}</p>
        <strong>{{ currentRun.diagnostics[0]?.recovery }}</strong>
        <button class="secondary-button" type="button" @click="retry">重试当前问题</button>
      </div>

      <div
        v-else-if="currentRun.state === 'canceled'"
        class="inline-outcome"
        role="status"
      >
        <span>运行已停止</span>
        <h3>没有执行或提交剩余阶段</h3>
        <p>问题仍保留在输入框中，可以调整后重新开始。</p>
        <button class="secondary-button" type="button" @click="retry">重新运行</button>
      </div>

      <div
        v-else-if="currentRun.state === 'empty'"
        ref="alertPanel"
        class="inline-outcome"
        role="status"
        tabindex="-1"
      >
        <span>结果 · 0 行</span>
        <h3>该期间超出合成数据覆盖范围</h3>
        <p>{{ currentRun.result?.coverage }}</p>
        <strong>建议改查 2024 年之后的期间。</strong>
        <RouterLink :to="`/runs/${currentRun.id}`">查看完整运行记录 →</RouterLink>
      </div>

      <div v-else-if="currentRun.state === 'succeeded' && currentRun.result" class="inline-result">
        <div class="inline-result-heading">
          <div>
            <span>结果已提交</span>
            <h3>{{ currentRun.result.rowCount }} 个区域</h3>
          </div>
          <RouterLink :to="`/runs/${currentRun.id}`">检查运行 →</RouterLink>
        </div>
        <ResultTable :result="currentRun.result" />
      </div>
    </aside>
  </div>
</template>
