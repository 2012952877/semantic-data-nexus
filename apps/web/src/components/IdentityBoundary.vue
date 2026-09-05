<script setup lang="ts">
import { inject, onMounted, onUnmounted } from 'vue'
import { browserSessionKey } from '@/api/browserSession'

const session = inject(browserSessionKey, undefined)
const refresh = () => { if (session) void session.refresh() }
const select = (event: Event) => {
  if (event.target instanceof HTMLSelectElement && event.target.value) {
    session?.select(event.target.value)
  }
}
onMounted(() => {
  refresh()
  window.addEventListener('focus', refresh)
})
onUnmounted(() => window.removeEventListener('focus', refresh))
</script>

<template>
  <template v-if="session">
    <section class="identity-boundary" aria-label="账户与工作区">
      <p v-if="session.state.loading" role="status">正在确认登录与工作区权限…</p>
      <p v-if="session.state.error" role="alert">{{ session.state.error }}</p>
      <button v-if="session.state.error" class="button-secondary" @click="refresh">重试连接</button>
      <template v-if="!session.state.loading && !session.state.authenticated && !session.state.error">
        <h1>登录工作区</h1>
        <p>使用已登记的企业身份登录。工作区权限由服务端确认，不接受自报的角色或组织信息。</p>
        <a v-for="provider in session.state.providers" :key="provider" class="button-primary"
           :href="`/auth/login/${encodeURIComponent(provider)}`">使用 {{ provider }} 登录</a>
      </template>
      <template v-if="session.state.authenticated">
        <span>{{ session.state.name }}</span>
        <label for="workspace-selector">工作区</label>
        <select id="workspace-selector" :value="session.state.workspaceId" @change="select">
          <option value="" disabled>选择已授权的工作区</option>
          <option v-for="workspace in session.state.workspaces" :key="workspace.workspaceId" :value="workspace.workspaceId">
            {{ workspace.name }} · {{ workspace.tenantId }}
          </option>
        </select>
        <button class="button-secondary" @click="session.logout()">退出此应用</button>
        <p v-if="session.state.workspaces.length === 0" role="status">
          登录成功，但当前账户没有有效工作区权限。请联系工作区管理员。
        </p>
      </template>
    </section>
    <slot v-if="session.state.authenticated && session.state.workspaceId && !session.state.error" />
  </template>
  <slot v-else />
</template>

<style scoped>
.identity-boundary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 1rem;
  padding: 1rem;
  border-bottom: 1px solid var(--rule-line, #c9cdc3);
}
.identity-boundary h1, .identity-boundary p { flex-basis: 100%; }
.identity-boundary select { max-width: 100%; min-height: 2.75rem; font: inherit; }
.identity-boundary a { display: inline-block; padding: 0.75rem; }
</style>
