"""MCP → agent_core 工具桥：让手写 agent loop 经 MCP 协议消费 ERP 工具。

桥接两侧：
- 工具发现：Client(create_server(db)) 内存连接 list_tools，把 MCP 的
  inputSchema 动态转成 Pydantic 参数模型（agent loop 用它做入参校验）
- 工具调用：Tool.handler 每次调用开一个内存 MCP 会话 call_tool，
  返回 CallToolResult.data（fastmcp 已反序列化的 dict/list）

写操作（ADR-0005）：writes=True 时写工具进入工具面，并**必须**提供
approval_gate——审批门（agent_core.approval.guarded）包在 handler 外层，
先 review 后执行；writes=True 而 gate 缺失直接 raise。写工具的风险等级
在本模块标注（MCP schema 不承载风险语义）。

为什么每次调用都开会话：内存传输的会话开销可忽略，换来的是无连接生命周期
管理（不会在长连接断开后悄悄失效）；独立进程（stdio/HTTP）部署在 M3 第 3
周之后按需切换，桥接口不变。
"""

import asyncio
import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_core.approval import (
    RISK_BATCH_CONFIRM,
    RISK_SINGLE_CONFIRM,
    ApprovalGate,
    guarded,
)
from agent_core.tools import Tool
from erp_store.db import DEFAULT_DB
from fastmcp import Client
from fastmcp.client.client import CallToolResult
from pydantic import BaseModel, Field, create_model

from mcp_erp.server import create_server

_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
}

# 写工具的风险分级（ADR-0005：资金/单据单笔确认，低风险批量确认）
WRITE_TOOL_RISK: dict[str, str] = {
    "create_order": RISK_SINGLE_CONFIRM,
    "cancel_order": RISK_SINGLE_CONFIRM,
    "adjust_stock": RISK_BATCH_CONFIRM,
    "set_product_status": RISK_BATCH_CONFIRM,
}


def build_agent_tools(
    db_path: Path | str = DEFAULT_DB,
    *,
    writes: bool = False,
    approval_gate: ApprovalGate | None = None,
    token_factory: Callable[[], str] | None = None,
) -> list[Tool]:
    """同步入口（应用启动 / CLI main，不能在事件循环内调用）。"""
    return asyncio.run(build_agent_tools_async(
        db_path, writes=writes, approval_gate=approval_gate, token_factory=token_factory,
    ))


async def build_agent_tools_async(
    db_path: Path | str = DEFAULT_DB,
    *,
    writes: bool = False,
    approval_gate: ApprovalGate | None = None,
    token_factory: Callable[[], str] | None = None,
) -> list[Tool]:
    """发现 MCP 工具并转换为 agent_core Tool 列表（可在运行中的 loop 内调用）。

    db 文件不存在时报带指引的错误。writes=True 必须给 approval_gate
    （无 gate 不给写工具，ADR-0005 决策 4）。token_factory（T05，ADR-0008）
    提供时写工具在审批展示前生成稳定幂等键，供组合层持久化与恢复复用。
    """
    if writes and approval_gate is None:
        raise ValueError("writes=True 必须提供 approval_gate：写工具不过审批门就不该存在")
    db_path = Path(db_path)
    if not db_path.is_file():
        raise FileNotFoundError(
            f"ERP 数据库不存在：{db_path}（先运行 uv run --package erp-store "
            "python -m erp_store seed 生成）"
        )
    server = create_server(db_path, include_writes=writes)
    async with Client(server) as client:
        mcp_tools = await client.list_tools()
    tools = [
        _convert(t.name, t.description or "", t.input_schema, db_path, writes)
        for t in mcp_tools
    ]
    if writes:
        tools = [_with_risk_and_gate(t, approval_gate, token_factory) for t in tools]  # type: ignore[arg-type]
    return tools


def _with_risk_and_gate(
    tool: Tool, gate: ApprovalGate | None, token_factory: Callable[[], str] | None = None
) -> Tool:
    """给写工具标注风险等级并包审批门；只读工具原样返回。"""
    risk = WRITE_TOOL_RISK.get(tool.name)
    if risk is None:
        return tool
    marked = Tool(
        name=tool.name,
        description=tool.description,
        params_model=tool.params_model,
        handler=tool.handler,
        risk=risk,
        retry_safe=True,
        schema_version=tool.schema_version,
    )
    assert gate is not None  # build_agent_tools_async 已校验
    return guarded(marked, gate, token_factory)


