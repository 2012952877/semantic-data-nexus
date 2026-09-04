import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'
import { validateProxyTarget } from './viteProxyTarget.js'

const proxyTarget = process.env.NEXUS_PROXY_TARGET
if (proxyTarget) validateProxyTarget(proxyTarget)

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: proxyTarget
    ? {
        proxy: {
          '/api': {
            target: proxyTarget,
            changeOrigin: true,
          },
        },
      }
    : undefined,
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['./tests/**/*.spec.ts'],
    setupFiles: ['./tests/setup.ts'],
  },
})
