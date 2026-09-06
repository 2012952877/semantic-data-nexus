import { defineConfig, devices } from '@playwright/test'

const baseURL = process.env.NEXUS_NETWORK_DEMO_URL
const username = process.env.NEXUS_TEST_USERNAME
const password = process.env.NEXUS_TEST_PASSWORD
if (process.env.NEXUS_ALLOW_NETWORK_DEMO !== '1' || !baseURL || !username || !password) {
  throw new Error('Explicit network-demo permission, URL, and test credentials are required.')
}
const target = new URL(baseURL)
if (target.protocol !== 'https:' || target.username || target.password
  || target.pathname !== '/' || target.search || target.hash) {
  throw new Error('The network-demo URL must be a credential-free HTTPS origin.')
}

export default defineConfig({
  testDir: './e2e-network',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 90_000,
  expect: { timeout: 60_000 },
  reporter: 'list',
  use: {
    ...devices['Desktop Chrome'],
    baseURL: target.origin,
    httpCredentials: { username, password, origin: target.origin },
    trace: 'off',
    video: 'off',
    screenshot: 'off',
  },
})
