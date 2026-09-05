import { test, expect } from '@playwright/test'
import { existsSync, readFileSync } from 'node:fs'
import { randomUUID } from 'node:crypto'
import { join } from 'node:path'

const root = process.env.NEXUS_IDENTITY_FIXTURE_DIR ?? '../../ops/identity/.generated'
const credential = JSON.parse(readFileSync(join(root, 'browser.json'), 'utf8'))
const catalogs = JSON.parse(readFileSync(join(root, 'catalog-browser.json'), 'utf8'))

declare global {
  interface Window {
    catalogAbort?: AbortController
    catalogAbortOutcome?: string
  }
}

test('real Keycloak session reaches guarded catalog query, runtime result and clarification', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('link', { name: '使用 Local 登录' }).click()
  await expect(page).toHaveURL(/identity\.localhost:8443/)
  expect(new URL(page.url()).searchParams.get('code_challenge_method')).toBe('S256')
  await page.getByLabel('Username or email').fill('alice')
  await page.getByLabel('Password', { exact: true }).fill(credential.password)
  await page.getByRole('button', { name: 'Sign In', exact: true }).click()
  await expect(page.getByRole('button', { name: '退出此应用' })).toBeVisible()
  await page.getByLabel('工作区', { exact: true }).selectOption('workspace-a')
  await page.waitForURL('**/ask')
  const csrf = (await (await page.request.get('/auth/session')).json()).csrfToken
  const headers = { 'X-Workspace-Id': 'workspace-a', 'X-Nexus-CSRF': csrf }
  for (const catalog of catalogs) {
    const response = await page.request.put('/api/v1/workspace/grants', {
      headers, data: { ...catalog.grant, requestId: `grant-${randomUUID()}` },
    })
    expect(response.status(), await response.text()).toBe(204)
  }
  const energy = { ...catalogs[0].request, request_id: `energy-${randomUUID()}` }
  const withoutCsrf = await page.request.post('/api/v1/catalog/queries', {
    data: energy, headers: { 'X-Workspace-Id': 'workspace-a' },
  })
  expect(withoutCsrf.status()).toBe(400)
  const queried = await page.request.post('/api/v1/catalog/queries', { data: energy, headers })
  expect(queried.status(), await queried.text()).toBe(200)
  const result = await queried.json()
  expect(result.status).toBe('succeeded')
  expect(result.provenance.runtime_contract).toBe('query-runtime/v1')
  expect(result.compilation.input_tokens).toBe(100)
  expect(result.result.rows).toEqual([['Cedar', 10], ['Delta', 7]])
  const replay = await page.request.post('/api/v1/catalog/queries', { data: energy, headers })
  expect(await replay.json()).toEqual(result)
  const foreign = await page.request.post('/api/v1/catalog/queries', {
    data: energy, headers: { ...headers, 'X-Workspace-Id': 'workspace-b' },
  })
  expect(foreign.status()).toBe(403)

  const laboratory = { ...catalogs[1].request, request_id: `lab-${randomUUID()}`,
    question: 'yield for alpha above score 1' }
  const clarify = await page.request.post('/api/v1/catalog/queries', { data: laboratory, headers })
  expect(clarify.status(), await clarify.text()).toBe(200)
  const pending = await clarify.json()
  expect(pending.status).toBe('clarification_required')
  const path = `/api/v1/catalog/clarifications/${pending.compilation.clarification_id}/answers`
  const answer = { contract_version: 'catalog-answer/v1', catalog: laboratory.catalog,
    revision: 1, choice_id: 'lab.mean_yield' }
  const completed = await page.request.post(path, { data: answer, headers })
  expect(completed.status(), await completed.text()).toBe(200)
  const resolved = await completed.json()
  expect(resolved.status).toBe('succeeded')
  expect(resolved.result.rows).toEqual([[4]])
  expect(await (await page.request.post(path, { data: answer, headers })).json()).toEqual(resolved)
  expect((await page.request.post(path, { data: { ...answer, choice_id: 'lab.total_yield' }, headers })).status()).toBe(409)
  const observation = join(root, 'catalog-observations', 'model.json')
  const previousSequence = existsSync(observation)
    ? JSON.parse(readFileSync(observation, 'utf8')).sequence : 0
  const cancelled = { ...energy, request_id: `abort-${randomUUID()}`,
    question: `${energy.question} [synthetic-disconnect]` }
  await page.evaluate(({ payload, requestHeaders }) => {
    window.catalogAbort = new AbortController()
    window.catalogAbortOutcome = 'pending'
    void fetch('/api/v1/catalog/queries', {
      method: 'POST', headers: { ...requestHeaders, 'Content-Type': 'application/json' },
      body: JSON.stringify(payload), signal: window.catalogAbort.signal,
    }).then(response => { window.catalogAbortOutcome = `status:${response.status}` })
      .catch(error => { window.catalogAbortOutcome = error instanceof DOMException ? error.name : 'error' })
  }, { payload: cancelled, requestHeaders: headers })
  await expect.poll(() => {
    if (!existsSync(observation)) return false
    const event = JSON.parse(readFileSync(observation, 'utf8'))
    return event.sequence > previousSequence && event.state === 'waiting'
  }).toBe(true)
  await page.evaluate(() => window.catalogAbort?.abort())
  await expect.poll(() => page.evaluate(() => window.catalogAbortOutcome)).toBe('AbortError')
  await expect.poll(() => JSON.parse(readFileSync(observation, 'utf8')).state).toBe('cancelled')
  const uncertain = await page.request.post('/api/v1/catalog/queries', { data: cancelled, headers })
  expect(uncertain.status(), await uncertain.text()).toBe(409)
  expect((await uncertain.json()).code).toBe('COMPILATION_IN_PROGRESS')
  const revoked = await page.request.put('/api/v1/workspace/grants', {
    headers, data: { ...catalogs[1].grant, active: false, requestId: `revoke-${randomUUID()}` },
  })
  expect(revoked.status(), await revoked.text()).toBe(204)
  expect((await page.request.post(path, { data: answer, headers })).status()).toBe(403)
})
