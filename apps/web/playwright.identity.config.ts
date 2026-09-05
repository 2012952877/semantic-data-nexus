import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: ['identity.spec.ts', 'catalog-identity.spec.ts'],
  workers: 1,
  retries: 0,
  use: {
    baseURL: 'https://localhost:8444',
    ignoreHTTPSErrors: true,
    trace: 'off',
    screenshot: 'off',
    video: 'off',
  },
})
