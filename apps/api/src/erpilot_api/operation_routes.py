"""Read immutable, session-bound business approvals with current validity."""

from fastapi import FastAPI

from erpilot_api.service import SessionError


def register_operation_routes(app: FastAPI, operations):
    @app.get("/api/sessions/{session_id}/operations/{operation_id}")
    def get_operation(session_id: str, operation_id: str):
        if operations is None:
            raise SessionError(
                503,
                "business_approvals_disabled",
                "当前运行环境未启用业务审批",
                "请使用配置了 ERP 数据源的业务审批运行环境",
            )
        return operations.get(session_id, operation_id)
