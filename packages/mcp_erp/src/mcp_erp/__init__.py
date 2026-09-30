"""mcp_erp：ERP 能力的 MCP 工具层（M3–M5 实现）。

设计约束（三个深方向之一：工具设计）：

- 粒度：15~20 个工具，每个粒度选择都要在 ADR 里给出理由
- 幂等：创建类工具（建订单、改库存）必须带幂等键
- 错误信息是给模型看的：结构化错误码 + 可操作的下一步提示，
  目标是"错误自愈率"（模型拿到错误后下一轮修好的比例）
- 返回值信息密度：只返回模型需要的字段，超长截断有策略

M3 第 2 周现状：只读 10 工具（server.py），agent 侧经 bridge.py 以 MCP
客户端消费；写操作（幂等键等约束的用武之地）在 M4+ 随 HITL 进入。
"""

from mcp_erp.bridge import build_agent_tools, build_agent_tools_async
from mcp_erp.server import create_server

__all__ = ["build_agent_tools", "build_agent_tools_async", "create_server"]
