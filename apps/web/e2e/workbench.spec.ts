import { expect, test } from '@playwright/test'

test('stable routes render their primary surfaces', async ({ page }) => {
  const routes = [
    ['/ask', '把业务问题编排为可信结果'],
    ['/runs', '运行记录'],
    ['/runs/run-syn-1001', '比较各区域第二季度净销售额、目标达成率和同比'],
    ['/ontology', '区域销售语义模型'],
    ['/settings', '组件状态'],
  ] as const

  for (const [path, heading] of routes) {
    await page.goto(path)
    await expect(page.getByRole('main').getByText(heading, { exact: false }).first()).toBeVisible()
  }
})

test('submits a governed question and renders the committed result', async ({ page }) => {
  await page.goto('/ask')
  await page.getByLabel('你想了解什么？').fill('比较各区域第二季度净销售额与目标')
  await page.getByRole('button', { name: '开始受控运行' }).click()

  await expect(page.getByText('结果已提交')).toBeVisible()
  await expect(page.getByRole('table')).toContainText('华东')
  await expect(page.getByRole('table')).toContainText('目标达成率')
})

test('keeps another tab active and cancels without committing a result', async ({ page, context }) => {
  await page.goto('/ask')
  const question = '按区域汇总 2025 年上半年的净销售额'
  await page.getByLabel('你想了解什么？').fill(question)
  await page.getByRole('button', { name: '开始受控运行' }).click()

  const observer = await context.newPage()
  await observer.goto('/runs')
  const activeRow = observer.getByRole('link').filter({ hasText: question })
  await expect(activeRow).toContainText('运行中')
  await expect(activeRow).not.toContainText('失败')

  await page.getByRole('button', { name: '取消运行' }).click()

  await expect(page.getByText('运行已停止')).toBeVisible()
  await expect(page.getByText('没有执行或提交剩余阶段')).toBeVisible()
  await expect(page.getByText('结果已提交')).toHaveCount(0)
  await expect(activeRow).toContainText('已取消')
  await observer.close()
})
