"""M1 的核心实现文件：手写 agent loop（第 2–3 周完成）。

实现目标（详见 VIE 仓库 docs/agent-project-plan.md §6）：

1. 流式对话：AsyncOpenAI + OpenAI 兼容端点（GLM），SSE 逐 token 转发
2. 工具调用循环：Pydantic 定义签名 → 自动生成 JSON Schema → 注入请求
   → 解析 tool_calls → asyncio 执行 → 结果回填 → 循环
3. 防护：max steps 防死循环；单工具执行超时（asyncio.wait_for）
4. 结构化输出：Pydantic 强约束解析
5. 错误回填：工具异常转成"给模型看的错误信息"，让它自我纠正而非死循环

刻意留空：这是学习型项目，实现过程本身就是产出。
约定：先写测试（respx mock，不烧真实 token）再写实现。
"""
