"""统一错误体与 FastAPI 异常处理器。

错误体格式：
    {"ok": false, "code": "...", "message": "...", "path": "..."}

- HTTPException(status_code, detail={"code":..., "message":..., "path":...}) 自动格式化
- RequestValidationError → 400 schema_invalid + 字段路径
- 其他未捕获异常 → 500 internal_error（traceback 落日志，响应只给一句话）
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


_INTERNAL_LOGGER = logging.getLogger("flysec.error")


def error_body(code: str, message: str, path: str | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"ok": False, "code": code, "message": message}
    if path:
        body["path"] = path
    return body


async def http_exception_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        body = error_body(
            code=str(detail.get("code")),
            message=str(detail.get("message", "")),
            path=detail.get("path"),
        )
    else:
        body = error_body(code=_default_code(exc.status_code), message=str(detail))
    return JSONResponse(body, status_code=exc.status_code, headers=exc.headers)


async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    first = exc.errors()[0] if exc.errors() else {}
    path = "/" + "/".join(str(x) for x in first.get("loc", []) if x != "body")
    message = first.get("msg", "schema invalid")
    body = error_body("schema_invalid", message, path=path or "/")
    if _request.url.path == "/memory/commit":
        body["errors"] = [{"code": "schema_invalid", "path": path, "message": message}]
        sid = getattr(_request.state, "session_id", None)
        if sid:
            from .db import connect
            import json
            conn = connect(_request.app.state.cfg.data_dir)
            try:
                row = conn.execute("SELECT id FROM observations WHERE session_id=? AND status='running' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                if row:
                    entry = {"op": "curator_commit", "arguments": exc.body, "ok": False, "errors": body["errors"], "response": body}
                    conn.execute("UPDATE observations SET tool_logs_json=json_insert(tool_logs_json,'$[#]',json(?)) WHERE id=?", (json.dumps(entry, ensure_ascii=False), row["id"]))
            finally:
                conn.close()
    return JSONResponse(body, status_code=400)



async def unhandled_exception_handler(
    _request: Request, exc: Exception
) -> JSONResponse:
    _INTERNAL_LOGGER.exception("unhandled: %s", exc)
    return JSONResponse(
        error_body("internal_error", "internal server error"),
        status_code=500,
    )


def _default_code(status: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
    }.get(status, "error")
