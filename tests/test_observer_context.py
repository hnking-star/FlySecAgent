"""/observer/context 五种 mode 的端到端测试。"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from service import db


SESSION = "sess-ctx"


def _hdr(app, session_id: str = SESSION) -> dict[str, str]:
    token = app.state.token_registry.issue(session_id)
    return {"X-FlySec-Token": token}


def _conn(cfg):
    return db.connect(cfg.data_dir)


def _ensure_project(cfg, session_id: str = SESSION) -> None:
    conn = _conn(cfg)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO projects (session_id, target, objective, created_at) "
                "VALUES (?, 'example.com', 'test', '2026-10-02T00:00:00Z')",
                (session_id,),
            )
    finally:
        conn.close()


def _insert_record(cfg, session_id: str, tool_name: str = "bash",
                   input_text: str = "{}", response_text: str = "{}") -> int:
    conn = _conn(cfg)
    try:
        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO tool_records "
                "(session_id, tool_name, tool_input_json, tool_response_json, "
                " metadata_json, received_at) VALUES (?, ?, ?, ?, '{}', ?)",
                (session_id, tool_name, input_text, response_text,
                 datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")),
            )
        return int(cur.lastrowid)
    finally:
        conn.close()


def _publish_state(cfg, session_id: str, state: dict,
                   map_text: str = "<observer-map/>") -> int:
    conn = _conn(cfg)
    try:
        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO observations "
                "(session_id, trigger, status, start_record_id, end_record_id, "
                " state_json, map_text, tool_logs_json, started_at, finished_at) "
                "VALUES (?, 'timer_5min', 'published', 0, 0, ?, ?, '[]', ?, ?)",
                (session_id, json.dumps(state), map_text,
                 "2026-10-02T00:00:00Z", "2026-10-02T00:00:00Z"),
            )
            obs_id = int(cur.lastrowid)
            conn.execute(
                "UPDATE projects SET current_observation_id = ?, processed_record_id = ? "
                "WHERE session_id = ?",
                (obs_id, 0, session_id),
            )
        return obs_id
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# summary
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_summary_empty_state(client):
    c, app = client
    _ensure_project(app.state.cfg)
    r = await c.post("/observer/context", headers=_hdr(app), json={"mode": "summary"})
    assert r.status_code == 200
    body = r.json()
    assert body["blackboard"]["revision"] is None
    assert body["blackboard"]["node_count"] == 0
    assert body["window"]["record_count"] == 0
    assert body["records_overview"]["by_tool"] == {}
    assert body["assessments"] == []


@pytest.mark.asyncio
async def test_summary_counts_window_records(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_record(app.state.cfg, SESSION, "bash")
    _insert_record(app.state.cfg, SESSION, "bash")
    _insert_record(app.state.cfg, SESSION, "http_request")

    # 固定一个 pending_window_end=3 让窗口有记录
    conn = _conn(app.state.cfg)
    try:
        conn.execute(
            "UPDATE projects SET pending_window_end = 3 WHERE session_id = ?",
            (SESSION,),
        )
    finally:
        conn.close()

    r = await c.post("/observer/context", headers=_hdr(app), json={"mode": "summary"})
    body = r.json()
    assert body["window"]["record_count"] == 3
    assert body["records_overview"]["by_tool"] == {"bash": 2, "http_request": 1}


@pytest.mark.asyncio
async def test_summary_shows_revision(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _publish_state(app.state.cfg, SESSION, {
        "schema_version": 1, "revision": "rev-A", "assessments": [], "retired": [],
        "apis": [], "guidance": {"hypothesis": None, "lock": None,
                                 "angleIds": [], "confirmedIds": [], "tension": []},
    })
    r = await c.post("/observer/context", headers=_hdr(app), json={"mode": "summary"})
    assert r.json()["blackboard"]["revision"] == "rev-A"


@pytest.mark.asyncio
async def test_summary_truncates_attempts_to_three(client):
    c, app = client
    _ensure_project(app.state.cfg)
    attempts = [
        {"id": f"a{i}", "action": "x", "result": "r", "evidenceRefs": ["record:1"]}
        for i in range(5)
    ]
    _publish_state(app.state.cfg, SESSION, {
        "schema_version": 1, "revision": "rev-1",
        "assessments": [{
            "id": "a-many", "subject": "s", "status": "tried-hit", "role": "direction",
            "api": None, "conclusion": "c", "basis": "b", "uncertainty": None,
            "evidenceRefs": ["record:1"], "attempts": attempts, "dependsOn": [],
        }],
        "retired": [], "apis": [],
        "guidance": {"hypothesis": None, "lock": None,
                     "angleIds": [], "confirmedIds": [], "tension": []},
    })
    r = await c.post("/observer/context", headers=_hdr(app), json={"mode": "summary"})
    a = r.json()["assessments"][0]
    assert a["attempts_total"] == 5
    assert [x["id"] for x in a["attempts"]] == ["a2", "a3", "a4"]


@pytest.mark.asyncio
async def test_summary_expands_requested_assessment(client):
    c, app = client
    _ensure_project(app.state.cfg)
    attempts = [
        {"id": f"a{i}", "action": "x", "result": "r", "evidenceRefs": ["record:1"]}
        for i in range(5)
    ]
    _publish_state(app.state.cfg, SESSION, {
        "schema_version": 1, "revision": "rev-1",
        "assessments": [{
            "id": "a-many", "subject": "s", "status": "tried-hit", "role": "direction",
            "api": None, "conclusion": "c", "basis": "b", "uncertainty": None,
            "evidenceRefs": ["record:1"], "attempts": attempts, "dependsOn": [],
        }],
        "retired": [], "apis": [],
        "guidance": {"hypothesis": None, "lock": None,
                     "angleIds": [], "confirmedIds": [], "tension": []},
    })
    r = await c.post(
        "/observer/context",
        headers=_hdr(app),
        json={"mode": "summary", "assessment_ids": ["a-many"]},
    )
    a = r.json()["assessments"][0]
    assert len(a["attempts"]) == 5


# ---------------------------------------------------------------------------
# window_records
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_window_records_lists_items(client):
    c, app = client
    _ensure_project(app.state.cfg)
    for _ in range(3):
        _insert_record(app.state.cfg, SESSION, "bash", input_text='{"cmd":"ls"}')
    conn = _conn(app.state.cfg)
    try:
        conn.execute(
            "UPDATE projects SET pending_window_end = 3 WHERE session_id = ?",
            (SESSION,),
        )
    finally:
        conn.close()

    r = await c.post("/observer/context", headers=_hdr(app),
                     json={"mode": "window_records"})
    body = r.json()
    assert len(body["records"]) == 3
    assert body["records"][0]["tool_name"] == "bash"
    assert body["records"][0]["truncated"] is False


# ---------------------------------------------------------------------------
# record_detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_detail_in_window(client):
    c, app = client
    _ensure_project(app.state.cfg)
    rec_id = _insert_record(
        app.state.cfg, SESSION, "bash",
        input_text='{"cmd":"ls"}', response_text='{"stdout":"a"}',
    )
    conn = _conn(app.state.cfg)
    try:
        conn.execute(
            "UPDATE projects SET pending_window_end = ? WHERE session_id = ?",
            (rec_id, SESSION),
        )
    finally:
        conn.close()

    r = await c.post(
        "/observer/context",
        headers=_hdr(app),
        json={"mode": "record_detail", "record_id": rec_id},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["record_id"] == rec_id
    assert body["segment"]["offset"] == 0
    assert "cmd" in body["segment"]["data"]
    assert "---" in body["segment"]["data"]


@pytest.mark.asyncio
async def test_record_detail_out_of_window_rejected(client):
    c, app = client
    _ensure_project(app.state.cfg)
    rec_id = _insert_record(app.state.cfg, SESSION)
    # 窗口 (0, 0]，record 不在里面

    r = await c.post(
        "/observer/context",
        headers=_hdr(app),
        json={"mode": "record_detail", "record_id": rec_id},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "record_out_of_window"


@pytest.mark.asyncio
async def test_history_record_allows_out_of_window(client):
    c, app = client
    _ensure_project(app.state.cfg)
    rec_id = _insert_record(app.state.cfg, SESSION)
    conn = _conn(app.state.cfg)
    try:
        conn.execute(
            "UPDATE projects SET processed_record_id = ?, pending_window_end = NULL "
            "WHERE session_id = ?",
            (rec_id, SESSION),
        )
    finally:
        conn.close()

    r = await c.post(
        "/observer/context",
        headers=_hdr(app),
        json={"mode": "history_record", "record_id": rec_id},
    )
    assert r.status_code == 200
    assert r.json()["record_id"] == rec_id


@pytest.mark.asyncio
async def test_record_detail_cross_session_rejected(client):
    c, app = client
    _ensure_project(app.state.cfg, SESSION)
    _ensure_project(app.state.cfg, "sess-other")
    other_rec = _insert_record(app.state.cfg, "sess-other")

    r = await c.post(
        "/observer/context",
        headers=_hdr(app, SESSION),
        json={"mode": "record_detail", "record_id": other_rec},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "record_not_found"


@pytest.mark.asyncio
async def test_record_detail_requires_record_id(client):
    c, app = client
    _ensure_project(app.state.cfg)

    r = await c.post(
        "/observer/context",
        headers=_hdr(app),
        json={"mode": "record_detail"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# blackboard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_blackboard_returns_initial_when_none(client):
    c, app = client
    _ensure_project(app.state.cfg)
    r = await c.post("/observer/context", headers=_hdr(app),
                     json={"mode": "blackboard"})
    body = r.json()
    assert body["schema_version"] == 1
    assert body["revision"] is None


@pytest.mark.asyncio
async def test_blackboard_returns_published_state(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _publish_state(app.state.cfg, SESSION, {
        "schema_version": 1, "revision": "rev-X",
        "assessments": [], "retired": [], "apis": [],
        "guidance": {"hypothesis": None, "lock": None,
                     "angleIds": [], "confirmedIds": [], "tension": []},
    })
    r = await c.post("/observer/context", headers=_hdr(app),
                     json={"mode": "blackboard"})
    assert r.json()["revision"] == "rev-X"


# ---------------------------------------------------------------------------
# 身份 / 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_requires_session_token(client):
    c, _ = client
    r = await c.post("/observer/context", json={"mode": "summary"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_context_rejects_body_identity(client):
    c, app = client
    token = app.state.token_registry.issue(SESSION)
    r = await c.post(
        "/observer/context",
        headers={"X-FlySec-Token": token},
        json={"mode": "summary", "session_id": "evil"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_context_session_not_found(client):
    c, app = client
    r = await c.post("/observer/context", headers=_hdr(app, "ghost"),
                     json={"mode": "summary"})
    assert r.status_code == 404
    assert r.json()["code"] == "session_not_found"
