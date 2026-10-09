"""M1 第 1–2 周产出：最小 LLM client——AsyncOpenAI 流式补全 + usage 统计 + 工具调用 + 结构化输出。

与 loop.py 的分工：本模块只封装"一次对话补全"的流式调用与计量；
多轮工具调用循环、防护策略是 loop.py 的内容。

事件模型：stream_chat 产出 StreamEvent（TextDelta | ToolCall | StreamEnd），
保证恰好以一个 StreamEnd 结束——第 4 周 FastAPI SSE 只需把事件逐个转发。
"""

import json
import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-5.3-flash"
DEFAULT_PROVIDER = "zhipu"


@dataclass(frozen=True, slots=True)
class ProviderProfile:
    """一个 OpenAI 兼容供应商的预设：端点、默认模型、读取 key 的环境变量名。

    多模型横向对比只需切 LLM_PROVIDER；base_url / model 仍可用 env 覆盖。
    不引入 LiteLLM：当前只需"切换 + 对比"，无自动降级/路由需求；若日后需要
    降级/路由再评估网关层。
    """

    base_url: str
    model: str
    api_key_env: str


# 仅收录 OpenAI 兼容端点；端点与默认模型以各家文档为准，计费价目在 prices.py 另行维护。
PROVIDERS: dict[str, ProviderProfile] = {
    "zhipu": ProviderProfile(DEFAULT_BASE_URL, DEFAULT_MODEL, "ZHIPU_API_KEY"),
    "deepseek": ProviderProfile(
        "https://api.deepseek.com/v1", "deepseek-chat", "DEEPSEEK_API_KEY"
    ),
    "qwen": ProviderProfile(
        "https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus", "DASHSCOPE_API_KEY"
    ),
}


@dataclass(frozen=True, slots=True)
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TextDelta:
    """一段增量回复文本。"""

    text: str


@dataclass(frozen=True, slots=True)
class ReasoningDelta:
    """一段增量思考文本（供应商 reasoning_content 字段）。

    独立于正文：不拼入 TextDelta，不写进 final_answer。
    端点或模型不提供思考内容时不会产出，消费方据此降级。
    """

    text: str


@dataclass(frozen=True, slots=True)
class ToolCall:
    """一次完整的工具调用请求（流式增量归并后的结果）。

    arguments 是协议原样的 JSON 字符串，由调用方解析校验。
    """

    id: str
    name: str
    arguments: str


@dataclass(frozen=True, slots=True)
class StreamEnd:
    """流的终止事件；端点未回 usage 时为 None。"""

    usage: Usage | None = None


StreamEvent = TextDelta | ReasoningDelta | ToolCall | StreamEnd


@dataclass(frozen=True, slots=True)
class StreamResult:
    """chat() 的一次性结果：完整回复 + 工具调用 + 计量。"""

    text: str
    usage: Usage | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class StructuredResult[ModelT: BaseModel]:
    """structured() 的结果：强约束解析后的数据 + 计量。"""

    data: ModelT
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class LLMConfig:
    api_key: str = field(repr=False)
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL
    # 思考展示是配置能力，默认关闭：确认端点/模型确实回 reasoning_content 后再开启
    thinking: bool = False

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """按 LLM_PROVIDER 选供应商预设（默认 zhipu），base_url / model 可用 env 覆盖。

        key 从该供应商对应的环境变量读取（zhipu→ZHIPU_API_KEY，
        deepseek→DEEPSEEK_API_KEY，qwen→DASHSCOPE_API_KEY）。默认 provider 下行为
        与旧版完全一致。
        """
        provider = os.environ.get("LLM_PROVIDER", DEFAULT_PROVIDER).lower()
        profile = PROVIDERS.get(provider)
        if profile is None:
            known = "、".join(sorted(PROVIDERS))
            raise RuntimeError(f"未知 LLM_PROVIDER={provider!r}：可选 {known}")
        api_key = os.environ.get(profile.api_key_env, "")
        if not api_key:
            raise RuntimeError(
                f"缺少 {profile.api_key_env}（LLM_PROVIDER={provider}）："
                "请在 .env 中填入该供应商的 API key"
            )
        return cls(
            api_key=api_key,
            base_url=os.environ.get("LLM_BASE_URL", profile.base_url),
            model=os.environ.get("LLM_MODEL", profile.model),
            thinking=os.environ.get("ERPILOT_THINKING", "").lower() in ("1", "true", "yes"),
        )


def _accumulate_tool_calls(pending: dict[int, dict[str, Any]], deltas: Sequence[Any]) -> None:
    """把流式 tool_calls 增量按 index 归并：id/name 在首包，arguments 分段拼接。"""
    for delta in deltas:
        slot = pending.setdefault(delta.index, {"id": None, "name": None, "arguments": []})
        if delta.id:
            slot["id"] = delta.id
        if delta.function:
            if delta.function.name:
                slot["name"] = delta.function.name
            if delta.function.arguments:
                slot["arguments"].append(delta.function.arguments)


