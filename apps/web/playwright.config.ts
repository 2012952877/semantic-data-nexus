import { defineConfig, devices } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  testIgnore: 'mock-locks.spec.ts',
  fullyParallel: false,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? 'github' : 'list',
  use: {
    baseURL: 'http://127.0.0.1:4173',
    trace: 'on-first-retry',
  },
  projects: [
    {
      name: 'chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
  webServer: [
    {
      command: 'node --experimental-strip-types tests/httpStubServer.ts',
      url: 'http://127.0.0.1:4310/api/v1/runs',
      reuseExistingServer: false,
    },
    {
      command: 'pnpm exec vite --host 127.0.0.1 --port 4173',
      url: 'http://127.0.0.1:4173/ask',
      reuseExistingServer: false,
      env: {
        VITE_NEXUS_CLIENT: 'http',
        VITE_NEXUS_BASE_URL: 'http://127.0.0.1:4173',
        VITE_NEXUS_DEV_SUBJECT: 'playwright-user',
        VITE_NEXUS_DEV_ROLES: 'Reader,Contributor',
        NEXUS_PROXY_TARGET: 'http://127.0.0.1:4310',
      },
    },
  ],
})
