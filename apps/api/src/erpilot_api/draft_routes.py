"""Session-scoped order draft HTTP contracts; submission remains an approved tool."""

from erp_store.mutations import MutationError
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from erpilot_api.service import SessionError


class DraftItemRequest(BaseModel):
    sku: str = Field(min_length=1, max_length=32)
    quantity: int = Field(strict=True, gt=0)


class DraftRequest(BaseModel):
    customer: str = Field(min_length=1, max_length=64)
    items: list[DraftItemRequest] = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=255)


class ReviseDraftRequest(DraftRequest):
    expected_revision: int = Field(strict=True, gt=0)


def register_draft_routes(app: FastAPI, session_erp_tools):
    @app.exception_handler(MutationError)
    async def draft_error(request: Request, exc: MutationError):
        status = (
            404
            if exc.code == "not_found"
            else 410
            if exc.code in {"draft_expired", "operation_expired"}
            else 409
            if exc.code in {
                "draft_changed",
                "quote_changed",
                "stock_changed",
                "draft_submitted",
                "operation_changed",
            }
            else 422
        )
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "hint": exc.hint,
                }
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if "/order-drafts" in request.url.path:
            return JSONResponse(
                status_code=422,
                content={
                    "error": {
                        "code": "invalid_argument",
                        "message": "草稿参数无效",
                        "hint": "请检查客户、商品、数量和版本号",
                    }
                },
            )
        from fastapi.exception_handlers import request_validation_exception_handler

        return await request_validation_exception_handler(request, exc)

    def drafts_service():
        if session_erp_tools is None:
            raise SessionError(
                503,
                "drafts_disabled",
                "当前运行环境未启用订单草稿",
                "请使用配置了 ERP 数据源的草稿运行环境",
            )
        return session_erp_tools.drafts

    @app.get("/api/sessions/{session_id}/order-drafts")
    def list_drafts(session_id: str):
        return {"items": drafts_service().list_latest(session_id)}

    @app.post("/api/sessions/{session_id}/order-drafts", status_code=201)
    def prepare_draft(session_id: str, req: DraftRequest):
        return drafts_service().prepare(
            session_id,
            req.customer,
            [(item.sku, item.quantity) for item in req.items],
            note=req.note,
        )

    @app.get("/api/sessions/{session_id}/order-drafts/{draft_id}")
    def get_draft(session_id: str, draft_id: str, revision: int | None = Query(default=None, gt=0)):
        return drafts_service().get(session_id, draft_id, revision=revision)

    @app.patch("/api/sessions/{session_id}/order-drafts/{draft_id}")
    def revise_draft(session_id: str, draft_id: str, req: ReviseDraftRequest):
        return drafts_service().revise(
            session_id,
            draft_id,
            req.expected_revision,
            req.customer,
            [(item.sku, item.quantity) for item in req.items],
            note=req.note,
        )
