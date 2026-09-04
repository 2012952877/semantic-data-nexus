import { expect, test } from '@playwright/test'

test('stable routes render their primary surfaces', async ({ page }) => {
  const routes = [
    ['/ask', '把业务问题编排为可信结果'],
    ['/runs', '运行记录'],
    ['/runs/run_00000000000000000000000000000001', '比较各区域第二季度净销售额、目标达成率和同比'],
    ['/ontology', '语义目录不可用'],
    ['/settings', '组件状态'],
  ] as const

  for (const [path, heading] of routes) {
    await page.goto(path)
    await expect(page.getByRole('main').getByText(heading, { exact: false }).first()).toBeVisible()
  }

  await page.goto('/settings')
  await expect(page.getByText('未知', { exact: true })).toBeVisible()
  await expect(page.getByText('未探测或推断后端健康度', { exact: false })).toBeVisible()
  await page.goto('/runs/run_00000000000000000000000000000001')
  await expect(page.locator('.run-id')).not.toContainText('合成数据')
  await expect(page.getByText('unknown', { exact: true })).toBeVisible()
  await expect(page.getByText('regional_quarterly_profit', { exact: true })).toBeVisible()
  await expect(page.getByText('Nexus Planner Small', { exact: true })).toHaveCount(0)

  await page.goto('/runs')
  await expect(page.getByRole('table', { name: '运行列表' })).toBeVisible()
  await expect(page.getByRole('columnheader')).toHaveCount(4)

  await page.setViewportSize({ width: 390, height: 844 })
  await page.goto('/ask')
  await page.keyboard.press('Tab')
  await expect(page.getByText('跳到主要内容')).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(page.getByRole('link', { name: '提问' })).toBeFocused()
  await expect(page.locator('.brand-mark')).toBeHidden()
})

test('submits a governed question and renders the committed result', async ({ page }) => {
  await page.goto('/ask')
  await page.getByLabel('你想了解什么？').fill('比较各区域第二季度净销售额与目标')
  await page.getByRole('button', { name: '开始受控运行' }).click()

  await expect(page.getByText('结果已提交')).toBeVisible()
  await expect(page.getByRole('table')).toContainText('华东')
  await expect(page.getByRole('table')).toContainText('目标达成率')
  await expect(page.getByRole('table')).toContainText('¥4,286,000.00')
  await expect(page.getByRole('table')).toContainText('12.3400%')
})

test('shares BFF history across tabs and cancels without committing a result', async ({ page, context }) => {
  await page.goto('/ask')
  const question = '按区域汇总 2025 年上半年的净销售额 [held-running-until-cancel]'
  await page.getByLabel('你想了解什么？').fill(question)
  await page.getByRole('button', { name: '开始受控运行' }).click()

  const observer = await context.newPage()
  await observer.goto('/runs')
  const activeRow = observer.getByRole('row').filter({ hasText: question })
  await expect(activeRow).toContainText(/排队中|运行中/)
  await expect(activeRow).not.toContainText('失败')
  await activeRow.getByRole('link').click()

  await page.getByRole('button', { name: '取消运行' }).click()

  await expect(page.getByText('运行已停止')).toBeVisible()
  await expect(page.getByText('没有执行或提交剩余阶段')).toBeVisible()
  await expect(page.getByText('结果已提交')).toHaveCount(0)
  await observer.reload()
  await expect(observer.locator('.run-title-line').getByText('已取消', { exact: true }))
    .toBeVisible()
  expect(await observer.evaluate(() => localStorage.length)).toBe(0)
  await observer.close()
})

test('renders BFF text as text instead of unsafe HTML', async ({ page }) => {
  const question = '<img src=x onerror="window.__unsafe = true"> 安全显示'
  await page.goto('/ask')
  await page.getByLabel('你想了解什么？').fill(question)
  await page.getByRole('button', { name: '开始受控运行' }).click()
  await expect(page.getByText('结果已提交')).toBeVisible()
  await page.getByRole('link', { name: '检查运行 →' }).click()

  await expect(page.getByRole('main')).toContainText(question)
  await expect(page.locator('main img')).toHaveCount(0)
  expect(await page.evaluate(() => (window as typeof window & { __unsafe?: boolean }).__unsafe))
    .toBeUndefined()
})
