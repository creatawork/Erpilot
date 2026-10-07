"""Shared AgentEvent protocol for graph, trace, CLI and API."""

from dataclasses import dataclass

from agent_core.llm import TextDelta, ToolCall, Usage


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """一段增量思考文本（步骤内）。思考不拼入正文、不进 final_answer，
    默认不落 trace 与展示日志；端点无思考字段时不会产出，消费方据此降级。"""

    step: int
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """模型请求了一次工具调用，即将执行。"""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolExecuting:
    """工具即将真正执行：参数校验通过、无需审批或审批已批准，第一次 handler 调用前发出。

    与 ToolCallStarted 的分工：started 表示"已取得完整调用请求"（校验可能尚未通过），
    executing 表示 handler 马上被调用。校验失败、未知工具、审批拒绝不产出本事件；
    内部重试不重复产出。
    """

    call_id: str
    name: str


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """工具执行完毕；ok=False 时 content 是回填给模型的错误信息。

    call_id 与 ToolCallStarted.call.id 对应——并行调用完成顺序不定，
    消费方靠它把 started/finished 精确配对。display 是展示适配器产物
    （设计 5.2），content 仍保留原始结果供模型与现有消费者使用。
    """

    call_id: str
    name: str
    content: str
    ok: bool = True
    display: dict | None = None


@dataclass(frozen=True, slots=True)
class StepStarted:
    """一轮 LLM 请求开始（step 从 1 计）。"""

    step: int


@dataclass(frozen=True, slots=True)
class StepEnd:
    """一轮 LLM 生成结束：本步 usage 与耗时（含该步前的上下文压缩）。"""

    step: int
    usage: Usage | None = None
    duration_ms: float = 0.0


@dataclass(frozen=True, slots=True)
class LoopEnd:
    """循环终止事件；completed=False 表示触发 max_steps 防护。"""

    steps: int
    usage: Usage | None = None
    completed: bool = True


@dataclass(frozen=True, slots=True)
class ApprovalPending:
    """一次写调用等待人工审批（M4 第 2 周，ADR-0005/0006）。

    事件产出后本轮 run 在此挂起：决策经审批门 respond(pending_id, decision)
    回填前，后续事件不再产出。call_id 与 ToolCallStarted.call.id 配对；
    pending_id 是消费方回填决策的凭据。
    """

    call_id: str
    pending_id: str
    tool: str
    risk: str
    arguments: dict


@dataclass(frozen=True, slots=True)
class ApprovalResolved:
    """审批决策已回填：批准 → 工具即将执行；拒绝 → 回填未执行结果。"""

    call_id: str
    pending_id: str
    tool: str
    approved: bool
    reason: str = ""


AgentEvent = (
    TextDelta
    | ReasoningDelta
    | StepStarted
    | StepEnd
    | ToolCallStarted
    | ToolExecuting
    | ToolCallFinished
    | ApprovalPending
    | ApprovalResolved
    | LoopEnd
)
