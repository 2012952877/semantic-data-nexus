import { createRouter, createWebHistory, type RouterHistory } from 'vue-router'

import AskView from '@/views/AskView.vue'
import OntologyView from '@/views/OntologyView.vue'
import RunDetailView from '@/views/RunDetailView.vue'
import RunsView from '@/views/RunsView.vue'
import SettingsView from '@/views/SettingsView.vue'

export const createNexusRouter = (history: RouterHistory = createWebHistory()) =>
  createRouter({
    history,
    routes: [
      { path: '/', redirect: '/ask' },
      { path: '/ask', name: 'ask', component: AskView, meta: { title: '提问' } },
      { path: '/runs', name: 'runs', component: RunsView, meta: { title: '运行记录' } },
      { path: '/runs/:id', name: 'run-detail', component: RunDetailView, meta: { title: '运行详情' } },
      { path: '/ontology', name: 'ontology', component: OntologyView, meta: { title: '语义模型' } },
      { path: '/settings', name: 'settings', component: SettingsView, meta: { title: '系统状态' } },
      { path: '/:pathMatch(.*)*', redirect: '/ask' },
    ],
    scrollBehavior: () => ({ top: 0 }),
  })
