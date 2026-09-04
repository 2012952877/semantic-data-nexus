import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'
import {
  developmentIdentityProxyGuard,
  validateProxyTarget,
} from './viteProxyTarget.js'

export default defineConfig(({ mode }) => {
  const environment = loadEnv(mode, fileURLToPath(new URL('.', import.meta.url)), '')
  const proxyTarget = environment.NEXUS_PROXY_TARGET
  if (proxyTarget) {
    validateProxyTarget(
      proxyTarget,
      Boolean(environment.VITE_NEXUS_DEV_SUBJECT || environment.VITE_NEXUS_DEV_ROLES),
    )
  }

  return {
    plugins: [
      vue(),
      ...(proxyTarget ? [developmentIdentityProxyGuard(proxyTarget)] : []),
    ],
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
  }
})
