"""/hook/* 四个路由的端到端测试。

全部走 Task 3 的 httpx client fixture，带服务级 token；DB 由 fixture 清空。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from service import db


def _hdr(app) -> dict[str, str]:
    return {"X-FlySec-Token": app.state.service_token}


async def _ensure(client, app, session_id: str = "sess-1") -> None:
    r = await client.post(
        "/hook/project.ensure",
        headers=_hdr(app),
        json={
            "session_id": session_id,
            "hint": {"target": "example.com", "objective": "test"},
        },
    )
    assert r.status_code == 200


def _conn(cfg) -> sqlite3.Connection:
    return db.connect(cfg.data_dir)


# ---------------------------------------------------------------------------
# project.ensure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_creates_first_time(client):
    c, app = client
    r = await c.post(
        "/hook/project.ensure",
        headers=_hdr(app),
        json={
            "session_id": "sess-1",
            "hint": {"target": "example.com", "objective": "test"},
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "created": True}

    conn = _conn(app.state.cfg)
    try:
        row = conn.execute(
            "SELECT target, objective, observation_enabled FROM projects "
            "WHERE session_id = 'sess-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["target"] == "example.com"
    assert row["objective"] == "test"
    assert row["observation_enabled"] == 1


@pytest.mark.asyncio
async def test_ensure_returns_existing(client):
    c, app = client
    await _ensure(c, app)
    # 第二次：不同 hint
    r = await c.post(
        "/hook/project.ensure",
        headers=_hdr(app),
        json={
            "session_id": "sess-1",
            "hint": {"target": "other.com", "objective": "other"},
        },
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "created": False}

    # 原字段不覆盖
    conn = _conn(app.state.cfg)
    try:
        row = conn.execute(
            "SELECT target, objective FROM projects WHERE session_id = 'sess-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["target"] == "example.com"
    assert row["objective"] == "test"


@pytest.mark.asyncio
async def test_ensure_missing_hint_first_time(client):
    c, app = client
    r = await c.post(
        "/hook/project.ensure",
        headers=_hdr(app),
        json={"session_id": "sess-new"},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "missing_hint"


# ---------------------------------------------------------------------------
# record.ingest
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ingest_writes_record(client):
    c, app = client
    await _ensure(c, app)

    r = await c.post(
        "/hook/record.ingest",
        headers=_hdr(app),
        json={
            "session_id": "sess-1",
            "tool_name": "bash",
            "call_key": "codex-1",
            "tool_input": {"command": "ls"},
            "tool_response": {"stdout": "ok"},
            "metadata": {"cwd": "/w"},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["record_id"] >= 1

    conn = _conn(app.state.cfg)
    try:
        row = conn.execute(
            "SELECT tool_name, call_key, tool_input_json FROM tool_records "
            "WHERE id = ?",
            (body["record_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert row["tool_name"] == "bash"
    assert row["call_key"] == "codex-1"
    assert json.loads(row["tool_input_json"]) == {"command": "ls"}


@pytest.mark.asyncio
async def test_ingest_dedup_same_call_key(client):
    c, app = client
    await _ensure(c, app)

    body_1 = {
        "session_id": "sess-1",
        "tool_name": "bash",
        "call_key": "codex-dup",
        "tool_input": {"a": 1},
        "tool_response": {"b": 2},
        "metadata": {},
    }
    r1 = await c.post("/hook/record.ingest", headers=_hdr(app), json=body_1)
    r2 = await c.post("/hook/record.ingest", headers=_hdr(app), json=body_1)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["record_id"] == r2.json()["record_id"]

    conn = _conn(app.state.cfg)
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM tool_records WHERE call_key = 'codex-dup'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 1


@pytest.mark.asyncio
async def test_ingest_null_call_key_keeps_both(client):
    c, app = client
    await _ensure(c, app)

    body = {
        "session_id": "sess-1",
        "tool_name": "bash",
        "tool_input": "echo hi",
        "tool_response": "hi",
        "metadata": {},
    }
    r1 = await c.post("/hook/record.ingest", headers=_hdr(app), json=body)
    r2 = await c.post("/hook/record.ingest", headers=_hdr(app), json=body)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json()["record_id"] != r2.json()["record_id"]


@pytest.mark.asyncio
async def test_ingest_rejects_unbound_session(client):
    c, app = client
    r = await c.post(
        "/hook/record.ingest",
        headers=_hdr(app),
        json={
            "session_id": "ghost",
            "tool_name": "bash",
            "tool_input": {},
            "tool_response": {},
            "metadata": {},
        },
    )
    assert r.status_code == 409
    assert r.json()["code"] == "session_not_ready"


@pytest.mark.asyncio
async def test_ingest_rejects_disabled_observation(client):
    c, app = client
    await _ensure(c, app)
    conn = _conn(app.state.cfg)
    try:
        conn.execute(
            "UPDATE projects SET observation_enabled = 0 WHERE session_id = 'sess-1'"
        )
    finally:
        conn.close()

    r = await c.post(
        "/hook/record.ingest",
        headers=_hdr(app),
        json={
            "session_id": "sess-1",
            "tool_name": "bash",
            "tool_input": {},
            "tool_response": {},
            "metadata": {},
        },
    )
    assert r.status_code == 409
    assert r.json()["code"] == "session_not_ready"


@pytest.mark.asyncio
async def test_ingest_handles_non_dict_payload(client):
    c, app = client
    await _ensure(c, app)

    r = await c.post(
        "/hook/record.ingest",
        headers=_hdr(app),
        json={
            "session_id": "sess-1",
            "tool_name": "bash",
            "tool_input": "ls -la",
            "tool_response": [1, 2, 3],
            "metadata": {},
        },
    )
    assert r.status_code == 200

    conn = _conn(app.state.cfg)
    try:
        row = conn.execute(
            "SELECT tool_input_json, tool_response_json FROM tool_records "
            "WHERE id = ?",
            (r.json()["record_id"],),
        ).fetchone()
    finally:
        conn.close()
    assert json.loads(row["tool_input_json"]) == "ls -la"
    assert json.loads(row["tool_response_json"]) == [1, 2, 3]


# ---------------------------------------------------------------------------
# map.pending / map.ack —— 需要手动塞一个 published observation
# ---------------------------------------------------------------------------


def _publish_fake_observation(
    cfg,
    session_id: str,
    revision: str,
    map_text: str = "<observer-map/>",
    make_current: bool = True,
) -> int:
    """塞一行 published observation；可选把 projects.current_observation_id 指过去。"""
    state_json = json.dumps({"schema_version":2,"revision":revision,"topics":[],"facts":[],"tests":[],"apis":[],"questions":[]})
    conn = db.connect(cfg.data_dir)
    try:
        with db.transaction(conn):
            cur = conn.execute(
                "INSERT INTO observations "
                "(session_id, trigger, status, start_record_id, end_record_id, "
                " base_observation_id, state_json, map_text, error, "
                " tool_logs_json, started_at, finished_at) "
                "VALUES (?, 'timer_5min', 'published', 0, 0, NULL, ?, ?, NULL, "
                "        '[]', '2026-10-02T00:00:00Z', '2026-10-02T00:00:00Z')",
                (session_id, state_json, map_text),
            )
            obs_id = int(cur.lastrowid)
            if make_current:
                conn.execute(
                    "UPDATE projects SET current_observation_id = ? "
                    "WHERE session_id = ?",
                    (obs_id, session_id),
                )
        return obs_id
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_pending_no_version(client):
    c, app = client
    await _ensure(c, app)
    r = await c.get("/hook/map.pending", params={"session_id": "sess-1"}, headers=_hdr(app))
    assert r.status_code == 200
    assert r.json() == {"ok": True, "revision": None, "map_text": None}


@pytest.mark.asyncio
async def test_pending_has_new_version(client):
    c, app = client
    await _ensure(c, app)
    _publish_fake_observation(app.state.cfg, "sess-1", "rev-abc", "<m>hi</m>")

    r = await c.get("/hook/map.pending", params={"session_id": "sess-1"}, headers=_hdr(app))
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["revision"] == "rev-abc"
    assert body["map_text"] == "<m>hi</m>"


@pytest.mark.asyncio
async def test_pending_already_acked(client):
    c, app = client
    await _ensure(c, app)
    obs_id = _publish_fake_observation(app.state.cfg, "sess-1", "rev-1")

    conn = _conn(app.state.cfg)
    try:
        conn.execute(
            "UPDATE projects SET last_feedback_observation_id = ? WHERE session_id = 'sess-1'",
            (obs_id,),
        )
    finally:
        conn.close()

    r = await c.get("/hook/map.pending", params={"session_id": "sess-1"}, headers=_hdr(app))
    assert r.status_code == 200
    assert r.json()["revision"] is None


@pytest.mark.asyncio
async def test_pending_observation_disabled(client):
    c, app = client
    await _ensure(c, app)
    _publish_fake_observation(app.state.cfg, "sess-1", "rev-1")
    conn = _conn(app.state.cfg)
    try:
        conn.execute(
            "UPDATE projects SET observation_enabled = 0 WHERE session_id = 'sess-1'"
        )
    finally:
        conn.close()

    r = await c.get("/hook/map.pending", params={"session_id": "sess-1"}, headers=_hdr(app))
    assert r.status_code == 200
    assert r.json()["revision"] is None


@pytest.mark.asyncio
async def test_pending_session_not_found(client):
    c, app = client
    r = await c.get("/hook/map.pending", params={"session_id": "ghost"}, headers=_hdr(app))
    assert r.status_code == 404
    assert r.json()["code"] == "session_not_found"


# ---------------------------------------------------------------------------
# map.ack
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ack_valid_revision(client):
    c, app = client
    await _ensure(c, app)
    obs_id = _publish_fake_observation(app.state.cfg, "sess-1", "rev-ok")

    r = await c.post(
        "/hook/map.ack",
        headers=_hdr(app),
        json={"session_id": "sess-1", "revision": "rev-ok"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "acked": True}

    conn = _conn(app.state.cfg)
    try:
        row = conn.execute(
            "SELECT last_feedback_observation_id FROM projects WHERE session_id = 'sess-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["last_feedback_observation_id"] == obs_id


@pytest.mark.asyncio
async def test_ack_unknown_revision(client):
    c, app = client
    await _ensure(c, app)
    _publish_fake_observation(app.state.cfg, "sess-1", "rev-ok")

    r = await c.post(
        "/hook/map.ack",
        headers=_hdr(app),
        json={"session_id": "sess-1", "revision": "rev-unknown"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "revision_outdated"


@pytest.mark.asyncio
async def test_ack_outdated_revision(client):
    c, app = client
    await _ensure(c, app)
    _publish_fake_observation(app.state.cfg, "sess-1", "rev-old", make_current=False)
    newer = _publish_fake_observation(app.state.cfg, "sess-1", "rev-new")

    r = await c.post(
        "/hook/map.ack",
        headers=_hdr(app),
        json={"session_id": "sess-1", "revision": "rev-old"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "revision_outdated"

    # last_feedback 应保持未设置
    conn = _conn(app.state.cfg)
    try:
        row = conn.execute(
            "SELECT last_feedback_observation_id FROM projects WHERE session_id = 'sess-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row["last_feedback_observation_id"] is None
    assert newer  # 占用变量


@pytest.mark.asyncio
async def test_ack_session_not_found(client):
    c, app = client
    r = await c.post(
        "/hook/map.ack",
        headers=_hdr(app),
        json={"session_id": "ghost", "revision": "x"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 整合流程
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_hook_flow(client):
    c, app = client
    await _ensure(c, app, "sess-flow")

    # 3 条记录
    for i in range(3):
        r = await c.post(
            "/hook/record.ingest",
            headers=_hdr(app),
            json={
                "session_id": "sess-flow",
                "tool_name": "bash",
                "call_key": f"call-{i}",
                "tool_input": {"i": i},
                "tool_response": {"ok": True},
                "metadata": {},
            },
        )
        assert r.status_code == 200

    # 还没发布：pending 为空
    r = await c.get(
        "/hook/map.pending", params={"session_id": "sess-flow"}, headers=_hdr(app)
    )
    assert r.json()["revision"] is None

    # 模拟 Observer 发布 v1
    _publish_fake_observation(app.state.cfg, "sess-flow", "flow-v1", "<m>v1</m>")

    # pending 看到
    r = await c.get(
        "/hook/map.pending", params={"session_id": "sess-flow"}, headers=_hdr(app)
    )
    assert r.json()["revision"] == "flow-v1"
    assert r.json()["map_text"] == "<m>v1</m>"

    # ack
    r = await c.post(
        "/hook/map.ack",
        headers=_hdr(app),
        json={"session_id": "sess-flow", "revision": "flow-v1"},
    )
    assert r.status_code == 200

    # 再查 pending → 空
    r = await c.get(
        "/hook/map.pending", params={"session_id": "sess-flow"}, headers=_hdr(app)
    )
    assert r.json()["revision"] is None
