# Erpilot

> 会请示的 ERP 智能体（an approval-aware ERP agent）—— agent 应用开发求职作品集项目（2026-09 启动，工期 12 个月）

**一句话**：运行在 mini-ERP（商品 / 库存 / 订单）上的业务智能体：FastMCP 工具层 + 分级人工审批（HITL）+ 全程评测驱动；前端提供流式对话、工具时间线与审批卡片。

## 为什么叫 Erpilot

ERP + (co)pilot。产品隐喻是一位**掌柜**：掌柜打理店铺日常——查库存、对订单、算报价，样样精通；但**动账动货的事，必须请示东家**。

这正是本项目的核心设计：只读操作自动执行，低风险操作批量确认，资金相关操作单笔审批。

## 技术栈

当前实现：Python 3.12 · FastAPI · 手写 agent loop · FastMCP · SQLite / SQLAlchemy ·
OpenAI 兼容 SDK · GLM-5.3 Flash · 本地 JSONL（可选 Langfuse sink）· React + TypeScript。

LangGraph、PostgreSQL / pgvector、LiteLLM 属于后续路线，尚未接入。

选型理由见 [`docs/adr/0001-tech-stack.md`](docs/adr/0001-tech-stack.md)。

## 仓库结构

```
├── apps/
│   ├── api/            # FastAPI：agent 宿主、会话管理、SSE
│   ├── cli/            # erpilot CLI：typer + rich（组合层，默认 MCP 真数据）
│   └── web/            # React + TS：流式对话、工具时间线、审批卡片（M1 第 4 周初始化）
├── packages/
│   ├── agent_core/     # 手写 agent loop（不依赖任何业务包）
│   ├── mcp_erp/        # FastMCP Server：ERP 能力 → MCP 工具 + agent 桥
│   ├── erp_store/      # 领域模型 + 种子数据（商品/库存/订单，SQLite）
│   └── evals/          # 评测集 + runner + 报告
└── docs/adr/           # 架构决策记录
```

## 快速开始

```bash
# 安装 uv（若未安装）：https://docs.astral.sh/uv/
uv sync --all-packages       # 创建虚拟环境并安装全部工作区依赖
cp .env.example .env         # 填入 ZHIPU_API_KEY
uv run pytest                # 单元测试（mock，不消耗 token）

# CLI：默认经 MCP 桥查询真数据（先 seed），trace 自动落盘 traces/*.jsonl
uv run --package erp-store python -m erp_store seed
uv run erpilot chat "订单 123 里买了什么？还有货吗？有货的话报个价"
uv run erpilot chat --tools demo                # 假 ERP 工具，仍调用真实模型
uv run erpilot chat --writes "给商品 A1001 入库 5 件"  # 终端审批后写入
uv run erpilot replay traces/<某个>.jsonl        # 把 trace 还原成可读对话

# API + 前端：SSE 链路
ERPILOT_TOOLS=mcp uv run --package erpilot-api uvicorn erpilot_api.main:app --reload
# 启用审批写入时再设置 ERPILOT_WRITES=1；PowerShell 用 $env:ERPILOT_WRITES="1"
# 验证：http://127.0.0.1:8000/healthz
cd apps/web && npm install && npm run dev      # http://localhost:5173

# MCP server 独立进程（stdio，给外部 MCP 客户端）
uv run --package mcp-erp python -m mcp_erp serve

# 统一真实模型基线：29 读 + 3 写拒绝 + 3 脚本化审批 = 35 条（消耗 token）
uv run python -m evals --budget 0.1 --timeout 30
uv run python -m evals --budget 0.02 --case app-01 --case app-02  # 定点报告
uv run python -m evals --suite write-preflight --budget 0.01  # 写前安全预检
# 报告落 reports/evals/；标注标准见 docs/eval-annotation-guide.md
```

### 无密钥演示与写入可靠性

```bash
uv run python scripts/demo.py                  # 报价 / 批准 / 拒绝 / 错误恢复
uv run python scripts/demo.py --recovery       # 加跑审批重启、提交后重启、拒绝重启
# 浏览器演示：先在 apps/web 中 npm ci && npm run build，再回到仓库根目录
uv run python scripts/demo.py --serve --port 8765
# 打开 http://127.0.0.1:8765；输入“报价”“批准”“拒绝”“恢复”并点击审批按钮
```

