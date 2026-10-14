# 当前工作台改版验证

日期：2026-10-12。验证当前工作区，不代表未提交改动已合并；本次未调用真实模型或执行 ERP 写入。

| 验证 | 本次结果 |
|---|---|
| `apps/web`：`npm test` | 73 passed，0 failed，0 skipped |
| `apps/web`：`npm run build` | 类型检查与生产构建通过；入口 JS 1,282.66 KB / gzip 412.58 KB，存在体积提示 |
| 默认 `npm run test:ui` | 7 项均在启动前失败：本机缺少 Playwright 要求的 `chromium_headless_shell-1248` |
| 指定本机 Chromium 后 `npm run test:ui -- --workers=1` | 7 passed，24.1s |
| RAG、MCP 操作工具与 ERP 业务操作定向回归 | 44 passed，9.17s；不代表全仓测试已重跑 |
| CI YAML 结构、安装顺序、失败证据路径与新增文档链接 | 本地检查通过；Playwright 本地帮助确认支持 `install --with-deps chromium` |

成功浏览器运行使用：

```text
set PLAYWRIGHT_CHROMIUM_EXECUTABLE=C:\Users\V\AppData\Local\ms-playwright\chromium-1243\chrome-win64\chrome.exe
npm run test:ui -- --workers=1
```

覆盖桌面分栏、窄屏抽屉及焦点恢复、流式 Markdown 与危险内容过滤、待审批锁定、历史恢复失败、连接异常终态解锁及中断任务保留锁定。使用模拟 API；其中“真实模型连接错误”是模拟该错误的测试名称，不代表进行了真实模型调用。

首轮默认浏览器失败没有通过更改断言或跳过测试处理；第二轮仅指定现有配置支持的浏览器路径，并限制本机并行度。已结束首轮遗留的本轮测试服务。

后端首轮 24 passed / 20 setup errors，均因系统临时目录不可写；复跑改用工作区内全新的临时目录和独立 cache，44 项通过。复跑命令（临时目录须为新的、未使用的目录）：

```text
.venv\Scripts\python.exe -m pytest packages/rag/tests packages/mcp_erp/tests/test_operation_tools.py packages/erp_store/tests/test_operations.py --basetemp=E:\workspace\erpilot\.playwright-mcp\pytest-b5-20261012-review -o cache_dir=E:\workspace\erpilot\.playwright-mcp\pytest-cache-b5-20261012 --tb=short
```

本次 CI 配置补充匹配版本 Chromium 的安装、7 项浏览器回归入口和失败证据上传。**GitHub 上的 CI 尚未运行**；本机已有 Chromium 的通过结果不替代 Linux CI 验收。

B5 目前只有设计规格，未修改取消业务实现；整体 B5 不能标为完成。原工作区已有的评测及界面变更未被批量提交。
