import { expect, test } from '@playwright/test'

test('deployed workbench uses the real same-origin BFF client', async ({ page }) => {
  await page.clock.setFixedTime(new Date('2024-04-15T09:00:00Z'))
  await page.goto('/ask')
  await expect(page.getByText('Mock 场景与执行设置')).toHaveCount(0)

  const question = '上季度各区域利润是多少?'
  await page.getByLabel('你想了解什么？').fill(question)
  const createResponse = page.waitForResponse((response) =>
    response.request().method() === 'POST'
    && new URL(response.url()).pathname.replace(/\/$/, '') === '/api/v1/runs')
  await page.getByRole('button', { name: '开始受控运行' }).click()

  expect([200, 202]).toContain((await createResponse).status())
  await expect(page.getByText('结果已提交')).toBeVisible()
  await expect(page.getByText('4 个区域')).toBeVisible()
  await expect(page.getByRole('table')).toContainText('北辰区')
  await expect(page.getByRole('table')).toContainText('2,334')
  expect(await page.evaluate(() => localStorage.length)).toBe(0)

  await page.getByRole('link', { name: '检查运行 →' }).click()
  await expect(page.getByRole('heading', { name: '已提交结果' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '来源与血缘' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '提交清单' })).toBeVisible()
  await expect(page.locator('.manifest')).toContainText('sha256:')
})
