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
# 报告落 reports/evals/；标注标准见 docs/eval-annotation-guide.md
```

### 无密钥演示与写入可靠性

```bash
uv run python scripts/demo.py                  # 报价 / 批准 / 拒绝 / 错误恢复
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

审批是内存中的单用户原型：正常决策、断连和取消会释放等待请求；未完成会话
恢复到本轮前历史。进程重启恢复、身份认证及审批归属校验尚未实现，当前适合
本机展示。下一阶段按持久化任务/审批、恢复协议、部署验收推进，见
[可靠性复盘](docs/write-reliability.md) 与 [ADR-0007](docs/adr/0007-write-reliability.md)。

Langfuse 双写（可选）：`.env` 配置 `LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY /
LANGFUSE_HOST` 后，CLI / API / 评测的 trace 在本地 JSONL 兜底之外同步上报远程
（需 `uv sync --package agent-core --extra langfuse`）；未配置时零成本纯本地。

M1 各周验收入口：第 1–3 周为 `python -m agent_core`（最简演示，多步任务 + 并行工具）；
第 4 周为上述 `erpilot` CLI 与 API/前端链路。

## 路线图

| 里程碑 | 内容 |
|---|---|
| M1–2 | 手写 agent loop：流式、工具调用、结构化输出、本地 trace |
| M3–5 | FastMCP Server + 工具设计精研（15~20 个工具）+ 评测集起步 |
| M6–8 | LangGraph 重构编排 + Postgres checkpointer + HITL 审批流 |
| M9–11 | 评测驱动迭代（成功率曲线、成本优化）+ prompt injection 防护 |
| M12 | 部署上线、项目档案页与系列文章收口 |

## 文章与决策

- 所有架构决策记录在 `docs/adr/`（0001–0007，含挂起审批与写入可靠性），范围冻结与启动计划见 [`docs/agent-project-plan.md`](docs/agent-project-plan.md)
- 评测集：人工标注标准 [`docs/eval-annotation-guide.md`](docs/eval-annotation-guide.md) + 工具卡 [`docs/tool-cards.md`](docs/tool-cards.md) + 错误自愈记录 [`docs/error-recovery-log.md`](docs/error-recovery-log.md)
- 系列文章发布于个人站点 [Vie](https://vie-vibe.cn)，文稿随仓库维护：
  1. 《手写 Agent Loop：从一次 API 调用到多步任务》—— [`docs/articles/01-handwritten-agent-loop.md`](docs/articles/01-handwritten-agent-loop.md)
- 撰写规则见 [`docs/articles/writing-rules.md`](docs/articles/writing-rules.md)（禁虚构、客观口吻、技术正确性、发布前核对清单）
