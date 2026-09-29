"""agent_core：手写 agent loop。

刻意不依赖任何业务包——它是可复用的最小 agent 运行时，
业务语义（ERP 工具、审批规则）全部留在 mcp_erp / apps 侧。
"""
