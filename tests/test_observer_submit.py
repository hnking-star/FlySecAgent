"""/observer/submit 的端到端测试：合并、错误码、幂等。"""

from __future__ import annotations

import json

import pytest

from service import db


SESSION = "sess-sub"


def _hdr(app, session_id: str = SESSION) -> dict[str, str]:
    token = app.state.token_registry.issue(session_id)
    return {"X-FlySec-Token": token}


def _ensure_project(cfg, session_id: str = SESSION) -> None:
    conn = db.connect(cfg.data_dir)
    try:
        with db.transaction(conn):
            conn.execute(
                "INSERT INTO projects (session_id, target, objective, created_at) "
                "VALUES (?, 'example.com', 'test', '2026-10-02T00:00:00Z')",
                (session_id,),
            )
    finally:
        conn.close()


def _insert_records(cfg, session_id: str, count: int = 5) -> list[int]:
    ids = []
    conn = db.connect(cfg.data_dir)
    try:
        with db.transaction(conn):
            for _ in range(count):
                cur = conn.execute(
                    "INSERT INTO tool_records "
                    "(session_id, tool_name, tool_input_json, tool_response_json, "
                    " metadata_json, received_at) "
                    "VALUES (?, 'bash', '{\"cmd\":\"ls\"}', '{\"stdout\":\"a\"}', "
                    "        '{}', '2026-10-02T00:00:00Z')",
                    (session_id,),
                )
                ids.append(int(cur.lastrowid))
        return ids
    finally:
        conn.close()


def _basic_asmt(
    id_: str = "recon-auth",
    status: str = "tried-hit",
    conclusion: str = "found /api/login",
    attempts: list | None = None,
    evidence: str = "record:1",
    dependsOn: list[str] | None = None,
    apiIds: list[str] | None = None,
) -> dict:
    return {
        "id": id_,
        "subject": "subject text",
        "status": status,
        "role": "direction",
        "conclusion": conclusion,
        "basis": "fact",
        "uncertainty": None,
        "evidenceRefs": [evidence],
        "attempts": attempts
        if attempts is not None
        else (
            [
                {
                    "id": "crawl",
                    "action": "do",
                    "result": "ok",
                    "evidenceRefs": [evidence],
                }
            ]
            if status != "inferred-open"
            else []
        ),
        "dependsOn": dependsOn or [],
        "apiIds": apiIds or [],
    }


# ---------------------------------------------------------------------------
# 身份
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_requires_session_token(client):
    c, _ = client
    r = await c.post("/observer/submit", json={"baseRevision": None})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_submit_rejects_body_identity(client):
    c, app = client
    token = app.state.token_registry.issue(SESSION)
    r = await c.post(
        "/observer/submit",
        headers={"X-FlySec-Token": token},
        json={"baseRevision": None, "session_id": "evil"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_submit_session_not_found(client):
    c, app = client
    r = await c.post(
        "/observer/submit", headers=_hdr(app, "ghost"), json={"baseRevision": None}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 首次发布
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_first_publish(client):
    c, app = client
    _ensure_project(app.state.cfg)
    rec_ids = _insert_records(app.state.cfg, SESSION, 2)

    payload = {
        "baseRevision": None,
        "upserts": [_basic_asmt(evidence=f"record:{rec_ids[0]}")],
        "apis": [],
    }
    r = await c.post("/observer/submit", headers=_hdr(app), json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["unchanged"] is False
    assert body["revision"]

    # projects 推进
    conn = db.connect(app.state.cfg.data_dir)
    try:
        proj = conn.execute(
            "SELECT current_observation_id, processed_record_id, pending_window_end "
            "FROM projects WHERE session_id = ?",
            (SESSION,),
        ).fetchone()
    finally:
        conn.close()
    assert proj["current_observation_id"] is not None
    assert proj["pending_window_end"] is None
    assert proj["processed_record_id"] == rec_ids[-1]


# ---------------------------------------------------------------------------
# stale_revision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_stale_revision(client):
    c, app = client
    _ensure_project(app.state.cfg)
    rec_ids = _insert_records(app.state.cfg, SESSION, 1)

    first = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [_basic_asmt(evidence=f"record:{rec_ids[0]}")],
        },
    )
    assert first.json()["ok"]

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": "wrong-revision",
            "upserts": [
                _basic_asmt(id_="recon-auth2", evidence=f"record:{rec_ids[0]}")
            ],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "stale_revision"


# ---------------------------------------------------------------------------
# unchanged
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_unchanged_second_time(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    first = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": None, "upserts": [_basic_asmt()]},
    )
    assert first.json()["ok"]
    rev1 = first.json()["revision"]

    # 相同 payload 但 baseRevision 更新 → 内容幂等 → unchanged
    second = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": rev1, "upserts": [_basic_asmt()]},
    )
    body = second.json()
    assert body["ok"] is True
    assert body["unchanged"] is True


# ---------------------------------------------------------------------------
# unknown_evidence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_unknown_evidence(client):
    c, app = client
    _ensure_project(app.state.cfg)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [_basic_asmt(evidence="record:999")],
        },
    )
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "unknown_evidence"


