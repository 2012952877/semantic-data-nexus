import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.NEXUS_FULL_STACK_BASE_URL ?? 'http://127.0.0.1:8080'
const username = process.env.NEXUS_TEST_USERNAME
const password = process.env.NEXUS_TEST_PASSWORD
if (Boolean(username) !== Boolean(password)) {
  throw new Error('Both temporary HTTPS test credentials must be supplied.')
}
if (username && new URL(baseURL).protocol !== 'https:') {
  throw new Error('Temporary test credentials require an HTTPS base URL.')
}
const httpCredentials = username && password
  ? { username, password, origin: new URL(baseURL).origin }
  : undefined

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
    baseURL,
    httpCredentials,
    trace: httpCredentials ? 'off' : 'on-first-retry',
  },
  projects: [
    {
      name: 'full-stack-chromium',
      use: { ...devices['Desktop Chrome'] },
    },
  ],
})
