"""M1 第 1 周产出：最小 LLM client——AsyncOpenAI 流式补全 + usage 统计。

与 loop.py 的分工：本模块只封装"一次对话补全"的流式调用与计量；
多轮工具调用循环、错误回填是第 2–3 周进入 loop.py 的内容。

事件模型：stream_chat 产出 StreamEvent（TextDelta | StreamEnd），
保证恰好以一个 StreamEnd 结束——第 4 周 FastAPI SSE 只需把事件逐个转发。
"""

import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

DEFAULT_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
DEFAULT_MODEL = "glm-5.3-flash"


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
class StreamEnd:
    """流的终止事件；端点未回 usage 时为 None。"""

    usage: Usage | None = None


StreamEvent = TextDelta | StreamEnd


@dataclass(frozen=True, slots=True)
class StreamResult:
    """chat() 的一次性结果：完整回复 + 计量。"""

    text: str
    usage: Usage | None = None


@dataclass(frozen=True, slots=True)
class LLMConfig:
    api_key: str
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE_URL

    @classmethod
    def from_env(cls) -> "LLMConfig":
        api_key = os.environ.get("ZHIPU_API_KEY", "")
        if not api_key:
            raise RuntimeError("缺少 ZHIPU_API_KEY：请复制 .env.example 为 .env 并填入")
        return cls(
            api_key=api_key,
            base_url=os.environ.get("LLM_BASE_URL", DEFAULT_BASE_URL),
            model=os.environ.get("LLM_MODEL", DEFAULT_MODEL),
        )


class LLMClient:
    """对 AsyncOpenAI 的最小封装：流式补全 + usage 计量。"""

    def __init__(self, config: LLMConfig, client: AsyncOpenAI | None = None) -> None:
        self._config = config
        self._client = client or AsyncOpenAI(api_key=config.api_key, base_url=config.base_url)

    async def stream_chat(
        self, messages: Sequence[ChatCompletionMessageParam]
    ) -> AsyncIterator[StreamEvent]:
        """流式补全：逐段产出 TextDelta，最后恰好产出一个 StreamEnd。"""
        stream = await self._client.chat.completions.create(
            model=self._config.model,
            messages=list(messages),
            stream=True,
            # OpenAI 官方端点需要 include_usage 才在流末尾回传 usage；智谱兼容端点默认
            # 就带。若换用的兼容端点拒绝该参数，删掉本行即可。
            stream_options={"include_usage": True},
        )
        ended = False
        async with stream:
            async for chunk in stream:
                if chunk.choices and (delta := chunk.choices[0].delta.content):
                    yield TextDelta(delta)
                if chunk.usage is not None:
                    ended = True
                    yield StreamEnd(
                        usage=Usage(
                            prompt_tokens=chunk.usage.prompt_tokens,
                            completion_tokens=chunk.usage.completion_tokens,
                            total_tokens=chunk.usage.total_tokens,
                        )
                    )
                    break
        if not ended:
            yield StreamEnd(usage=None)

    async def chat(self, messages: Sequence[ChatCompletionMessageParam]) -> StreamResult:
        """收完整个流，返回完整文本与 usage（CLI / 不需要逐 token 的场景用）。"""
        parts: list[str] = []
        usage: Usage | None = None
        async for event in self.stream_chat(messages):
            match event:
                case TextDelta(text=text):
                    parts.append(text)
                case StreamEnd(usage=final_usage):
                    usage = final_usage
        return StreamResult(text="".join(parts), usage=usage)