@pytest.mark.asyncio
async def test_submit_cross_session_evidence(client):
    c, app = client
    _ensure_project(app.state.cfg, SESSION)
    _ensure_project(app.state.cfg, "sess-other")
    other_ids = _insert_records(app.state.cfg, "sess-other", 1)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app, SESSION),
        json={
            "baseRevision": None,
            "upserts": [_basic_asmt(evidence=f"record:{other_ids[0]}")],
        },
    )
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "unknown_evidence"


# ---------------------------------------------------------------------------
# pydantic 侧的 schema_invalid（status=tried-* 必须有 attempt）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_tried_without_attempt_is_schema_invalid(client):
    c, app = client
    _ensure_project(app.state.cfg)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [_basic_asmt(status="tried-miss", attempts=[])],
        },
    )
    assert r.status_code == 400
    assert r.json()["code"] == "schema_invalid"


# ---------------------------------------------------------------------------
# dependsOn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_unknown_dependency(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [_basic_asmt(dependsOn=["ghost"])],
        },
    )
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "unknown_dependency"


@pytest.mark.asyncio
async def test_submit_cycle(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [
                _basic_asmt(id_="a", dependsOn=["b"]),
                _basic_asmt(id_="b", dependsOn=["a"]),
            ],
        },
    )
    body = r.json()
    assert body["ok"] is False
    assert any(e["code"] == "cycle" for e in body["errors"])


# ---------------------------------------------------------------------------
# conflicting_update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_conflicting_attempt(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    # 首次：attempt crawl action="do"
    first = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": None, "upserts": [_basic_asmt()]},
    )
    rev1 = first.json()["revision"]

    # 再次：相同 attempt.id 不同 action
    conflict = _basic_asmt(
        attempts=[
            {
                "id": "crawl",
                "action": "DIFFERENT",
                "result": "ok",
                "evidenceRefs": ["record:1"],
            }
        ]
    )
    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": rev1, "upserts": [conflict]},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "conflicting_update"


# ---------------------------------------------------------------------------
# retire
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_retire_moves_to_retired(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    first = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": None, "upserts": [_basic_asmt()]},
    )
    rev1 = first.json()["revision"]

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": rev1, "retireIds": ["recon-auth"]},
    )
    assert r.json()["ok"]

    # 检查落库
    conn = db.connect(app.state.cfg.data_dir)
    try:
        cur_obs = conn.execute(
            "SELECT current_observation_id FROM projects WHERE session_id = ?",
            (SESSION,),
        ).fetchone()["current_observation_id"]
        state = json.loads(
            conn.execute(
                "SELECT state_json FROM observations WHERE id = ?",
                (cur_obs,),
            ).fetchone()["state_json"]
        )
    finally:
        conn.close()
    assert state["assessments"] == []
    assert state["retired"][0]["id"] == "recon-auth"


@pytest.mark.asyncio
async def test_submit_retire_unknown_id(client):
    c, app = client
    _ensure_project(app.state.cfg)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": None, "retireIds": ["ghost"]},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "unknown_id"


# ---------------------------------------------------------------------------
# reserved_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_reserved_id(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [_basic_asmt(id_="host-flag-1")],
        },
    )
    body = r.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "reserved_id"


