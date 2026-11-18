import { expect, test } from '@playwright/test'

/**
 * 采集规则发布快照：选择器测试通过 → 待发布候选 → 显式确认 → 生效；
 * 采集监控按版本取数并把差异回写到采集规则/待办工作台。
 *
 * 使用示例规则 CRAW-0002，并先保证它有一条待发布候选，用例可重复执行。
 */
test('采集规则发布快照与监控版本核对', async ({ page }) => {
  test.setTimeout(300_000)
  const browserErrors: string[] = []
  page.on('pageerror', (e) => browserErrors.push(e.message))
  page.on('response', (r) => {
    if (r.url().includes('/api/') && r.status() >= 500) browserErrors.push(`${r.status()} ${r.url()}`)
  })

  const ruleList = await (await page.request.get('/api/crawler')).json()
  const rule = ruleList.items.find((item: any) => item['规则编号'] === 'CRAW-0002')
  expect(rule).toBeTruthy()
  const ruleId = rule.id

  // 准备一条待发布候选：已有就复用，没有就提交一次选择器测试。
  let snapshots = await (await page.request.get(`/api/crawler/snapshots?rule_id=${ruleId}`)).json()
  let candidate = snapshots.items.find((item: any) => item.status === '待发布')
  if (!candidate) {
    const submitted = await page.request.post(`/api/crawler/${ruleId}/tests`, {
      data: { 正文选择器: 'article.main-body', 翻页参数: 'page=1&size=20' },
    })
    expect((await submitted.json()).ok).toBeTruthy()
    snapshots = await (await page.request.get(`/api/crawler/snapshots?rule_id=${ruleId}`)).json()
    candidate = snapshots.items.find((item: any) => item.status === '待发布')
  }
  expect(candidate).toBeTruthy()
  const candidateVersion = candidate['版本号']
  const effectiveBefore = (await (await page.request.get(`/api/crawler/${ruleId}/effective`)).json().catch(() => null))?.['版本号']

  await page.goto('/crawler')
  await expect(page.getByTestId(`row-${ruleId}`)).toBeVisible({ timeout: 10_000 })

  // A. 快照抽屉：草稿与待发布候选分开呈现，生效版本与候选版本不同。
  await page.getByTestId(`snapshots-${ruleId}`).click()
  await expect(page.getByTestId('snapshot-panel')).toBeVisible()
  await expect(page.getByTestId('draft-block')).toContainText('规则版本')
  await expect(page.getByTestId('effective-version')).toHaveText(effectiveBefore ?? '未发布')
  await expect(page.locator('[data-snapshot-status="待发布"]')).toHaveCount(1)
  await expect(page.locator('[data-snapshot-status="待发布"]')).toContainText(candidateVersion)

  // B. 参数解析预览：目标网址与翻页参数被结构化拆开。
  await page.getByTestId('testform-url').fill('https://example.com/list?cat=1')
  await page.getByTestId('testform-body-selector').fill('article.main-body')
  await page.getByTestId('testform-pagination').fill('page=1&size=20')
  await page.getByTestId('testform-parse').click()
  await expect(page.getByTestId('parse-preview')).toContainText('example.com')
  await expect(page.getByTestId('parse-preview')).toContainText('"size": "20"')

  // C. 显式确认候选快照后才切换生产读取的版本。
  await page.locator('[data-snapshot-status="待发布"]').first().getByRole('button', { name: '确认发布', exact: true }).click()
  await expect(page.getByTestId('panel-message')).toContainText('发布')
  await expect(page.locator('[data-snapshot-status="已发布"]')).toHaveCount(1)
  await expect(page.getByTestId('effective-version')).toHaveText(candidateVersion)

  // D. 已发布快照不再展示确认入口，重复确认经接口也只能被拒绝。
  const published = page.locator('[data-snapshot-status="已发布"]').first()
  await expect(published.getByRole('button', { name: '确认发布', exact: true })).toHaveCount(0)
  const confirmedAgain = await page.request.post(`/api/crawler/snapshots/${candidate.id}/actions`, { data: { action: '确认发布' } })
  const confirmedAgainBody = await confirmedAgain.json()
  expect(confirmedAgainBody.ok).toBeFalsy()
  expect(String(confirmedAgainBody.message)).toContain('不能再次确认发布')

  // E. 测试记录与审计都指向同一个版本。
  await expect(page.locator('[data-testid^="test-"]').filter({ hasText: candidateVersion }).first()).toContainText('通过')
  await published.getByRole('button', { name: '审计', exact: true }).click()
  await expect(page.getByTestId('audit-table')).toContainText('确认发布')

  await page.getByTestId('close-snapshots').click()

  // F. 采集监控按版本汇总，消费快照后回写并把待办写进工作台。
  await page.goto('/monitor')
  await expect(page.getByTestId('kpi-card').first()).toBeVisible({ timeout: 10_000 })
  await page.getByTestId('sync-snapshots').click()
  await expect(page.getByTestId('sync-message')).toContainText('已消费')
  await expect(page.getByTestId('version-table')).toContainText(candidateVersion)
  await expect(page.getByTestId('todo-table')).toContainText(candidateVersion)

  const todoRow = page.locator('tbody tr[data-testid^="todo-"]').filter({ hasText: candidateVersion }).first()
  await todoRow.getByRole('button', { name: '开始处理', exact: true }).click()
  await expect(todoRow).toHaveAttribute('data-status', '处理中')
  await todoRow.getByRole('button', { name: '标记已处理', exact: true }).click()
  await expect(todoRow).toHaveAttribute('data-status', '已处理')

  // G. 待办回写后，规则页能看到监控核对版本。
  const ruleDetail = await (await page.request.get(`/api/crawler/${ruleId}`)).json()
  expect(String(ruleDetail['核对结果'] ?? '')).toContain(candidateVersion)

  expect(browserErrors).toEqual([])
})
