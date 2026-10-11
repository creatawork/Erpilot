"""Cache discovery metadata while binding execution to the trusted chat session."""

from pathlib import Path

from erp_store.db import DEFAULT_DB, make_engine
from erp_store.drafts import ErpDrafts
from fastmcp import Client

from mcp_erp.bridge import ErpMutationReconciler, _convert, _with_risk_and_gate
from mcp_erp.server import create_server


class SessionErpTools:
    @classmethod
    async def create(cls, db_path=DEFAULT_DB, *, writes=False, gate=None, policy_resolver=None):
        if writes and gate is None:
            raise ValueError("writes=True 必须提供 approval gate")
        path = Path(db_path)
        if not path.is_file():
            raise FileNotFoundError(f"ERP 数据库不存在：{path}")
        server = create_server(
            path, include_writes=writes, session_id="discovery", policy_resolver=policy_resolver
        )
        async with Client(server) as client:
            metadata = await client.list_tools()
        return cls(path, writes, gate, policy_resolver, metadata)

    def __init__(self, path, writes, gate, policy_resolver, metadata):
        self.db_path, self.writes, self.gate = path, writes, gate
        self.policy_resolver, self._metadata = policy_resolver, metadata
        self.drafts = ErpDrafts(make_engine(path))
        self.tools = self.for_session("discovery")

    def for_session(self, session_id):
        tools = [
            _convert(
                t.name,
                t.description or "",
                t.input_schema,
                self.db_path,
                self.writes,
                self.policy_resolver,
                session_id,
            )
            for t in self._metadata
        ]
        return [_with_risk_and_gate(t, self.gate) for t in tools] if self.writes else tools

    def reconciler(self, session_id):
        return ErpMutationReconciler(self.db_path, session_id=session_id)