def _schema_sha(schema: dict[str, Any]) -> str:
    """参数 JSON Schema 的 sha256（T05）：恢复时校验工具版本兼容性。"""
    canonical = json.dumps(schema, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _convert(
    name: str, description: str, schema: dict[str, Any], db_path: Path, writes: bool
) -> Tool:
    """把一个 MCP 工具转成 agent Tool；调用侧必须以同一 writes 口径建 server。

    （缺陷修复，M4 第 2 周 app-01 评测发现）：工具发现用 include_writes=writes，
    调用 handler 此前却固定 include_writes=False——写工具"能发现不能调用"
    （Unknown tool），AutoDeny 评测永远到不了执行段所以没暴露。
    """
    params_model = _params_model(name, schema)

    async def handler(args: BaseModel, _name: str = name) -> object:
        if _name in WRITE_TOOL_RISK and args.client_token is None:
            # 同次工具执行的超时/异常重试复用同一个参数对象与 token；
            # 接恢复层时 token 已由审批门前注入（T05），此处只兜底遗留路径。
            args.client_token = uuid4().hex
        server = create_server(db_path, include_writes=writes)
        async with Client(server) as client:
            result = await client.call_tool(_name, args.model_dump(mode="json", exclude_none=True))
        return _extract(result)

    return Tool(
        name=name,
        description=description,
        params_model=params_model,
        handler=handler,
        schema_version=_schema_sha(schema),
    )


def _params_model(tool_name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """inputSchema → Pydantic 模型：required 无默认值，可选参数给默认。"""
    fields: dict[str, Any] = {}
    required = set(schema.get("required", []))
    for prop_name, prop in schema.get("properties", {}).items():
        description = prop.get("description", "")
        annotation = _annotation_of(prop)
        if prop_name in required:
            fields[prop_name] = (annotation, Field(description=description))
        elif prop.get("default") is not None:
            # 工具有自己的默认值：不传时 exclude_none 会让服务端走默认
            fields[prop_name] = (
                annotation,
                Field(default=prop["default"], description=description),
            )
        else:
            fields[prop_name] = (
                annotation | None,
                Field(default=None, description=description),
            )
    model = create_model(f"{tool_name}_params", **fields)
    model.__doc__ = f"工具 {tool_name} 的入参（由 MCP inputSchema 生成）"
    return model


def _annotation_of(prop: dict[str, Any]) -> type:
    """单个 property 的类型：基本类型映射；anyOf [T, null] → Optional[T]。"""
    if "anyOf" in prop:
        non_null = [p for p in prop["anyOf"] if p.get("type") != "null"]
        return _TYPE_MAP.get(non_null[0].get("type", "string"), str) if non_null else str
    return _TYPE_MAP.get(prop.get("type", "string"), str)


async def execute_write_async(
    db_path: Path | str,
    tool: str,
    arguments: dict[str, Any],
    client_token: str,
) -> Any:
    """恢复路径专用执行入口（T06，ADR-0008）：**原 token** 直呼一次写工具。

    只供组合层恢复协调使用——不在工具面注册（不增加模型可见工具数量），
    不经过审批门（能进这里的调用必须已持有效批准，见恢复协调的校验）。
    业务错误沿用工具层错误契约（{"error": {...}}），由调用方按结果判定。
    """
    if tool not in WRITE_TOOL_RISK:
        raise ValueError(f"{tool} 不是写工具：恢复执行入口只接受写调用")
    server = create_server(Path(db_path), include_writes=True)
    async with Client(server) as client:
        result = await client.call_tool(
            tool, {**arguments, "client_token": client_token}
        )
    return _extract(result)


def _extract(result: CallToolResult) -> Any:
    """取回模型需要的数据：优先 fastmcp 反序列化的 .data，退回解析 JSON 文本。"""
    data = getattr(result, "data", None)
    if data is not None:
        if isinstance(data, BaseModel):
            return json.loads(data.model_dump_json())
        if isinstance(data, (dict, list)):
            return data
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text
    return None