# ---------------------------------------------------------------------------
# API 关联与测试追加
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_node_links_api_and_later_tests_append(client):
    c, app = client
    _ensure_project(app.state.cfg)
    first_record = _insert_records(app.state.cfg, SESSION, 1)[0]

    first = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [
                _basic_asmt(
                    evidence=f"record:{first_record}", apiIds=["api-user"]
                )
            ],
            "apis": [
                {
                    "id": "api-user",
                    "endpoint": "GET /api/user",
                    "purpose": "查询用户",
                    "parameters": [{"name": "id"}],
                    "tests": [],
                }
            ],
        },
    )
    assert first.json()["ok"] is True

    second_record = _insert_records(app.state.cfg, SESSION, 1)[0]
    second = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": first.json()["revision"],
            "apis": [
                {
                    "id": "api-user",
                    "endpoint": "GET /api/user",
                    "purpose": "查询用户",
                    "parameters": [{"name": "id"}],
                    "tests": [
                        {
                            "id": "idor",
                            "action": "把 id 从 123 替换为 456",
                            "result": "返回其他用户信息",
                            "record_ids": [second_record],
                        }
                    ],
                }
            ],
        },
    )
    assert second.json()["ok"] is True

    third_record = _insert_records(app.state.cfg, SESSION, 1)[0]
    third = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": second.json()["revision"],
            "apis": [
                {
                    "id": "api-user",
                    "endpoint": "GET /api/user",
                    "purpose": "查询用户",
                    "parameters": [{"name": "id"}],
                    "tests": [
                        {
                            "id": "sqli",
                            "action": "把 id 替换为单引号 payload",
                            "result": "返回参数错误",
                            "record_ids": [third_record],
                        }
                    ],
                }
            ],
        },
    )
    assert third.json()["ok"] is True

    conn = db.connect(app.state.cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT state_json FROM observations WHERE session_id=? "
            "AND status='published' ORDER BY id DESC LIMIT 1",
            (SESSION,),
        ).fetchone()
        state = json.loads(row["state_json"])
    finally:
        conn.close()
    assert state["assessments"][0]["apiIds"] == ["api-user"]
    assert [test["id"] for test in state["apis"][0]["tests"]] == ["idor", "sqli"]


@pytest.mark.asyncio
async def test_submit_rejects_unknown_api_reference(client):
    c, app = client
    _ensure_project(app.state.cfg)
    record_id = _insert_records(app.state.cfg, SESSION, 1)[0]

    response = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [
                _basic_asmt(evidence=f"record:{record_id}", apiIds=["missing-api"])
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["errors"][0]["code"] == "unknown_api"
    assert body["errors"][0]["path"] == "/upserts/0/apiIds/0"


# ---------------------------------------------------------------------------
# API 规范化
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_api_endpoint_normalization(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    r = await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "apis": [
                {
                    "id": "api-x",
                    "endpoint": "POST /api/x/?a=1",
                    "purpose": "x",
                    "parameters": [],
                    "tests": [],
                }
            ],
        },
    )
    assert r.json()["ok"]

    conn = db.connect(app.state.cfg.data_dir)
    try:
        cur_obs = conn.execute(
            "SELECT current_observation_id FROM projects WHERE session_id = ?",
            (SESSION,),
        ).fetchone()["current_observation_id"]
        state = json.loads(
            conn.execute(
                "SELECT state_json FROM observations WHERE id = ?",
                (cur_obs,),
            ).fetchone()["state_json"]
        )
    finally:
        conn.close()
    assert state["apis"][0]["endpoint"] == "POST /api/x"


# ---------------------------------------------------------------------------
# 幂等缓存
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_idempotent_within_cache(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    payload = {"baseRevision": None, "upserts": [_basic_asmt()]}
    first = await c.post("/observer/submit", headers=_hdr(app), json=payload)
    second = await c.post("/observer/submit", headers=_hdr(app), json=payload)

    assert first.json()["ok"]
    assert second.json() == first.json()  # 返回原响应


# ---------------------------------------------------------------------------
# tool_logs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_appends_tool_log(client):
    c, app = client
    _ensure_project(app.state.cfg)
    _insert_records(app.state.cfg, SESSION, 1)

    # 一次失败（未知证据）
    await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={
            "baseRevision": None,
            "upserts": [_basic_asmt(evidence="record:999")],
        },
    )
    # 一次成功
    await c.post(
        "/observer/submit",
        headers=_hdr(app),
        json={"baseRevision": None, "upserts": [_basic_asmt()]},
    )

    conn = db.connect(app.state.cfg.data_dir)
    try:
        row = conn.execute(
            "SELECT tool_logs_json FROM observations "
            "WHERE session_id = ? ORDER BY id DESC LIMIT 1",
            (SESSION,),
        ).fetchone()
    finally:
        conn.close()
    logs = json.loads(row["tool_logs_json"])
    assert len(logs) >= 1
    assert all(e["op"] == "observation_submit" for e in logs)
