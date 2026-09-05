import { test, expect, type Page } from '@playwright/test'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const credential: { password: string } = JSON.parse(
  readFileSync(join(process.env.NEXUS_IDENTITY_FIXTURE_DIR ?? '../../ops/identity/.generated',
    'browser.json'), 'utf8'))

async function login(page: Page, user: string) {
  await page.goto('/')
  await page.getByRole('link', { name: '使用 Local 登录' }).click()
  await expect(page).toHaveURL(/identity\.localhost:8443/)
  expect(new URL(page.url()).searchParams.get('code_challenge_method')).toBe('S256')
  await page.getByLabel('Username or email').fill(user)
  await page.getByLabel('Password', { exact: true }).fill(credential.password)
  await page.getByRole('button', { name: 'Sign In', exact: true }).click()
  await expect(page).toHaveURL(/localhost:8444/)
  await expect(page.getByRole('button', { name: '退出此应用' })).toBeVisible()
}

test('real code+PKCE login, authorized workspace isolation, CSRF and logout', async ({ page, context }) => {
  await login(page, 'alice')
  const session = await (await page.request.get('/auth/session')).json()
  expect(session.authenticated).toBe(true)
  expect(session.workspaces).toHaveLength(2)
  const cookies = await context.cookies()
  const cookie = cookies.find(c => c.name === '__Host-nexus-session')
  expect(cookie?.httpOnly).toBe(true)
  expect(cookie?.secure).toBe(true)
  expect(await page.evaluate(() => document.cookie)).not.toContain('__Host-nexus-session')
  expect(await page.evaluate(() => Object.values(localStorage))).toEqual([])
  await page.getByLabel('工作区', { exact: true }).selectOption('workspace-a')
  await page.waitForURL('**/ask')
  const payload = {
    clientRequestId: 'identity-browser-run', workload: 'synthetic-profit',
    question: '上季度各区域利润是多少?', evaluationClock: '2024-04-15T09:00:00Z',
    evaluationTimezone: 'Etc/UTC', compilationMode: 'regional_quarterly_profit',
    executionMode: 'thread', outputMode: 'normal',
  }
  const csrf = (await (await page.request.get('/auth/session')).json()).csrfToken
  const a = { 'X-Workspace-Id': 'workspace-a', 'X-Nexus-CSRF': csrf }
  expect((await page.request.post('/api/v1/runs', { data: payload,
    headers: { 'X-Workspace-Id': 'workspace-a' } })).status()).toBe(400)
  const response = await page.request.post('/api/v1/runs', { data: payload, headers: a })
  expect(response.status(), await response.text()).toBe(202)
  const run = await response.json()
  await expect.poll(async () => (await (await page.request.get(`/api/v1/runs/${run.id}/semantic-status`,
    { headers: a })).json()).state, { timeout: 45_000 }).toBe('Succeeded')
  const detail = await page.request.get(`/api/v1/runs/${run.id}/detail`, { headers: a })
  expect(detail.status()).toBe(200)
  expect((await detail.json()).result.rows.length).toBeGreaterThan(0)
  const b = { 'X-Workspace-Id': 'workspace-b', 'X-Nexus-CSRF': csrf }
  for (const suffix of ['', '/detail', '/semantic-status', '/feedback']) {
    expect((await page.request.get(`/api/v1/runs/${run.id}${suffix}`, { headers: b })).status()).toBe(404)
  }
  expect((await page.request.post(`/api/v1/runs/${run.id}/cancel`, { headers: b, data: {} })).status()).toBe(404)
  const sameRequest = await page.request.post('/api/v1/runs', { data: payload, headers: b })
  expect(sameRequest.status()).toBe(202)
  expect((await sameRequest.json()).id).not.toBe(run.id)
  expect((await (await page.request.get('/api/v1/runs?limit=1', { headers: b })).json()).items).toHaveLength(1)
  expect((await page.request.get('/api/v1/runs',
    { headers: { ...a, 'X-Workspace-Id': 'unassigned', 'X-Dev-Roles': 'admin' } })).status()).toBe(403)
  const oldCookie = cookie!
  await page.getByRole('button', { name: '退出此应用' }).click()
  await expect(page.getByRole('link', { name: '使用 Local 登录' })).toBeVisible()
  await context.addCookies([oldCookie])
  expect((await (await page.request.get('/auth/session')).json()).authenticated).toBe(false)
})

test('a valid identity without membership receives no workspace data', async ({ page }) => {
  await login(page, 'bob')
  await expect(page.getByText('登录成功，但当前账户没有有效工作区权限。请联系工作区管理员。')).toBeVisible()
  const result = await page.request.get('/api/v1/runs', {
    headers: { 'X-Workspace-Id': 'workspace-a', 'X-Dev-Subject': 'principal-alice', 'X-Dev-Roles': 'admin' },
  })
  expect(result.status()).toBe(403)
})
