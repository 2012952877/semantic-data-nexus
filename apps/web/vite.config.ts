import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

const proxyTarget = process.env.NEXUS_PROXY_TARGET
if (proxyTarget) {
  const url = new URL(proxyTarget)
  if (!['http:', 'https:'].includes(url.protocol)
    || url.username
    || url.password
    || url.search
    || url.hash) {
    throw new Error(
      'NEXUS_PROXY_TARGET must be an HTTP(S) URL without credentials, query, or fragment.',
    )
  }
}

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
