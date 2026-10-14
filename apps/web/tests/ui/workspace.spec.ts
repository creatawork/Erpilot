import { test, expect, type Page } from '@playwright/test';
async function mockWorkspace(page: Page, approval = false) {
  const mutationRequests: string[] = [];
  const operation = { operation_id: 'op-original', kind: 'adjust_stock', status: 'open',
    expires_at: '2099-01-01T00:00:00Z', invalid_reason: null,
    summary: { sku: 'A', name: '验收商品', delta: -3, quantity_before: 20, quantity_after: 17, warehouse: '主仓', stock_exists: true } };
  const pending = { call_id: 'call-original', pending_id: 'pending-original', tool: 'adjust_stock',
    risk: 'single_confirm', arguments: { operation_id: operation.operation_id }, operation };
  await page.route('**/api/**', async route => {
    const request = route.request(); const path = new URL(request.url()).pathname;
    const json = (body: unknown, status = 200) => route.fulfill({ status, json: body });
    if (request.method() !== 'GET') mutationRequests.push(path);
    if (path === '/api/runtime') return json({ data_source: 'demo', writes_enabled: true, drafts_enabled: true, business_approvals_enabled: true });
    if (path === '/api/sessions') return json({ items: [
      { session_id: 'history-done', title: '昨天的订单报价', status: 'completed', updated_at: '2026-10-10T12:00:00Z' },
      { session_id: 'history-broken', title: '恢复失败的会话', status: 'failed', updated_at: '2026-10-10T12:00:00Z' },
    ], has_more: false });
    if (path.endsWith('/state')) {
      const id = path.split('/')[3];
      if (id === 'history-broken') return json({ error: { message: '验收恢复失败' } }, 503);
      return json({ session_id: id, status: approval ? 'waiting_approval' : 'completed',
        messages: id === 'history-done' ? [{ role: 'user', content: '昨天的订单明细' }] : [],
        pending_approvals: approval ? [pending] : [], tool_results: [] });
    }
    if (path.endsWith('/order-drafts')) return json({ items: [] });
    if (path.includes('/operations/')) return json(operation);
    if (path === '/api/chat/stream') {
      const text = '**报价完成**\n\n| 商品 | 库存 |\n| --- | --- |\n| A | 20 |\n\n[来源](https://example.com)\n\n<script>window.erpilotUnsafeMarkupExecuted=true</script>\n[危险链接](javascript:alert(1))';
      return route.fulfill({ contentType: 'text/event-stream', body: [
        ['start', { model: 'scripted', session_id: request.postDataJSON().session_id }],
        ['delta', { text }], ['done', { steps: 1, completed: true, duration_ms: 20, usage: null, cost: null, trace: null }],
      ].map(([event, data]) => 'event: ' + event + '\ndata: ' + JSON.stringify(data) + '\n\n').join('') });
    }
    return json({ error: { message: 'Unexpected fixture route: ' + path } }, 404);
  });
  return mutationRequests;
}

test('桌面工作台把历史、对话和业务表单分开', async ({ page }) => {
  await mockWorkspace(page); await page.goto('/');
  await expect(page.locator('.workspace-sidebar')).toBeVisible();
  await expect(page.locator('.chat-main')).toBeVisible();
  await expect(page.getByRole('complementary', { name: '业务面板' })).toBeVisible();
  await expect(page.getByRole('textbox', { name: '草稿客户' })).toBeVisible();
  await expect(page.locator('.chat-main').getByRole('textbox', { name: '草稿客户' })).toHaveCount(0);
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toBeEnabled();
});

test('窄屏工作台通过抽屉查看会话和业务，关闭后恢复焦点', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 }); await mockWorkspace(page); await page.goto('/');
  await page.getByRole('button', { name: '历史会话', exact: true }).click();
  await expect(page.getByRole('dialog').getByText('昨天的订单报价')).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: '历史会话', exact: true })).toBeFocused();
  await page.getByRole('button', { name: '业务面板', exact: true }).click();
  await expect(page.getByRole('dialog').getByRole('textbox', { name: '草稿客户' })).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: '业务面板', exact: true })).toBeFocused();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
});

