<script setup lang="ts">
import { computed, inject } from 'vue'
import { useRoute } from 'vue-router'

import { nexusClientKey } from '@/api/clientContext'

const client = inject(nexusClientKey)
if (!client) throw new Error('SemanticNexusClient is not provided')

const route = useRoute()
const title = computed(() => String(route.meta.title ?? 'Semantic Nexus'))
const isMock = client.mode === 'mock'

const navItems = [
  { to: '/ask', label: '提问', icon: 'ask' },
  { to: '/runs', label: '运行', icon: 'runs' },
  { to: '/ontology', label: '语义', icon: 'ontology' },
  { to: '/settings', label: '状态', icon: 'settings' },
]
</script>

<template>
  <div class="app-frame">
    <a class="skip-link" href="#main-content">跳到主要内容</a>
    <aside class="rail" aria-label="主导航">
      <RouterLink class="brand-mark" to="/ask" aria-label="Semantic Nexus 首页">
        <svg viewBox="0 0 32 32" aria-hidden="true">
          <path d="M5 7h8v8H5zM19 7h8v8h-8zM12 19h8v8h-8z" />
          <path d="M13 11h6M16 15v4" fill="none" />
        </svg>
        <span>SN</span>
      </RouterLink>
      <nav class="rail-nav">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :aria-label="item.label"
        >
          <svg v-if="item.icon === 'ask'" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 4h14v12H9l-4 4V4Z" />
            <path d="M8 8h8M8 12h5" />
          </svg>
          <svg v-else-if="item.icon === 'runs'" viewBox="0 0 24 24" aria-hidden="true">
            <path d="M5 5h14v14H5zM8 9h8M8 13h8M8 17h5" />
          </svg>
          <svg v-else-if="item.icon === 'ontology'" viewBox="0 0 24 24" aria-hidden="true">
            <circle cx="6" cy="12" r="3" /><circle cx="18" cy="6" r="3" /><circle cx="18" cy="18" r="3" />
            <path d="m9 11 6-4M9 13l6 4" />
          </svg>
          <svg v-else viewBox="0 0 24 24" aria-hidden="true">
            <path d="M4 7h16M7 7v10M4 17h16M17 7v10" />
            <circle cx="7" cy="11" r="2" /><circle cx="17" cy="14" r="2" />
          </svg>
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>
      <div class="rail-mode" :title="isMock ? '当前为严格 Mock 模式' : '当前连接控制面 BFF'">
        <span class="mode-light" />
        {{ isMock ? 'MOCK' : 'HTTP' }}
      </div>
    </aside>

    <div class="workspace">
      <header class="topbar">
        <div>
          <strong>Semantic Nexus</strong>
          <span class="topbar-divider" aria-hidden="true" />
          <span>{{ title }}</span>
        </div>
        <div class="topbar-status">
          <span class="status-dot" aria-hidden="true" />
          {{ isMock ? '本地治理模式' : 'BFF 治理模式' }}
        </div>
      </header>
      <main id="main-content" tabindex="-1">
        <RouterView />
      </main>
    </div>
  </div>
</template>
