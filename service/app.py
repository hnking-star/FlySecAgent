"""FastAPI 应用工厂 + 鉴权中间件 + 身份注入 + 请求日志。

本文件也负责在 lifespan 里启动/停止 Scheduler + PiRunner。
"""

from __future__ import annotations

import json
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from . import observation
from .auth import TokenRegistry, token_fingerprint
from .bootstrap import ensure_data_dir, ensure_service_token
from .config import load_config
from .db import connect, init_db
from .errors import (
    http_exception_handler,
    unhandled_exception_handler,
    validation_exception_handler,
)
from .pi_runner import PiRunner
from .request_log import log_request, setup_logging
from .scheduler import Scheduler


VERSION = "0.1.0"

# body 顶层不允许出现的身份字段（防越权）。
_FORBIDDEN_IDENTITY_KEYS = {"session_id", "project_id", "sessionId", "projectId"}

# 需要服务级 token 的路由前缀。
_SERVICE_TOKEN_PREFIXES = ("/hook/", "/control/", "/web/")

# 需要会话级 token 的路由前缀。
_SESSION_TOKEN_PREFIXES = ("/observer/",)

# 不鉴权的路由。
_PUBLIC_PATHS = {"/health"}


def create_app(cfg=None, *, start_background: bool | None = None) -> FastAPI:
    cfg = cfg or load_config()
    ensure_data_dir(cfg.data_dir)
    service_token = ensure_service_token(cfg.data_dir)
    registry = TokenRegistry()
    setup_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Scheduler + PiRunner 延迟到有真实请求前初始化；测试时也能用。
        async def pi_ready(sid: str, message: dict):
            observer_sid = message.get("observer_session_id")
            if observer_sid:
                conn = connect(cfg.data_dir)
                try:
                    conn.execute("UPDATE projects SET observer_session_id=? WHERE session_id=?", (observer_sid, sid))
                finally:
                    conn.close()
        pi_runner = PiRunner(
            api_base=f"http://127.0.0.1:{cfg.port}" if cfg.port else "http://127.0.0.1:0",
            token_registry=registry,
            on_run_done=_make_run_done_handler(app),
            on_ready=pi_ready,
        )
        async def dispatch(sid: str, trigger: str):
            conn = connect(cfg.data_dir)
            try:
                row = conn.execute("SELECT observer_paused, observation_enabled FROM projects WHERE session_id=?", (sid,)).fetchone()
                if not row or row["observer_paused"] or (not row["observation_enabled"] and trigger != "observation_close"):
                    await app.state.scheduler.pi_run_failed(sid)
                    return
                obs_id = observation.start_observation(conn, sid, trigger)
                app.state.active_observations[sid] = obs_id
                app.state.submit_cache.clear_session(sid)
            finally:
                conn.close()
            await pi_runner.dispatch(sid, trigger)
        scheduler = Scheduler(dispatcher=dispatch)
        app.state.pi_runner = pi_runner
        app.state.scheduler = scheduler
        if start_background:
            await scheduler.start()
        try:
            yield
        finally:
            await scheduler.stop()
            await pi_runner.stop_all()

    app = FastAPI(title="FlySecAgent", version=VERSION, lifespan=lifespan)
    app.state.cfg = cfg
    app.state.service_token = service_token
    app.state.token_registry = registry
    app.state.active_observations = {}

    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)

    @app.middleware("http")
    async def _auth_and_log(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        return await _handle(request, call_next, service_token, registry)

    _register_health(app)
    _register_routers(app)
    _register_stubs(app)
    return app


def _make_run_done_handler(app: FastAPI):
    async def handler(session_id: str, msg: dict) -> None:
        scheduler: Scheduler = app.state.scheduler
        # Confirm the database receipt, not a subprocess self-report.
        conn = connect(app.state.cfg.data_dir)
        try:
            obs_id = app.state.active_observations.get(session_id)
            row = conn.execute("SELECT status FROM observations WHERE session_id=? AND id=?", (session_id, obs_id)).fetchone()
            success = bool(row and row["status"] in {"published", "unchanged"})
            if not success and row and row["status"] == "running":
                observation.mark_failed(conn, session_id, obs_id, "Observer process ended without publication")
        finally:
            conn.close()
        if success:
            await scheduler.pi_run_done(session_id)
        else:
            await scheduler.pi_run_failed(session_id)

    return handler


def _register_health(app: FastAPI) -> None:
    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {"ok": True, "version": VERSION}


def _register_routers(app: FastAPI) -> None:
    """挂真正的业务 router。"""
    from .routers import control, hook, observer

    app.include_router(hook.router)
    app.include_router(observer.router)
    app.include_router(control.router)
    app.state.submit_cache = observer.SubmitCache()


def _register_stubs(app: FastAPI) -> None:
    """control 的 stub 路由；Task 10 替换。"""

    @app.post("/control/health-stub")
    async def control_stub() -> dict[str, Any]:
        return {"ok": True, "stub": "control"}


async def _handle(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
    service_token: str,
    registry: TokenRegistry,
) -> Response:
    request_id = str(uuid.uuid4())
    request.state.request_id = request_id
    request.state.session_id = None
    request.state.token_fp = None
    started = time.perf_counter()
    status = 500

    try:
        guard = await _authorize(request, service_token, registry)
        if guard is not None:
            status = guard.status_code
            return guard

        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        _log(request, request_id, status, time.perf_counter() - started)


async def _authorize(
    request: Request, service_token: str, registry: TokenRegistry
) -> JSONResponse | None:
    """返回 None 放行，返回 JSONResponse 直接拒绝。"""
    client_host = request.client.host if request.client else None
    if client_host and client_host not in {"127.0.0.1", "::1"}:
        return _reject(403, "non_loopback", "only loopback requests are allowed")

    path = request.url.path
    if path in _PUBLIC_PATHS:
        return None

    if any(path.startswith(prefix) for prefix in _SERVICE_TOKEN_PREFIXES):
        token = request.headers.get("x-flysec-token", "")
        if token != service_token or not token:
            return _reject(401, "unauthorized", "invalid service token")
        request.state.token_fp = token_fingerprint(token)
        return None

    if any(path.startswith(prefix) for prefix in _SESSION_TOKEN_PREFIXES):
        token = request.headers.get("x-flysec-token", "")
        if not token:
            return _reject(401, "unauthorized", "missing session token")
        session_id = registry.resolve(token)
        if session_id is None:
            return _reject(401, "unauthorized", "invalid session token")
        request.state.session_id = session_id
        request.state.token_fp = token_fingerprint(token)
        return await _check_body_identity(request)

    return None


async def _check_body_identity(request: Request) -> JSONResponse | None:
    """pre-parse JSON body；带身份字段 → 403。非 JSON body 一律 400。

    读过的 body 会被缓存到 request._body，下游 await request.body() 不会卡死。
    """
    if request.method.upper() in {"GET", "HEAD", "OPTIONS", "DELETE"}:
        return None

    raw = await request.body()
    if raw == b"":
        return None  # 允许空 body；业务层再决定

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return _reject(400, "schema_invalid", "body is not valid JSON")

    if isinstance(payload, dict):
        for key in _FORBIDDEN_IDENTITY_KEYS:
            if key in payload:
                return _reject(
                    403,
                    "identity_field_not_allowed",
                    f"body must not contain {key!r}",
                )
    return None


def _reject(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "code": code, "message": message}, status_code=status
    )


def _log(request: Request, request_id: str, status: int, duration: float) -> None:
    fields: dict[str, Any] = {
        "request_id": request_id,
        "route": f"{request.method} {request.url.path}",
        "status": status,
        "duration_ms": round(duration * 1000, 2),
    }
    session_id = getattr(request.state, "session_id", None)
    if session_id:
        fields["session_id"] = session_id
    token_fp = getattr(request.state, "token_fp", None)
    if token_fp:
        fields["token_fp"] = token_fp
    log_request(fields)


def startup_recover(cfg) -> int:
    """首启建表 + 对账 pending。返回恢复的会话数。"""
    init_db(cfg.data_dir)
    recovered = 0
    conn = connect(cfg.data_dir)
    try:
        rows = conn.execute(
            "SELECT session_id FROM projects WHERE pending_window_end IS NOT NULL"
        ).fetchall()
        for row in rows:
            observation.recover_pending(conn, row["session_id"])
            recovered += 1
    finally:
        conn.close()
    return recovered