def _strip_json_fence(text: str) -> str:
    """剥掉模型常见的 ```json 围栏；裸 JSON 原样返回。"""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    body = stripped.split("\n", 1)[1] if "\n" in stripped else ""
    return body.rstrip().removesuffix("```").strip()


class LLMClient:
    """对 AsyncOpenAI 的最小封装：流式补全 + usage 计量 + 工具调用 + 结构化输出。"""

    def __init__(self, config: LLMConfig, client: AsyncOpenAI | None = None) -> None:
        self._config = config
        self._client = client or AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)

    @property
    def config(self) -> LLMConfig:
        """只读配置（evals runner 按 config.model 计量成本）。"""
        return self._config

    async def stream_chat(
        self,
        messages: Sequence[ChatCompletionMessageParam],
        tools: Sequence[dict[str, Any]] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """流式补全：逐段产出 TextDelta / ReasoningDelta / ToolCall，最后恰好产出一个 StreamEnd。"""
        extra: dict[str, Any] = {"tools": list(tools)} if tools else {}
        stream = await self._client.chat.completions.create(
            model=self._config.model,
            messages=list(messages),
            stream=True,
            # OpenAI 官方端点需要 include_usage 才在流末尾回传 usage；智谱兼容端点默认
            # 就带。若换用的兼容端点拒绝该参数，删掉本行即可。
            stream_options={"include_usage": True},
            **extra,
        )
        pending: dict[int, dict[str, Any]] = {}
        usage: Usage | None = None
        async with stream:
            async for chunk in stream:
                choice = chunk.choices[0] if chunk.choices else None
                if choice and choice.delta:
                    reasoning = getattr(choice.delta, "reasoning_content", None)
                    if reasoning is None:
                        # openai SDK 的 pydantic 模型把扩展字段收进 model_extra
                        reasoning = (choice.delta.model_extra or {}).get("reasoning_content")
                    # 实测 glm-5.3-flash 端点默认回 reasoning_content 且不接受 thinking
                    # 请求参数；配置只控制是否解析产出，端点能力须另行实测
                    if reasoning and self._config.thinking:
                        yield ReasoningDelta(reasoning)
                    if choice.delta.content:
                        yield TextDelta(choice.delta.content)
                if choice and choice.delta.tool_calls:
                    _accumulate_tool_calls(pending, choice.delta.tool_calls)
                if chunk.usage is not None:
                    usage = Usage(
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                        total_tokens=chunk.usage.total_tokens,
                    )
                    break  # usage 包即收口（test_stream_stops_right_after_usage_chunk）
        for index in sorted(pending):
            slot = pending[index]
            yield ToolCall(
                id=slot["id"] or f"call_{index}",
                name=slot["name"] or "",
                arguments="".join(slot["arguments"]),
            )
        yield StreamEnd(usage=usage)

    async def chat(self, messages: Sequence[ChatCompletionMessageParam]) -> StreamResult:
        """收完整个流，返回完整文本、工具调用与 usage（CLI / 不需要逐 token 的场景用）。"""
        parts: list[str] = []
        calls: list[ToolCall] = []
        usage: Usage | None = None
        async for event in self.stream_chat(messages):
            match event:
                case TextDelta(text=text):
                    parts.append(text)
                case ReasoningDelta():
                    pass  # 思考不进入最终结果
                case ToolCall() as call:
                    calls.append(call)
                case StreamEnd(usage=final_usage):
                    usage = final_usage
        return StreamResult(text="".join(parts), usage=usage, tool_calls=calls)

    async def structured[ModelT: BaseModel](
        self,
        messages: Sequence[ChatCompletionMessageParam],
        response_model: type[ModelT],
    ) -> StructuredResult[ModelT]:
        """结构化输出：schema 注入提示词 + Pydantic 强约束解析，解析失败直接抛错。

        走提示词约定而非 response_format 参数——对各类 OpenAI 兼容端点最稳；
        schema 注入部分若 endpoint 原生支持 json_schema 再升级。
        """
        schema = json.dumps(response_model.model_json_schema(), ensure_ascii=False)
        instruction = (
            "只输出一个 JSON 对象，不要 markdown 代码块、不要解释文字。"
            f"它必须符合下面的 JSON Schema：\n{schema}"
        )
        result = await self.chat([*messages, {"role": "system", "content": instruction}])
        data = response_model.model_validate_json(_strip_json_fence(result.text))
        return StructuredResult(data=data, usage=result.usage)
