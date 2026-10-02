"""/control/* 路由。

Task 6 做：agent-turn/begin, agent-turn/stop
Task 10 补：observer.pause, observer.resume, observation.close

全部走服务级 token（Task 3 中间件已校验）。
"""

from __future__ import annotations

import sqlite3
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..db import connect
from ..schemas import NonEmptyStr
from pydantic import BaseModel

router = APIRouter(prefix="/control", tags=["control"])


class _SessionBody(BaseModel):
    session_id: NonEmptyStr


def _conn(request: Request) -> sqlite3.Connection:
    return connect(request.app.state.cfg.data_dir)


def _require_project(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT observation_enabled, observer_paused, agent_turn_active FROM projects WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "session_not_found", "message": "no project for this session"},
        )
    return row


@router.post("/agent-turn/begin")
async def agent_turn_begin(payload: _SessionBody, request: Request) -> dict[str, Any]:
    conn = _conn(request)
    try:
        row = _require_project(conn, payload.session_id)
        if int(row["observation_enabled"]) == 0:
            raise HTTPException(
                status_code=409,
                detail={"code": "observation_disabled",
                        "message": "observation is disabled for this session"},
            )
        conn.execute(
            "UPDATE projects SET agent_turn_active = 1 WHERE session_id = ?",
            (payload.session_id,),
        )
    finally:
        conn.close()

    scheduler = request.app.state.scheduler
    pi_runner = request.app.state.pi_runner
    if not row["observer_paused"]:
        await scheduler.agent_turn_begin(payload.session_id)
    return {"ok": True}


@router.post("/agent-turn/stop")
async def agent_turn_stop(payload: _SessionBody, request: Request) -> dict[str, Any]:
    conn = _conn(request)
    try:
        row = _require_project(conn, payload.session_id)
        conn.execute(
            "UPDATE projects SET agent_turn_active = 0 WHERE session_id = ?",
            (payload.session_id,),
        )
    finally:
        conn.close()

    scheduler = request.app.state.scheduler
    if row["observation_enabled"] and not row["observer_paused"] and row["agent_turn_active"]:
        await scheduler.agent_turn_stop(payload.session_id)
    return {"ok": True}
