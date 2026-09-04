import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testMatch: 'full-stack.spec.ts',
  fullyParallel: false,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  timeout: 60_000,
  expect: {
    timeout: 45_000,
  },
  use: {
    baseURL: process.env.NEXUS_FULL_STACK_BASE_URL ?? 'http://127.0.0.1:8080',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'full-stack-chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
