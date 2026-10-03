"""Read-only Web blackboard API and static UI tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from service import db


def create_project(cfg, sid="web-a"):
    conn = db.connect(cfg.data_dir)
    try:
        conn.execute(
            "INSERT INTO projects(session_id,target,objective,created_at) VALUES(?,?,?,?)",
            (sid, "https://example.com", "只读页面验收", "2026-10-03T00:00:00Z"),
        )
    finally:
        conn.close()


def publish(cfg, sid="web-a", revision="rev-1", script_text="safe"):
    state = {
        "schema_version": 1,
        "revision": revision,
        "assessments": [{
            "id": "node-1", "subject": script_text, "status": "tried-hit",
            "role": "direction", "conclusion": "读取成功", "basis": "record:1",
            "uncertainty": None, "evidenceRefs": ["record:1"],
            "attempts": [{"id": "try-1", "action": "读取", "result": script_text,
                          "assessment": None, "evidenceRefs": ["record:1"]}],
            "dependsOn": [], "apiIds": ["api-1"],
        }],
        "retired": [],
        "apis": [{"id": "api-1", "endpoint": "GET /api/demo", "purpose": script_text,
                  "parameters": [{"name": "id", "description": "编号"}],
                  "tests": [{"id": "api-test", "action": "正常请求", "result": "200",
                             "record_ids": [1]}]}],
        "guidance": {"hypothesis": "静态验证", "lock": None,
                     "angleIds": [], "confirmedIds": ["node-1"], "tension": []},
    }
    conn = db.connect(cfg.data_dir)
    try:
        if conn.execute("SELECT 1 FROM tool_records WHERE session_id=?", (sid,)).fetchone() is None:
            conn.execute(
                "INSERT INTO tool_records(session_id,call_key,tool_name,tool_input_json,tool_response_json,metadata_json,received_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (sid, "call-web", "Read", json.dumps({"path": "fixture"}),
                 json.dumps({"text": script_text}), json.dumps({"source": "test"}),
                 "2026-10-03T00:00:01Z"),
            )
        cur = conn.execute(
            "INSERT INTO observations(session_id,trigger,status,start_record_id,end_record_id,state_json,map_text,tool_logs_json,started_at,finished_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (sid, "agent_stop", "published", 0, 1, json.dumps(state),
             f"<observer-map>{script_text}</observer-map>",
             json.dumps([{"op": "observation_context", "arguments": {"mode": "summary"}}]),
             "2026-10-03T00:00:02Z", "2026-10-03T00:00:03Z"),
        )
        obs_id = cur.lastrowid
        conn.execute(
            "UPDATE projects SET current_observation_id=?, processed_record_id=1 WHERE session_id=?",
            (obs_id, sid),
        )
        return obs_id
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_web_shell_and_read_only_apis_are_public_on_loopback(client):
    c, _ = client
    page = await c.get("/web/")
    assert page.status_code == 200
    assert "FlySecAgent" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"
    assert (await c.get("/web/app.js")).status_code == 200
    assert (await c.get("/web/styles.css")).status_code == 200
    assert (await c.get("/web/projects")).status_code == 200


@pytest.mark.asyncio
async def test_projects_and_empty_state(client):
    c, app = client
    create_project(app.state.cfg)
    projects = await c.get("/web/projects")
    assert projects.status_code == 200
    assert projects.json()["projects"][0]["session_id"] == "web-a"
    project = await c.get("/web/project/web-a")
    assert project.status_code == 200
    assert project.json()["state"] is None
    report = await c.get("/web/report/web-a")
    assert "尚未生成黑板" in report.text


@pytest.mark.asyncio
async def test_published_project_record_logs_and_report(client):
    c, app = client
    create_project(app.state.cfg)
    obs_id = publish(app.state.cfg)
    project = (await c.get("/web/project/web-a")).json()
    assert project["state"]["revision"] == "rev-1"
    assert project["state"]["apis"][0]["endpoint"] == "GET /api/demo"
    assert project["observation"]["id"] == obs_id
    record = await c.get("/web/record/web-a/1")
    assert record.json()["record"]["tool_response"] == {"text": "safe"}
    logs = await c.get(f"/web/observation/web-a/{obs_id}/logs")
    assert logs.json()["logs"][0]["op"] == "observation_context"
    report = await c.get("/web/report/web-a")
    assert report.headers["content-type"].startswith("text/markdown")
    assert "GET /api/demo" in report.text
    assert "关联 API：GET /api/demo" in report.text
    assert "关联判断：" in report.text
    assert "record:1" in report.text


@pytest.mark.asyncio
async def test_historical_version_is_scoped_and_selectable(client):
    c, app = client
    create_project(app.state.cfg)
    old_id = publish(app.state.cfg, revision="old")
    new_id = publish(app.state.cfg, revision="new")
    current = await c.get("/web/project/web-a")
    assert current.json()["state"]["revision"] == "new"
    old = await c.get(
        "/web/project/web-a", params={"observation_id": old_id}
    )
    assert old.json()["state"]["revision"] == "old"
    assert new_id != old_id


@pytest.mark.asyncio
async def test_cross_session_record_and_observation_are_hidden(client):
    c, app = client
    create_project(app.state.cfg, "web-a")
    create_project(app.state.cfg, "web-b")
    obs_id = publish(app.state.cfg, "web-a")
    assert (await c.get("/web/record/web-b/1")).status_code == 404
    assert (
        await c.get(f"/web/observation/web-b/{obs_id}/logs")
    ).status_code == 404


def test_frontend_never_uses_inner_html_for_data():
    source = (Path(__file__).resolve().parent.parent / "web/app.js").read_text()
    assert "innerHTML" not in source
    assert "textContent" in source
    assert "renderRelatedApis" in source
    assert "assessment.apiIds" in source
    assert '"api-summary"' in source
    assert 'setAttribute("aria-expanded"' in source
    assert "expandedApiId" in source


@pytest.mark.asyncio
async def test_script_like_text_round_trips_as_data(client):
    c, app = client
    create_project(app.state.cfg)
    payload = '<script>globalThis.pwned=true</script><img src=x onerror=alert(1)>'
    publish(app.state.cfg, script_text=payload)
    project = await c.get("/web/project/web-a")
    assert project.json()["state"]["assessments"][0]["subject"] == payload
    report = await c.get("/web/report/web-a")
    assert payload in report.text
