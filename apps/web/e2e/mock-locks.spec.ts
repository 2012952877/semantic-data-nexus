import { expect, test } from '@playwright/test'

test('prevents a stale owner loop from resurrecting a terminalized run', async ({ page, context }) => {
  const question = '验证过期执行租约不会复活'
  await page.addInitScript(() => {
    const nativeSetTimeout = window.setTimeout.bind(window)
    window.setTimeout = ((handler, timeout, ...args) =>
      nativeSetTimeout(handler, timeout === 360 ? 2_000 : timeout, ...args)
    ) as typeof window.setTimeout
  })
  const observer = await context.newPage()
  await observer.goto('/runs')
  await page.goto('/ask')
  await page.getByLabel('你想了解什么？').fill(question)
  await page.getByRole('button', { name: '开始受控运行' }).click()

  await expect.poll(() => observer.evaluate((targetQuestion) =>
    Object.keys(localStorage).some((candidate) => {
      if (!candidate.startsWith('semantic-nexus:run:v1:')) return false
      return JSON.parse(localStorage.getItem(candidate) ?? '{}').run?.question === targetQuestion
    }), question)).toBe(true)
  await observer.evaluate(async (targetQuestion) => {
    const key = Object.keys(localStorage).find((candidate) => {
      if (!candidate.startsWith('semantic-nexus:run:v1:')) return false
      return JSON.parse(localStorage.getItem(candidate) ?? '{}').run?.question === targetQuestion
    })
    if (!key) throw new Error('Active run record not found')
    const id = JSON.parse(localStorage.getItem(key) ?? '{}').run.id
    await navigator.locks.request(`semantic-nexus:run:${id}`, { mode: 'exclusive' }, async () => {
      await new Promise((resolve) => window.setTimeout(resolve, 2_200))
      const record = JSON.parse(localStorage.getItem(key) ?? '{}')
      const run = record.run
      run.state = 'failed'
      run.completedAt = new Date().toISOString()
      delete run.executionLease
      const failedIndex = Math.max(0, run.stages.findIndex(
        (stage: { state: string }) => stage.state === 'pending' || stage.state === 'running',
      ))
      run.stages.forEach((stage: { state: string }, index: number) => {
        if (index === failedIndex) stage.state = 'failed'
        else if (index > failedIndex || stage.state !== 'succeeded') stage.state = 'canceled'
      })
      run.diagnostics = [{
        code: 'MOCK_RUN_INTERRUPTED',
        title: '观察者已终止过期运行',
        message: '执行租约已失效。',
        recovery: '重新发起运行。',
        severity: 'warning',
      }]
      localStorage.setItem(key, JSON.stringify(record))
    })
  }, question)

  await expect(page.getByText('观察者已终止过期运行')).toBeVisible()
  const persistedState = await observer.evaluate((targetQuestion) => {
    const key = Object.keys(localStorage).find((candidate) => {
      if (!candidate.startsWith('semantic-nexus:run:v1:')) return false
      return JSON.parse(localStorage.getItem(candidate) ?? '{}').run?.question === targetQuestion
    })
    return key ? JSON.parse(localStorage.getItem(key) ?? '{}').run?.state : undefined
  }, question)
  expect(persistedState).toBe('failed')
})

test('observer expires a closed owner lease without reloading', async ({ page, context }) => {
  const question = '验证关闭页面后的租约到期'
  await page.addInitScript(() => {
    const nativeSetTimeout = window.setTimeout.bind(window)
    window.setTimeout = ((handler, timeout, ...args) =>
      nativeSetTimeout(handler, timeout === 360 ? 2_000 : timeout, ...args)
    ) as typeof window.setTimeout
  })
  const observer = await context.newPage()
  await observer.goto('/runs')
  await page.goto('/ask')
  await page.getByLabel('你想了解什么？').fill(question)
  await page.getByRole('button', { name: '开始受控运行' }).click()

  const activeRow = observer.getByRole('row').filter({ hasText: question })
  await expect(activeRow).toContainText('运行中')

  await page.evaluate(async (targetQuestion) => {
    const key = Object.keys(localStorage).find((candidate) => {
      if (!candidate.startsWith('semantic-nexus:run:v1:')) return false
      return JSON.parse(localStorage.getItem(candidate) ?? '{}').run?.question === targetQuestion
    })
    if (!key) throw new Error('Active run record not found')
    const id = JSON.parse(localStorage.getItem(key) ?? '{}').run.id
    await navigator.locks.request(`semantic-nexus:run:${id}`, { mode: 'exclusive' }, () => {
      const record = JSON.parse(localStorage.getItem(key) ?? '{}')
      record.run.executionLease.heartbeatAt = new Date(Date.now() - 29_500).toISOString()
      localStorage.setItem(key, JSON.stringify(record))
    })
  }, question)
  await page.close()

  await expect(activeRow).toContainText('失败', { timeout: 5_000 })
  await expect(activeRow).not.toContainText('运行中')
})
