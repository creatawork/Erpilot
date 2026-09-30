# 手写 Agent Loop：从一次 API 调用到多步任务

> Erpilot 系列第 1 篇 · 2026-09-30
>
> 代码在 [erpilot/packages/agent_core](https://github.com/creatawork/erpilot)（M1 第 1–4 周），约 1200 行 Python + 44 项单测。
> 这一版刻意不用任何 agent 框架——为什么、以及什么时候我会换上 LangGraph，文末说。

大模型 API 本身只会一件事：你发一段消息列表，它回一段消息。所谓 agent，是在这件事外面套了一个循环——**让它能用工具、能看结果、能接着想**。市面上所有 agent 框架，剥开 UI 和生态，核心都是这个循环。这篇文章把它从零写一遍：从一次流式 API 调用开始，到多步工具调用、防死循环、错误回填、上下文压缩，最后落一份可回放的 trace。

## 一、先让流式跑起来

直接调 `chat.completions.create` 拿完整回复当然可以，但 agent 的体验生死线是**首字延迟**——用户必须看到字在往外蹦，才相信程序没死。所以第一件事是流式：

```python
stream = await client.chat.completions.create(
    model=model, messages=messages, stream=True,
    stream_options={"include_usage": True},
)
async for chunk in stream:
    if chunk.choices and chunk.choices[0].delta.content:
        yield TextDelta(chunk.choices[0].delta.content)
    if chunk.usage is not None:
        usage = Usage(...)   # 最后一个包带 token 计量
```

第一个设计决定在这里：**不要把 SDK 的 chunk 对象泄漏出去**。定义自己的事件模型——

```python
@dataclass(frozen=True, slots=True)
class TextDelta:   text: str
@dataclass(frozen=True, slots=True)
class ToolCall:    id: str; name: str; arguments: str
@dataclass(frozen=True, slots=True)
class StreamEnd:   usage: Usage | None
```

`stream_chat()` 产出 `TextDelta | ToolCall | StreamEnd`，并且保证**恰好以一个 StreamEnd 结束**。这条小约定后来救了我好几次：所有下游（CLI 渲染、SSE 转发、trace 落盘）都只需要处理三种事件、都只需要等一个确定的终止信号。供应商换成智谱、DeepSeek 还是中转端点，下游一行不改。

顺带处理一个容易忽略的脏活：流式模式下工具调用是**分片到达**的——第一个包只有 `id` 和函数名，参数 JSON 分成好几段。要按 `index` 把碎片归并回完整的 ToolCall：

```python
slot = pending.setdefault(delta.index, {"id": None, "name": None, "arguments": []})
if delta.function.arguments:
    slot["arguments"].append(delta.function.arguments)
```

## 二、工具循环：agent 的本体

有了流式，agent loop 的骨架其实只有十几行伪代码：

```
while 步数 < 上限:
    回复 = 流式请求(messages, tools=schema)
    if 回复不含工具调用: return 回复          # 模型认为可以收口了
    执行每个工具调用, 把结果以 role=tool 消息回填
```

每一步有几个协议细节必须做对：

**schema 注入**。工具用 Pydantic 定义参数模型，`model_json_schema()` 自动生成 OpenAI tools 格式。一个小技巧：生成后递归剥掉所有 `title` 键——Pydantic 会给每个字段加 title，对注入请求是纯噪音，白花 token。

**回填必须成对**。模型发出工具调用后，历史里要有两条消息：assistant 消息带 `tool_calls`（它在请求什么），tool 消息带 `tool_call_id`（结果是什么）。漏掉一半或者 id 对不上，下一次请求直接被端点拒绝。这也是后面上下文压缩的约束来源：**裁剪只能按轮进行，不能把 tool 结果和它的父调用拆开**。

**校验放在执行前**。工具入参先过 Pydantic 校验，模型给错参数时，错误信息回填给它自己修正，而不是让 handler 炸出一个堆栈。

到这里，"查订单 123 的状态"就能跑通了：模型请求 `get_order_status` → 执行 → 回填 → 第二轮生成"订单 123 已发货"。两步，这就是一个最小的 agent。

## 三、防护：循环的第一课是刹车

无限循环不是假设，是常态。模型会在同一处反复调用同一个工具、或者每次都发明一个新参数。三道闸：

**max_steps 防死循环**——到步数上限就停，事件里带 `completed=False`，上层可以提示用户而不是无声失败。

**单工具超时**——`asyncio.wait_for(tool.handler(args), timeout)`。任何工具作者都可能写出死循环或慢查询，不能让一个工具挂死整个 agent。

**异常永不外抛，转成结构化错误回填**。这是我认为 loop 设计里最重要的一个决定。工具抛异常、参数校验失败、超时——统统转成这样的 JSON 塞回对话：

```json
{"error": {"type": "timeout", "message": "工具执行超时（>30s，已重试 1 次）"}}
```

为什么不直接 raise？因为**模型看到错误是可以自救的**：参数错了会改参数，工具坏了会换一条路，甚至直接告诉用户"库存系统暂时查不了"。把异常抛给调用方等于放弃自愈能力，而结构化的 `type` 字段让模型可以稳定地区分"该重试的瞬态故障"和"该改道的确定性错误"。

顺着这个区分就有了重试策略 v1：`timeout` / `execution` 视为瞬态，按次数 + 退避自动重试；`validation` / `unknown_tool` 是确定性错误，重试毫无意义，立刻回填让模型修正。

## 四、并行工具调用

一个真实任务——"订单里买了什么？有货吗？报个价"——模型第一轮会同时要订单明细，第二轮同时查库存和价格。第二轮的多个调用**互相独立，串行执行纯属浪费**：

```python
pending = [self._execute_indexed(i, c) for i, c in enumerate(calls)]
for done in asyncio.as_completed(pending):
    index, content, ok = await done
    yield ToolCallFinished(...)   # 完成一个转发一个
```

三个顺序要分清：`Started` 事件按**调用顺序**发（模型请求了什么）；`Finished` 按**完成顺序**发（谁先跑完谁先走，前端时间线如实呈现）；回填消息按**调用顺序**放（协议要求 tool 消息顺序无所谓，但和 assistant 的 tool_calls 一一对应最稳）。

实测这一步把三工具任务的耗时从 3 次串行往返压到 1 次并行往返——LLM 请求占掉任务时间的 90%，工具本身几微秒，省的全是等待模型的时间。

## 五、上下文压缩：把历史塞回预算里

工具结果是很肥的——一个订单 JSON 几百 token，聊十几轮上下文就爆了。压缩策略 v1 是两层防线：

1. **单条截断**：超长的 tool 结果保留头尾（头 2/3 尾 1/3），中间换成省略标记——JSON 的结构信息通常在头尾
2. **整轮丢弃**：总体超预算时，从最老的轮次开始一整轮一整轮地丢（一条 user 消息连同它引发的 assistant/tool 消息）。丢弃的边界**绝不能落在工具交换中间**，否则就违反了第二节说的成对约束

system 消息和最近几条永远保留；发生过丢弃就插入一条 system 提示"更早的对话已被省略"，让模型知道自己失忆了。token 估算用 `len/3` 的粗启发式——中英混合场景够用，等接了 LiteLLM 再换真 tokenizer。

## 六、可观测：没有 trace 的 agent 不可调试

agent 的行为是非确定的：同一个问题，今天两步明天三步，工具调用顺序每次都可能不同。**出了错，你需要的不是堆栈，是完整的过程回放**。

第 4 周加了 trace 模块，设计上只做一件事：旁观事件流，原样落盘。

```python
recorder = JsonlTraceRecorder(path, model=config.model)
async for event in recorder.run(agent, messages):   # 事件原样透传
    ...
```

一行一个 JSON 记录，追加写、逐行 flush：`run_start`（开跑时的历史快照）→ `step_start` / `step_end`（每轮文本、token、成本、耗时）→ `tool_call`（每次调用的入参出参与耗时）→ `run_end`（结束时完整历史）。异常也留痕：`run_error` 记完再原样抛出。

JSONL 这个格式被质疑过"不如上 Langfuse"。但对 M1 来说：逐行 flush 意味着**进程崩了已写的行还在**（观测管道最需要工作的时刻恰恰是故障时刻）；裸文本意味着 `grep`/`jq`/`tail -f` 全都能用；而且格式是我自己定义的，将来映射到 OTel GenAI 语义约定或者 Langfuse 只是换个 sink。这个取舍我写进了 [ADR-0003](https://github.com/creatawork/erpilot/blob/main/docs/adr/0003-local-jsonl-trace-first.md)。

回放长这样（`erpilot replay traces/xxx.jsonl`）：

```
== run ac89906e358e · glm-5.3-flash · 2026-09-30T10:01:38 ==
[用户] 订单 123 里买了什么？这些商品现在还有货吗？…
--- 第 1 轮 ---
[工具✓] get_order_status({"order_id": "123"}) → {"status": "待发货", ...}（4123ms）
== 结束：3 步 · 共 1240 tok · ≈¥0.0003 · 18.2s ==
```

顺带一个真实案例：写本文当天，上游中转端点连续挂掉两次（一次 APIError、一次 502 upstream_error），两次都被 `run_error` 完整留痕——**trace 的第一次实战价值，就是记录它自己没跑成的那两次**。

## 七、验收：三条入口，一个循环

同一套 loop + 同一批工具，三种消费方式：

- **CLI**（`erpilot chat`）：rich 渲染流式输出与工具时间线，trace 自动落盘
- **FastAPI**（`POST /api/chat/stream`）：loop 事件逐个转成 SSE 帧转发，前端按 `event` 名分发
- **React 页**：fetch + ReadableStream 手解 SSE（`EventSource` 不支持 POST），增量文本、工具时间线、token/成本全都在

前后端协议就是 loop 的事件模型加两个信封事件（`start` / `done`），字段表在 `erpilot_api/events.py` 的 docstring 里，TypeScript 侧的镜像类型在 `apps/web/src/protocol.ts`——两侧必须同步改，这是目前协议唯一的"文档"。

## 写在最后：什么时候轮到框架

手写这 600 行之后，我很清楚框架在替你做什么：流式事件归并、工具执行的并发调度、错误回填——这些手写过一遍就不再是魔法。但也同样清楚**手写解决不了什么**：会话持久化、断点恢复、人工审批的 interrupt/恢复，这些是 LangGraph 的 checkpointer 和 interrupt 真正在行的东西，恰好是 Erpilot 的核心卖点（分级 HITL 审批）在 M6 才需要的。

所以计划是：M6 把 loop 内核换成 LangGraph，事件协议、trace 格式、前端、评测**全部不动**——这层协议自主权就是手写这两周买下的东西。迁移完成后我会再写一篇对比，验证这个判断对不对。

下一篇：《给 ERP 写一个 MCP Server》。