test('发送流式回复后正确展示 Markdown 并清洗模型 HTML 和危险链接', async ({ page }) => {
  await mockWorkspace(page); await page.goto('/');
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  await expect(input).toBeEnabled(); await input.fill('查询商品 A 的库存'); await input.press('Enter');
  await expect(page.locator('.md').getByText('报价完成', { exact: true })).toBeVisible();
  await expect(page.locator('.md table')).toBeVisible();
  await expect(page.locator('.md script')).toHaveCount(0);
  await expect(page.locator('.md a[href^="javascript:"]')).toHaveCount(0);
  expect(await page.evaluate(() => (window as unknown as { erpilotUnsafeMarkupExecuted?: boolean }).erpilotUnsafeMarkupExecuted)).toBeUndefined();
  await expect(page.locator('.md').getByRole('link', { name: '来源' })).toHaveAttribute('rel', 'noopener noreferrer nofollow');
  await expect(input).toBeEnabled();
});

test('恢复审批保持输入锁定，业务面板显示核对后的实际影响', async ({ page }) => {
  const mutations = await mockWorkspace(page, true); await page.goto('/');
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toBeDisabled();
  await expect(page.getByRole('complementary', { name: '业务面板' }).getByText('20 →')).toBeVisible();
  await expect(page.getByRole('complementary', { name: '业务面板' }).getByRole('button', { name: '批准', exact: true })).toBeEnabled();
  await expect(page.getByRole('button', { name: '新会话', exact: true })).toBeDisabled(); expect(mutations).toEqual([]);
});

test('切换历史恢复失败时锁定输入，重试和新会话仍可使用', async ({ page }) => {
  await mockWorkspace(page); await page.goto('/'); await page.getByText('昨天的订单报价', { exact: true }).click();
  await expect(page.locator('.chat-main').getByText('昨天的订单明细', { exact: true })).toBeVisible();
  await page.getByText('恢复失败的会话', { exact: true }).click();
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toBeDisabled();
  await expect(page.getByRole('button', { name: '重试恢复', exact: true })).toBeEnabled();
  await page.getByRole('button', { name: '开始新会话', exact: true }).click();
  await expect(page.getByRole('textbox', { name: '消息', exact: true })).toBeEnabled();
});

test('真实模型连接错误后核对服务端终态并解锁输入', async ({ page }) => {
  await mockWorkspace(page);
  let failed = false;
  await page.route('**/api/chat/stream', async route => {
    failed = true;
    return route.fulfill({ contentType: 'text/event-stream', body:
      'event: error\ndata: ' + JSON.stringify({ code: 'llm_error', message: 'Connection error.' }) + '\n\n' });
  });
  await page.route('**/api/sessions/*/state', route => !failed ? route.fulfill({ status: 404, json: { detail: 'not found' } }) : route.fulfill({ json: {
    session_id: new URL(route.request().url()).pathname.split('/')[3],
    status: failed ? 'failed' : 'completed', messages: [], pending_approvals: [], tool_results: [],
  } }));
  await page.goto('/');
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  await expect(input).toBeEnabled(); await input.fill('查询库存'); await input.press('Enter');
  await expect(page.getByText('Connection error.', { exact: true })).toBeVisible();
  await expect(input).toBeEnabled();
});
test('连接错误后的中断任务保留输入锁定并提供继续恢复', async ({ page }) => {
  await mockWorkspace(page);
  let failed = false;
  await page.route('**/api/chat/stream', async route => {
    failed = true;
    return route.fulfill({ contentType: 'text/event-stream', body:
      'event: error\ndata: ' + JSON.stringify({ code: 'llm_error', message: 'Connection error.' }) + '\n\n' });
  });
  await page.route('**/api/sessions/*/state', route => !failed ? route.fulfill({ status: 404, json: { detail: 'not found' } }) : route.fulfill({ json: {
    session_id: new URL(route.request().url()).pathname.split('/')[3],
    status: failed ? 'interrupted' : 'completed', messages: [], pending_approvals: [], tool_results: [],
  } }));
  await page.goto('/');
  const input = page.getByRole('textbox', { name: '消息', exact: true });
  await expect(input).toBeEnabled(); await input.fill('查询库存'); await input.press('Enter');
  await expect(page.getByText('Connection error.', { exact: true })).toBeVisible();
  await expect(input).toBeDisabled();
  await expect(page.getByRole('button', { name: '继续恢复', exact: true })).toBeEnabled();
});
