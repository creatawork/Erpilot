import { test, expect, type Page } from '@playwright/test';

const assessment = {
  schema_version: 1, assessed_at: '2026-10-12T12:00:00Z',
  order: {order_id: 'SO-T', customer: '甲', status: '待付款', total_amount: 19.5},
  business_allowed: true,
  business_reason: {code: 'cancellable_status', message: '当前状态允许发起取消申请'},
  policy_state: 'matched', decision: 'eligible',
  message: '当前可以发起取消申请，仍需审批；本次仅查询，未执行取消。',
  references: [{source: 'order-policy.md', title: '可取消的订单',
    text: '<script>window.policyUnsafe=true</script> javascript:alert(1)',
    score: 0.9, policy_id: 'order', version: '2026-10-12.1',
    effective_from: '2026-10-11T16:00:00Z', effective_until: null}],
};
const display = {version: 1, kind: 'cancellation_assessment', outcome: 'succeeded', assessment};
const result = {call_id: 'assessment', name: 'assess_order_cancellation', ok: true,
  content: JSON.stringify(assessment), display};

async function mock(page: Page) {
  const posts: string[] = [];
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    if (request.method() !== 'GET') posts.push(path);
    const json = (body: unknown) => route.fulfill({json: body});
    if (path === '/api/runtime') return json({data_source: 'erp', writes_enabled: true,
      drafts_enabled: true, business_approvals_enabled: true});
    if (path === '/api/sessions') return json({items: [{session_id: 'policy-history',
      title: '取消条件查询历史', status: 'completed', updated_at: assessment.assessed_at}], has_more: false});
    if (path.endsWith('/order-drafts')) return json({items: []});
    if (path.endsWith('/state')) {
      const id = path.split('/')[3];
      return json({session_id: id, status: 'completed', pending_approvals: [],
        messages: id === 'policy-history' ? [{role: 'user', content: '这张订单还能取消吗？'},
          {role: 'assistant', tool_calls: [{id: 'assessment', function: {
            name: 'assess_order_cancellation', arguments: '{"order_id":"SO-T"}'}}]},
          {role: 'tool', tool_call_id: 'assessment', content: result.content}] : [],
        tool_results: id === 'policy-history' ? [result] : []});
    }
    if (path === '/api/chat/stream') return route.fulfill({contentType: 'text/event-stream', body: [
      ['start', {session_id: request.postDataJSON().session_id, model: 'scripted'}],
      ['tool_started', {id: 'assessment', name: result.name, arguments: '{"order_id":"SO-T"}'}],
      ['tool_finished', {...result, id: 'assessment'}],
      ['done', {steps: 2, completed: true, duration_ms: 20, usage: null, cost: null, trace: null}],
    ].map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('')});
    return route.fulfill({status: 404, json: {error: {message: 'unexpected route ' + path}}});
  });
  return posts;
}

for (const width of [320, 768, 1024, 1440]) {
  test(`取消资格卡在 ${width}px 展示事实和可键盘展开的政策，不写入业务`, async ({page}) => {
    await page.setViewportSize({width, height: 1000});
    const errors: string[] = []; page.on('pageerror', error => errors.push(error.message));
    const posts = await mock(page); await page.goto('/');
    const input = page.getByRole('textbox', {name: '消息', exact: true});
    await expect(input).toBeEnabled(); await input.fill('这张订单还能取消吗？'); await input.press('Enter');
    const card = page.locator('.cancellation-assessment');
    await expect(card.getByText('可发起取消申请', {exact: true})).toBeVisible();
    await expect(card.getByText('查询完成', {exact: true})).toBeVisible();
    await expect(card.getByText('原单金额 ¥19.50')).toBeVisible();
    await expect(card.getByText(assessment.message)).toBeVisible();
    const summary = card.locator('summary'); await summary.focus(); await page.keyboard.press('Enter');
    await expect(card.locator('details')).toHaveAttribute('open', '');
    await expect(card.getByText('可取消的订单')).toBeVisible();
    await expect(card.getByText(/版本 2026-10-12.1/)).toBeVisible();
    await expect(card.locator('script, img, a[href^="javascript:"]')).toHaveCount(0);
    await expect(card.getByRole('button')).toHaveCount(0);
    expect(await page.evaluate(() => (window as unknown as {policyUnsafe?: boolean}).policyUnsafe)).toBeUndefined();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
    expect(posts.filter(path => path !== '/api/chat/stream')).toEqual([]);
    expect(errors).toEqual([]);
    if (width === 320 || width === 1440) await card.screenshot({path: `../../.playwright-mcp/b5-card-${width}.png`});
  });
}

test('历史恢复保留原查询结论和引用，不发起新查询或审批', async ({page}) => {
  const posts = await mock(page); await page.goto('/');
  await page.getByText('取消条件查询历史', {exact: true}).click();
  const card = page.locator('.cancellation-assessment');
  await expect(card.getByText('可发起取消申请', {exact: true})).toBeVisible();
  await card.locator('summary').click();
  await expect(card.getByText(/版本 2026-10-12.1/)).toBeVisible();
  await expect(page.getByRole('button', {name: '批准', exact: true})).toHaveCount(0);
  expect(posts).toEqual([]);
});
