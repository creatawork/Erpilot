"""Shared AgentEvent protocol for graph, trace, CLI and API."""

from dataclasses import dataclass

from agent_core.llm import TextDelta, ToolCall, Usage


@dataclass(frozen=True, slots=True)
class ToolCallStarted:
    """模型请求了一次工具调用，即将执行。"""

    call: ToolCall


@dataclass(frozen=True, slots=True)
class ToolCallFinished:
    """工具执行完毕；ok=False 时 content 是回填给模型的错误信息。

    call_id 与 ToolCallStarted.call.id 对应——并行调用完成顺序不定，
    消费方靠它把 started/finished 精确配对。
    """

    call_id: str
    name: str
    content: str
    ok: bool = True


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
    | StepStarted
    | StepEnd
    | ToolCallStarted
    | ToolCallFinished
    | ApprovalPending
    | ApprovalResolved
    | LoopEnd
)


