import { expect, test } from '@playwright/test'
import { writeFile } from 'node:fs/promises'

test('public core flow uses a real model and exposes committed synthetic results', async ({ page }, testInfo) => {
  const response = await page.goto('/ask')
  expect(response?.status()).toBe(200)
  await expect(page.getByRole('note')).toContainText('合成业务数据')
  await page.getByRole('button', { name: '按区域和季度汇总利润', exact: true }).click()
  await expect(page.getByRole('textbox', { name: '你想了解什么？' }))
    .toHaveValue('按区域和季度汇总利润')
  await page.getByRole('button', { name: '开始受控运行', exact: true }).click()
  await expect(page.locator('.inline-result, .outcome-error').first()).toBeVisible()
  if (await page.locator('.outcome-error').isVisible()) {
    await page.screenshot({ path: testInfo.outputPath('public-query-failure.png'), fullPage: true })
    throw new Error(await page.locator('.outcome-error').innerText())
  }
  await expect(page.getByText('结果已提交', { exact: true })).toBeVisible()
  await expect(page.getByRole('heading', { name: '20 行结果', exact: true })).toBeVisible()
  await expect(page.locator('.scope-ledger dd').nth(3))
    .toHaveText('gpt-4.1-mini-2025-04-14')
  await expect(page.locator('.inline-result tbody tr')).toHaveCount(20)

  const runId = (await page.locator('.execution-heading p').innerText()).trim()
  expect(runId).toMatch(/^run_[0-9a-f]{32}$/)
  const statusResponse = await page.request.get(`/api/v1/runs/${runId}`, { maxRedirects: 0 })
  expect(statusResponse.status()).toBe(200)
  const status = await statusResponse.json()
  expect(status.state).toBe('Succeeded')
  expect(status.tokenUsage.model).toBe('gpt-4.1-mini-2025-04-14')
  expect(status.tokenUsage.inputTokens).toBeGreaterThan(0)
  expect(status.tokenUsage.outputTokens).toBeGreaterThan(0)

  await page.screenshot({ path: testInfo.outputPath('01-public-query-result.png'), fullPage: true })
  await page.getByRole('link', { name: '检查运行 →', exact: true }).click()
  await expect(page).toHaveURL(new RegExp(`/runs/${runId}$`))
  await expect(page.locator('body')).toContainText(runId)
  await expect(page.getByRole('heading', { name: '来源与血缘', exact: true })).toBeVisible()
  await expect(page.locator('.source-list article').first()).toBeVisible()
  await expect(page.getByRole('heading', { name: '提交清单', exact: true })).toBeVisible()
  await expect(page.locator('.manifest dd').nth(1)).toContainText(/[a-f0-9]{64}/)
  await page.screenshot({ path: testInfo.outputPath('02-public-run-detail.png'), fullPage: true })
  const evidencePath = testInfo.outputPath('network-demo-evidence.json')
  await writeFile(evidencePath, JSON.stringify({
    url: page.url(),
    runId,
    question: status.question,
    state: status.state,
    model: status.tokenUsage.model,
    tokenUsage: status.tokenUsage,
    rowCount: 20,
    data: 'synthetic',
    sourceCommit: process.env.NEXUS_DEMO_SOURCE_COMMIT ?? null,
  }, null, 2))
  await testInfo.attach('network-demo-evidence', {
    path: evidencePath,
    contentType: 'application/json',
  })
})
