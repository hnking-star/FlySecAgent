"""/hook/* 路由：Codex Hook 把 ensure / ingest / map.pending / map.ack 挂上来。

关键规则（见 docs/07-http-api.md §2）：
- 身份键 session_id 来自 body/query，Python 以此为 WHERE 过滤。
- project.ensure 幂等：已存在时原字段不覆盖。
- record.ingest 入库前原样 json.dumps；call_key 存在时返回原 record_id。
- map.pending 只查不改；map.ack 独立做 last_feedback_observation_id 的 UPDATE。
- revision 从 observations.state_json 顶层字段读（Task 5 落）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from ..db import connect, transaction
from ..blackboard import prepare_state
from ..feedback import render_digest
from ..schemas import (
    MapAckInput,
    MapAckOutput,
    MapPendingOutput,
    ProjectEnsureInput,
    ProjectEnsureOutput,
    RecordIngestInput,
    RecordIngestOutput,
)

router = APIRouter(prefix="/hook", tags=["hook"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _conn(request: Request) -> sqlite3.Connection:
    return connect(request.app.state.cfg.data_dir)


def _raise(status: int, code: str, message: str) -> None:
    raise HTTPException(status_code=status, detail={"code": code, "message": message})


def _parse_revision(state_json: str | None) -> str | None:
    if not state_json:
        return None
    try:
        return json.loads(state_json).get("revision")
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# project.ensure
# ---------------------------------------------------------------------------


@router.post("/project.ensure", response_model=ProjectEnsureOutput)
async def project_ensure(payload: ProjectEnsureInput, request: Request):
    conn = _conn(request)
    try:
        existing = conn.execute(
            "SELECT 1 FROM projects WHERE session_id = ?",
            (payload.session_id,),
        ).fetchone()
        if existing is not None:
            return ProjectEnsureOutput(created=False)

        if (
            payload.hint is None
            or not payload.hint.target
            or not payload.hint.objective
        ):
            _raise(
                400,
                "missing_hint",
                "target and objective are required when creating a new project",
            )

        with transaction(conn):
            conn.execute(
                "INSERT INTO projects (session_id, target, objective, created_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    payload.session_id,
                    payload.hint.target,
                    payload.hint.objective,
                    _now_iso(),
                ),
            )
        return ProjectEnsureOutput(created=True)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# record.ingest
# ---------------------------------------------------------------------------


@router.post("/record.ingest", response_model=RecordIngestOutput)
async def record_ingest(payload: RecordIngestInput, request: Request):
    conn = _conn(request)
    try:
        proj = conn.execute(
            "SELECT observation_enabled FROM projects WHERE session_id = ?",
            (payload.session_id,),
        ).fetchone()
        if proj is None or int(proj["observation_enabled"]) == 0:
            _raise(
                409,
                "session_not_ready",
                "session is not bound or observation is disabled",
            )

        if payload.call_key is not None:
            dup = conn.execute(
                "SELECT id, tool_name, tool_input_json, tool_response_json FROM tool_records "
                "WHERE session_id = ? AND call_key = ?",
                (payload.session_id, payload.call_key),
            ).fetchone()
            if dup is not None:
                if (dup["tool_name"] != payload.tool_name or json.loads(dup["tool_input_json"]) != payload.tool_input or json.loads(dup["tool_response_json"]) != payload.tool_response):
                    _raise(409, "call_key_conflict", "same call key has different tool evidence")
                return RecordIngestOutput(record_id=int(dup["id"]))

        tool_input = json.dumps(payload.tool_input, ensure_ascii=False)
        tool_response = json.dumps(payload.tool_response, ensure_ascii=False)
        metadata = json.dumps(payload.metadata, ensure_ascii=False)

        with transaction(conn):
            cur = conn.execute(
                "INSERT INTO tool_records "
                "(session_id, call_key, tool_name, tool_input_json, "
                " tool_response_json, metadata_json, received_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    payload.session_id,
                    payload.call_key,
                    payload.tool_name,
                    tool_input,
                    tool_response,
                    metadata,
                    _now_iso(),
                ),
            )
        return RecordIngestOutput(record_id=int(cur.lastrowid))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# map.pending
# ---------------------------------------------------------------------------


@router.get("/map.pending", response_model=MapPendingOutput)
async def map_pending(session_id: str, request: Request):
    conn = _conn(request)
    try:
        proj = conn.execute(
            "SELECT observation_enabled, current_observation_id, "
            "       last_feedback_observation_id "
            "FROM projects WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if proj is None:
            _raise(404, "session_not_found", "no project for this session")

        if int(proj["observation_enabled"]) == 0:
            return MapPendingOutput()

        current = proj["current_observation_id"]
        if current is None:
            return MapPendingOutput()

        last = int(proj["last_feedback_observation_id"] or 0)
        if last >= int(current):
            return MapPendingOutput()

        obs = conn.execute(
            "SELECT state_json, map_text, end_record_id FROM observations WHERE id = ? AND session_id = ? AND status = 'published'",
            (int(current), session_id),
        ).fetchone()
        if obs is None or obs["map_text"] is None:
            return MapPendingOutput()

        revision = _parse_revision(obs["state_json"])
        if not revision or obs["map_text"] == "<observer-map/>":
            return MapPendingOutput()
        state = json.loads(obs["state_json"])
        if state.get("schema_version", 1) == 1:
            target = conn.execute("SELECT target,objective FROM projects WHERE session_id=?", (session_id,)).fetchone()
            digest = render_digest(prepare_state(state, target["target"]), obs["end_record_id"], target["objective"])
        else:
            digest = obs["map_text"]
        return MapPendingOutput(revision=revision, map_text=digest)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# map.ack
# ---------------------------------------------------------------------------


@router.post("/map.ack", response_model=MapAckOutput)
async def map_ack(payload: MapAckInput, request: Request):
    conn = _conn(request)
    try:
        proj = conn.execute(
            "SELECT current_observation_id FROM projects WHERE session_id = ?",
            (payload.session_id,),
        ).fetchone()
        if proj is None:
            _raise(404, "session_not_found", "no project for this session")

        rows = conn.execute(
            "SELECT id, state_json FROM observations "
            "WHERE session_id = ? AND status = 'published' "
            "ORDER BY id DESC",
            (payload.session_id,),
        ).fetchall()
        target_id: int | None = None
        for row in rows:
            if _parse_revision(row["state_json"]) == payload.revision:
                target_id = int(row["id"])
                break

        if target_id is None:
            _raise(409, "revision_outdated", "revision not found for this session")

        current = int(proj["current_observation_id"] or 0)
        if target_id < current:
            _raise(
                409,
                "revision_outdated",
                "a newer revision has already been published",
            )

        with transaction(conn):
            conn.execute(
                "UPDATE projects SET last_feedback_observation_id = ? "
                "WHERE session_id = ?",
                (target_id, payload.session_id),
            )
        return MapAckOutput()
    finally:
        conn.close()
