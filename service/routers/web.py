"""Read-only Web blackboard routes."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse

from ..db import connect
from ..report import render_report

router = APIRouter(prefix="/web", tags=["web"])
WEB_DIR = Path(__file__).resolve().parent.parent.parent / "web"


def fail(code: str, message: str) -> None:
    raise HTTPException(404, {"code": code, "message": message})


def parse_json(value: str | None, fallback):
    if value is None:
        return fallback
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


@router.get("/", include_in_schema=False)
async def index():
    return FileResponse(WEB_DIR / "index.html", media_type="text/html")


@router.get("/app.js", include_in_schema=False)
async def javascript():
    return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")


@router.get("/styles.css", include_in_schema=False)
async def stylesheet():
    return FileResponse(WEB_DIR / "styles.css", media_type="text/css")


@router.get("/projects")
async def projects(request: Request):
    conn = connect(request.app.state.cfg.data_dir)
    try:
        rows = conn.execute(
            "SELECT p.*, o.status AS current_status, o.finished_at AS published_at "
            "FROM projects p LEFT JOIN observations o ON o.id=p.current_observation_id "
            "ORDER BY p.created_at DESC LIMIT 200"
        ).fetchall()
        return {"ok": True, "projects": [dict(row) for row in rows]}
    finally:
        conn.close()


def load_project(conn, session_id: str, observation_id: int | None = None):
    project = conn.execute("SELECT * FROM projects WHERE session_id=?", (session_id,)).fetchone()
    if project is None:
        fail("session_not_found", "project not found")
    obs_id = observation_id if observation_id is not None else project["current_observation_id"]
    observation = None
    state = None
    map_text = None
    if obs_id is not None:
        observation = conn.execute(
            "SELECT * FROM observations WHERE id=? AND session_id=? AND status='published'",
            (obs_id, session_id),
        ).fetchone()
        if observation is None:
            fail("observation_not_found", "published observation not found in this session")
        state = parse_json(observation["state_json"], None)
        map_text = observation["map_text"]
    latest = conn.execute(
        "SELECT id,trigger,status,start_record_id,end_record_id,error,started_at,finished_at "
        "FROM observations WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)
    ).fetchone()
    versions = conn.execute(
        "SELECT id,trigger,status,start_record_id,end_record_id,started_at,finished_at,state_json "
        "FROM observations WHERE session_id=? AND status='published' ORDER BY id DESC LIMIT 100", (session_id,)
    ).fetchall()
    return dict(project), (dict(observation) if observation else None), state, map_text, (dict(latest) if latest else None), [
        {**{k: row[k] for k in row.keys() if k != "state_json"}, "revision": (parse_json(row["state_json"], {}) or {}).get("revision")}
        for row in versions
    ]


@router.get("/project/{session_id}")
async def project(session_id: str, request: Request, observation_id: int | None = Query(None)):
    conn = connect(request.app.state.cfg.data_dir)
    try:
        p, obs, state, map_text, latest, versions = load_project(conn, session_id, observation_id)
        if obs:
            obs.pop("state_json", None); obs.pop("map_text", None); obs.pop("tool_logs_json", None)
        return {"ok": True, "project": p, "observation": obs, "state": state,
                "map_text": map_text, "latest_run": latest, "versions": versions}
    finally:
        conn.close()


@router.get("/record/{session_id}/{record_id}")
async def record(session_id: str, record_id: int, request: Request):
    conn = connect(request.app.state.cfg.data_dir)
    try:
        row = conn.execute("SELECT * FROM tool_records WHERE session_id=? AND id=?", (session_id, record_id)).fetchone()
        if row is None:
            fail("record_not_found", "record not found in this session")
        value = dict(row)
        for key in ("tool_input_json", "tool_response_json", "metadata_json"):
            value[key.removesuffix("_json")] = parse_json(value.pop(key), None)
        return {"ok": True, "record": value}
    finally:
        conn.close()


@router.get("/observation/{session_id}/{observation_id}/logs")
async def observer_logs(session_id: str, observation_id: int, request: Request):
    conn = connect(request.app.state.cfg.data_dir)
    try:
        row = conn.execute("SELECT tool_logs_json FROM observations WHERE session_id=? AND id=?", (session_id, observation_id)).fetchone()
        if row is None:
            fail("observation_not_found", "observation not found in this session")
        return {"ok": True, "logs": parse_json(row["tool_logs_json"], [])}
    finally:
        conn.close()


@router.get("/report/{session_id}", response_class=PlainTextResponse)
async def report(session_id: str, request: Request, observation_id: int | None = Query(None)):
    conn = connect(request.app.state.cfg.data_dir)
    try:
        project, observation, state, _, _, _ = load_project(conn, session_id, observation_id)
        return PlainTextResponse(render_report(project, observation, state), media_type="text/markdown; charset=utf-8")
    finally:
        conn.close()
