"""mcp_erp：ERP 能力的 MCP 工具层（M3–M5 实现）。

设计约束（三个深方向之一：工具设计）：

- 粒度：15~20 个工具，每个粒度选择都要在 ADR 里给出理由
- 幂等：可重放的写请求保留 client_token，bridge 为同次执行自动生成并复用
- 错误信息是给模型看的：结构化错误码 + 可操作的下一步提示，
  目标是"错误自愈率"（模型拿到错误后下一轮修好的比例）
- 返回值信息密度：只返回模型需要的字段，超长截断有策略

当前：16 个只读工具 + 默认关闭的 4 个写工具（server.py），agent 侧经 bridge.py
以 MCP 客户端消费，写入先过审批门；持久化幂等与 SQLite 写锁见 ADR-0007。
"""

from mcp_erp.bridge import build_agent_tools, build_agent_tools_async
from mcp_erp.server import create_server

__all__ = ["build_agent_tools", "build_agent_tools_async", "create_server"]
