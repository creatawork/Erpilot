# Erpilot

> 会请示的 ERP 智能体（an approval-aware ERP agent）—— agent 应用开发求职作品集项目（2026-09 启动，工期 12 个月）

**一句话**：运行在 mini-ERP（商品 / 库存 / 订单）上的业务智能体：FastMCP 工具层 + 分级人工审批（HITL）+ 全程评测驱动；前端提供流式对话、工具时间线与审批卡片。

## 为什么叫 Erpilot

ERP + (co)pilot。产品隐喻是一位**掌柜**：掌柜打理店铺日常——查库存、对订单、算报价，样样精通；但**动账动货的事，必须请示东家**。

这正是本项目的核心设计：只读操作自动执行，低风险操作批量确认，资金相关操作单笔审批。

## 技术栈

Python 3.12 · FastAPI · 手写 agent loop → LangGraph · FastMCP · PostgreSQL + pgvector · LiteLLM · GLM-5.3 Flash · Langfuse · React + TypeScript

选型理由见 [`docs/adr/0001-tech-stack.md`](docs/adr/0001-tech-stack.md)。

## 仓库结构

```
├── apps/
│   ├── api/            # FastAPI：agent 宿主、会话管理、SSE
│   └── web/            # React + TS：流式对话、工具时间线、审批卡片（M1 第 4 周初始化）
├── packages/
│   ├── agent_core/     # 手写 agent loop（不依赖业务包）
│   ├── mcp_erp/        # FastMCP Server：ERP 能力 → MCP 工具
│   ├── erp_store/      # 领域模型 + 种子数据（商品/库存/订单）
│   └── evals/          # 评测集 + runner + 报告
└── docs/adr/           # 架构决策记录
```

## 快速开始

```bash
# 安装 uv（若未安装）：https://docs.astral.sh/uv/
uv sync --all-packages       # 创建虚拟环境并安装全部工作区依赖
cp .env.example .env         # 填入 ZHIPU_API_KEY
uv run pytest                # 单元测试（mock，不消耗 token）
uv run --package erpilot-api uvicorn erpilot_api.main:app --reload
# 验证：http://127.0.0.1:8000/healthz
```

## 路线图

| 里程碑 | 内容 |
|---|---|
| M1–2 | 手写 agent loop：流式、工具调用、结构化输出、本地 trace |
| M3–5 | FastMCP Server + 工具设计精研（15~20 个工具）+ 评测集起步 |
| M6–8 | LangGraph 重构编排 + Postgres checkpointer + HITL 审批流 |
| M9–11 | 评测驱动迭代（成功率曲线、成本优化）+ prompt injection 防护 |
| M12 | 部署上线、项目档案页与系列文章收口 |

## 文章与决策

- 所有架构决策记录在 `docs/adr/`，范围冻结与启动计划见 VIE 仓库 `docs/agent-project-plan.md`
- 系列文章《手写 Agent Loop》《给 ERP 写一个 MCP Server》《Agent 的评测怎么做》等发布于个人站点 [Vie](https://vie-vibe.cn)
