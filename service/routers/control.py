"""/control/* 路由。

Task 6 做：agent-turn/begin, agent-turn/stop
Task 10 补：curator.pause, curator.resume, observation.close

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
        "SELECT observation_enabled, observer_paused, agent_turn_active,final_summary_requested FROM projects WHERE session_id = ?",
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
            "UPDATE projects SET agent_turn_active = 1,agent_activity_known=1 WHERE session_id = ?",
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
            "UPDATE projects SET agent_turn_active = 0,agent_activity_known=1 WHERE session_id = ?",
            (payload.session_id,),
        )
    finally:
        conn.close()

    scheduler = request.app.state.scheduler
    if row["observation_enabled"] and not row["observer_paused"] and row["agent_turn_active"]:
        await scheduler.agent_turn_stop(payload.session_id)
    return {"ok": True}


@router.post('/curator.pause')
@router.post('/observer.pause', include_in_schema=False)
async def curator_pause(payload: _SessionBody, request: Request):
    conn = _conn(request)
    try:
        _require_project(conn, payload.session_id)
        conn.execute('UPDATE projects SET observer_paused=1 WHERE session_id=?', (payload.session_id,))
    finally: conn.close()
    await request.app.state.scheduler.curator_paused(payload.session_id)
    await request.app.state.pi_runner.stop_session(payload.session_id)
    conn = _conn(request)
    try:
        row = conn.execute("SELECT id FROM observations WHERE session_id=? AND status='running'", (payload.session_id,)).fetchone()
        if row:
            from ..observation import mark_failed
            mark_failed(conn, payload.session_id, row['id'], 'Memory Curator stopped by operator; unpublished window retained')
    finally: conn.close()
    await request.app.state.scheduler.pi_run_failed(payload.session_id)
    return {'ok': True}


@router.post('/curator.resume')
@router.post('/observer.resume', include_in_schema=False)
async def curator_resume(payload: _SessionBody, request: Request):
    conn = _conn(request)
    try:
        row = _require_project(conn, payload.session_id)
        if not row['observation_enabled'] and not row['final_summary_requested']:
            raise HTTPException(409, {'code': 'observation_disabled', 'message': 'Re-enable observation explicitly before resuming'})
        conn.execute('UPDATE projects SET observer_paused=0 WHERE session_id=?', (payload.session_id,))
    finally: conn.close()
    await request.app.state.scheduler.curator_resumed(payload.session_id, agent_turn_active=bool(row['agent_turn_active']))
    if row['final_summary_requested']:
        state = request.app.state.scheduler.snapshot(payload.session_id)
        # Repeated resume clicks must not queue a second closing summary.
        if not state or not state.running:
            await request.app.state.scheduler.request_summary(payload.session_id, 'observation_close')
    return {'ok': True, 'final_summary_only': not bool(row['observation_enabled'])}


@router.post('/observation.close')
async def observation_close(payload: _SessionBody, request: Request):
    conn = _conn(request)
    try:
        row = _require_project(conn, payload.session_id)
        if not row['observation_enabled']:
            return {'ok': True, 'final_summary_pending': bool(row['final_summary_requested'])}
        conn.execute('UPDATE projects SET observation_enabled=0,agent_turn_active=0,agent_activity_known=1,final_summary_requested=1 WHERE session_id=?', (payload.session_id,))
    finally: conn.close()
    # Closing never overrides an operator pause. A pending close is retained in
    # scheduler state, and can run only after an explicit resume/re-enable.
    if row['observer_paused']:
        await request.app.state.scheduler.curator_paused(payload.session_id)
    await request.app.state.scheduler.observation_closed(payload.session_id)
    return {'ok': True, 'final_summary_pending': bool(row['observer_paused'])}


@router.post('/observation.open')
async def observation_open(payload: _SessionBody, request: Request):
    conn = _conn(request)
    try:
        row = _require_project(conn, payload.session_id)
        conn.execute('UPDATE projects SET observation_enabled=1 WHERE session_id=?', (payload.session_id,))
    finally: conn.close()
    await request.app.state.scheduler.observation_opened(payload.session_id, bool(row['observer_paused']))
    if row['final_summary_requested'] and not row['observer_paused']:
        await request.app.state.scheduler.request_summary(payload.session_id, 'observation_close')
    return {'ok': True, 'curator_paused': bool(row['observer_paused']), 'observer_paused': bool(row['observer_paused'])}
