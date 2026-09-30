# ADR-0003：可观测 v1 用本地 JSONL trace，Langfuse 推迟到评测起步

- 状态：已接受
- 日期：2026-09-30

## 背景

ADR-0001 已定终态可观测方案：Langfuse 自托管 + OpenTelemetry GenAI 语义约定。M1 第 4 周需要交付"一次完整任务的 trace 可回放，成本/延迟有数字"（M1 验收线），问题是：可观测的第一步直接上 Langfuse，还是先做一层本地的轻量落盘？

约束：单人业余时间开发；先跑通自托管 Langfuse（Docker Compose + 迁移 + SDK 接入）至少占掉一周的运维配额，而 M1 阶段只有单机、单进程、低频调用的场景。

## 决策

v1 用 `agent_core/trace.py`：JSONL 事件溯源落盘（一行一个 JSON 记录、追加写、逐行 flush），CLI 提供 `erpilot replay` 回放；Langfuse 推迟到 M3 评测集起步时接入。

记录模型 v1（`run_id` 关联一次 `agent.run`）：

| 记录 | 关键字段 |
|---|---|
| `run_start` | model、messages（开跑时历史快照） |
| `step_start` / `step_end` | step、text、usage、cost、duration_ms |
| `tool_call` | id、name、arguments、content、ok、duration_ms |
| `run_end` | steps、completed、usage、cost、duration_ms、messages（完整历史） |
| `run_error` | error（异常留痕后原样抛出） |

## 理由

- **零依赖零部署**：trace.py 约 200 行，标准库实现；Langfuse 自托管要 Docker + Postgres + ClickHouse + 对象存储，M1 的收益撑不起这个运维面积
- **故障留痕最可靠**：逐行 flush 的追加写意味着进程崩溃、断网、上游 5xx 时已发生的记录都在——本周两次真实上游故障（APIError / 502 upstream_error）都被 `run_error` 记录即为实证；而"观测管道自身不可达"恰恰是故障高发时刻
- **格式自有，不被锁定**：JSONL 记录与 loop 事件一一对应，后续接 Langfuse/OTel GenAI 时把 recorder 替换/并联为一个 sink 即可，字段映射是自己定义的
- **回放即验收**：`erpilot replay` 把流水还原成"用户提问 → 每轮工具调用与 token/成本 → 最终回答"的可读对话，直接满足 M1 验收线，不依赖任何 UI

## 备选与放弃原因

- 直接自托管 Langfuse：终态方案提前落地，但 M1 无多任务对比、无评测看板需求，一周运维配额性价比过低
- SQLite：结构化查询强，但 schema 迁移是额外负担，且"文本流水按行追加"天然适合 JSONL，用 SQLite 属于过度设计
- 只打日志不上文件：日志轮转/级别会切碎 run 边界，无法整任务回放

## 后果

- 没有跨进程聚合与可视化：看趋势要么 `jq` 聚合，要么等 Langfuse 接入（M3）
- trace 文件无轮转与清理策略，靠 .gitignore 兜底不入库，量大后手工清理
- M3 接 Langfuse 时，本模块降级为兜底 sink（本地留档 + 远程上报双写），删除窗口不存在