该演示使用脚本化模型，执行真实 loop → MCP → 审批 → 临时 SQLite；
不消耗模型 token，不改业务库，也不代表真实模型成功率。trace 与断言结果落
`traces/demo/`，浏览器 trace 落 `traces/browser-demo/`。

四个写工具支持 `client_token`：同键同参数返回原结果，同键不同参数报
`idempotency_conflict`；bridge 自动为单次调用生成并复用 token。业务变更与
幂等记录同事务提交，SQLite 写事务在读取库存之前取得写锁，避免并发超卖。
写工具自动重试只在具备幂等契约时启用；整轮写任务不自动重跑。

API 会话、任务、写意图、审批和事件已持久化到 `data/runs.db`。审批绑定原
run/session、参数指纹及版本，TTL 默认 30 分钟；同决定重试幂等，相反决定冲突。
刷新通过快照和事件游标恢复原任务，断开 SSE 不取消后台执行。提交结果不明时
先按原 token 对账；结果未知不显示成功，已提交业务不会因取消而撤销。

目前仅支持本机单用户、单 API 执行进程；不要启动多个 worker。硬终止后原
执行者租约最多保留 30 秒，前端随后重试恢复。恢复须复用原业务库与运行库，
不要重新 seed；不兼容版本停止恢复并保留记录。身份认证与多用户授权尚未接入。
离线证据、恢复 API 和待人工验收项见 [恢复验收交接](docs/plans/恢复验收交接.md)。

Langfuse 双写（可选）：`.env` 配置 `LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
LANGFUSE_HOST` 后，CLI / API / 评测的 trace 在本地 JSONL 兜底之外同步上报远程
（需 `uv sync --package agent-core --extra langfuse`）；未配置时零成本纯本地。

M1 各周验收入口：第 1–3 周为 `python -m agent_core`（最简演示，多步任务 + 并行工具）；
第 4 周为上述 `erpilot` CLI 与 API/前端链路。

## 下一阶段规划

持久化恢复、前端重连及四工具故障回归已开发；真实模型写错误与真人审批
验收仍待执行。新增 `--suite write-errors` 与原 35 条基线分别出报告，操作见
[真实验收手册](docs/plans/真实验收操作手册.md)。本轮未开展真机测试。
后续按 [总体规划](tasks/plan.md)、[任务清单](tasks/todo.md) 补验收，再评估框架、
数据库和部署访问控制；当前没有生产上线结论。

## 路线图

| 里程碑 | 内容 |
|---|---|
| M1–2 | 手写 agent loop：流式、工具调用、结构化输出、本地 trace |
| M3–5 | FastMCP Server + 工具设计精研（15~20 个工具）+ 评测集起步 |
| M6–8 | LangGraph 重构编排 + Postgres checkpointer + HITL 审批流 |
| M9–11 | 评测驱动迭代（成功率曲线、成本优化）+ prompt injection 防护 |
| M12 | 部署上线、项目档案页与系列文章收口 |

## 文章与决策

- 所有架构决策记录在 `docs/adr/`（0001–0008；0008 为恢复契约草案，待负责人接受），范围冻结与启动计划见 [`docs/agent-project-plan.md`](docs/agent-project-plan.md)
- 评测集：人工标注标准 [`docs/eval-annotation-guide.md`](docs/eval-annotation-guide.md) + 工具卡 [`docs/tool-cards.md`](docs/tool-cards.md) + 错误自愈记录 [`docs/error-recovery-log.md`](docs/error-recovery-log.md)
- 系列文章发布于个人站点 [Vie](https://vie-vibe.cn)，文稿随仓库维护：
  1. 《手写 Agent Loop：从一次 API 调用到多步任务》—— [`docs/articles/01-handwritten-agent-loop.md`](docs/articles/01-handwritten-agent-loop.md)
- 撰写规则见 [`docs/articles/writing-rules.md`](docs/articles/writing-rules.md)（禁虚构、客观口吻、技术正确性、发布前核对清单）
