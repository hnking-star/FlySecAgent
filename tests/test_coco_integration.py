"""Integration regression using Coco's captured event shape and real HTTP hooks."""
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

from tests.test_hook_scripts import live_service, _run_hook, _ensure_project
from service import db, observation
from hook.coco import parse_start_prompt


def env_for(svc, **extra):
    return {"FLYSEC_DATA_DIR": str(svc.data_dir), "FLYSEC_API_BASE": svc.api_base, **extra}


def test_parse_direct_chat_activation():
    assert parse_start_prompt("开始进行测试 https://example.com") == (
        "https://example.com",
        "记录授权测试过程，梳理攻击面、API 与测试结果",
    )
    assert parse_start_prompt("请开始渗透测试 api.example.com，重点检查越权") == (
        "api.example.com",
        "重点检查越权",
    )
    assert parse_start_prompt("帮我解释 https://example.com") is None


@pytest.mark.asyncio
async def test_coco_direct_chat_activation_uses_runtime_session_id(live_service):
    svc = live_service
    event = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": "runtime-chat-session",
        "prompt": "开始进行测试 https://example.com，重点检查越权",
    }
    result = await _run_hook("coco", env_for(svc), event)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert "证据记忆已开启" in output["hookSpecificOutput"]["additionalContext"]
    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        row = conn.execute(
            "SELECT target, objective, agent_turn_active FROM projects WHERE session_id=?",
            ("runtime-chat-session",),
        ).fetchone()
        assert row == ("https://example.com", "重点检查越权", 1)
        assert "开始进行测试" not in str(conn.execute("SELECT * FROM projects").fetchall())
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_coco_runtime_identity_and_failure_event(live_service):
    svc = live_service
    env = env_for(svc, FLYSEC_TARGET="fixture", FLYSEC_OBJECTIVE="local integration")
    await _run_hook("coco", env, {"hook_event_name": "SessionStart", "session_id": "runtime-A", "cwd": "/fixture"})
    result = await _run_hook("coco", env, {
        "hook_event_name": "PostToolUseFailure", "session_id": "runtime-A",
        "agent_id": "agent-A", "tool_use_id": "call-1", "tool_name": "Read",
        "tool_input": {"file_path": "missing.txt"}, "error": "file not found",
        "prompt": "CHAT_TEXT_MUST_NOT_BE_SAVED",
    })
    assert result.returncode == 0
    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        row = conn.execute("SELECT session_id, tool_response_json, metadata_json FROM tool_records").fetchone()
        assert row[0] == "runtime-A"
        assert json.loads(row[1]) == {"error": "file not found", "is_error": True}
        assert json.loads(row[2])["hook_event_name"] == "PostToolUseFailure"
        assert "CHAT_TEXT_MUST_NOT_BE_SAVED" not in str(row)
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_coco_unenrolled_session_is_not_collected(live_service):
    svc = live_service
    await _run_hook("coco", env_for(svc), {"hook_event_name": "PostToolUse", "session_id": "unknown", "tool_name": "Read", "tool_response": "private"})
    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM tool_records").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_coco_offline_replay_is_lossless_and_bounded(live_service):
    svc = live_service
    await _ensure_project(svc, "offline")
    event = {"hook_event_name": "PostToolUse", "session_id": "offline", "tool_name": "Read", "tool_use_id": "offline-1", "tool_input": {}, "tool_response": "汉字🙂" * 10000}
    started = time.monotonic()
    r = await _run_hook("coco", env_for(svc, FLYSEC_API_BASE="http://127.0.0.1:1"), event)
    assert r.returncode == 0 and time.monotonic() - started < 3
    path = svc.data_dir / "queue/pending.jsonl"
    assert json.loads(path.read_text())["payload"]["tool_response"] == event["tool_response"]
    await _run_hook("coco", env_for(svc), {"hook_event_name": "SessionStart", "session_id": "offline"})
    assert path.read_text() == ""
    # Replaying the same runtime call keeps one stored record.
    await _run_hook("coco", env_for(svc), event)
    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        rows = conn.execute("SELECT tool_response_json FROM tool_records WHERE session_id='offline'").fetchall()
        assert len(rows) == 1
        assert json.loads(rows[0][0]) == event["tool_response"]
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_curator_utf8_and_window_isolation(client):
    c, app = client
    headers = {"X-FlySec-Token": app.state.service_token}
    for sid in ["A", "B"]:
        await c.post("/hook/project.ensure", headers=headers, json={"session_id": sid, "hint": {"target": "fixture", "objective": "test"}})
    original = {"text": "中文🙂验证" * 1000}
    r = await c.post("/hook/record.ingest", headers=headers, json={"session_id": "A", "tool_name": "Read", "tool_input": {}, "tool_response": original})
    record_id = r.json()["record_id"]
    conn = db.connect(app.state.cfg.data_dir)
    try:
        observation.start_observation(conn, "A", "timer_5min")
    finally:
        conn.close()
    curator_headers = {"X-FlySec-Token": app.state.token_registry.issue("A")}
    chunks, offset = [], 0
    while True:
        response = await c.post("/memory/read", headers=curator_headers, json={"mode": "record", "record_id": record_id, "offset": offset, "length": 511})
        assert response.status_code == 200
        segment = response.json()["segment"]
        chunks.append(segment["data"])
        offset = segment["next_offset"]
        if not segment["has_more"]:
            break
    decoded = json.loads("".join(chunks).split("\n---\n", 1)[1])
    assert decoded == original
    other_headers = {"X-FlySec-Token": app.state.token_registry.issue("B")}
    cross = await c.post("/memory/read", headers=other_headers, json={"mode": "record", "record_id": record_id})
    assert cross.status_code == 404


@pytest.mark.asyncio
async def test_coco_installer_preserves_existing_hooks(tmp_path):
    path = tmp_path / ".trae/hooks.json"
    path.parent.mkdir()
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo existing"}]}]}}
    path.write_text(json.dumps(existing))
    cmd = [sys.executable, "-m", "hook.install_coco", str(tmp_path)]
    await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
    first = path.read_text()
    await asyncio.to_thread(subprocess.run, cmd, check=True, capture_output=True)
    assert path.read_text() == first
    assert json.loads(first)["hooks"]["Stop"][0] == existing["hooks"]["Stop"][0]
    await asyncio.to_thread(subprocess.run, [*cmd, "--uninstall"], check=True, capture_output=True)
    assert json.loads(path.read_text()) == existing


@pytest.mark.asyncio
async def test_coco_concurrent_offline_hooks_keep_every_record(live_service):
    svc = live_service
    await _ensure_project(svc, "parallel")
    offline = env_for(svc, FLYSEC_API_BASE="http://127.0.0.1:1")
    results = await asyncio.gather(*[
        _run_hook("coco", offline, {"hook_event_name": "PostToolUse", "session_id": "parallel",
                  "tool_name": "Read", "tool_use_id": f"parallel-{i}",
                  "tool_input": {"i": i}, "tool_response": [i, "ok"]})
        for i in range(12)
    ])
    assert all(result.returncode == 0 for result in results)
    queue = svc.data_dir / "queue/pending.jsonl"
    payloads = [json.loads(line)["payload"] for line in queue.read_text().splitlines()]
    payloads += [json.loads(p.read_text())["payload"] for p in (svc.data_dir / "queue/spool").glob("*.json")]
    assert {p["call_key"] for p in payloads} == {f"parallel-{i}" for i in range(12)}, [r.stderr for r in results]
    for _ in range(3):
        await _run_hook("coco", env_for(svc), {"hook_event_name": "SessionStart", "session_id": "parallel"})
    assert queue.read_text() == ""
    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM tool_records WHERE session_id='parallel'").fetchone()[0] == 12
    finally:
        conn.close()


@pytest.mark.asyncio
async def test_camel_events_interrupt_and_late_tool_do_not_restart_finished_turn(live_service):
    svc = live_service; sid = 'camel-interrupt'; env = env_for(svc)
    await _run_hook('coco', env, {'hook_event_name':'userPromptSubmit', 'session_id':sid, 'prompt':'开始进行测试 https://fixture.test'})
    await _run_hook('coco', env, {'hook_event_name':'interrupt', 'session_id':sid})
    await _run_hook('coco', env, {'hook_event_name':'postToolUse', 'session_id':sid, 'tool_use_id':'late', 'tool_name':'Read', 'tool_input':{}, 'tool_response':{'stdout':'late local receipt'}})
    conn = sqlite3.connect(svc.data_dir / 'flysec.db')
    try:
        assert conn.execute('SELECT agent_turn_active,agent_activity_known FROM projects WHERE session_id=?', (sid,)).fetchone()==(0,1)
        assert conn.execute('SELECT count(*) FROM tool_records WHERE session_id=?', (sid,)).fetchone()[0]==1
    finally: conn.close()


@pytest.mark.asyncio
async def test_failure_hook_preserves_actual_partial_response(live_service):
    from service.evidence import execution_status
    svc = live_service; sid = 'partial-error'; await _ensure_project(svc,sid)
    original = {'stdout':'partial local output 中文🙂', 'stderr':'diagnostic detail'}
    await _run_hook('coco',env_for(svc),{'hook_event_name':'PostToolUseFailure','session_id':sid,'tool_use_id':'partial','tool_name':'Read','tool_input':{'path':'local-fixture'},'tool_response':original,'error':'The tool failed after producing partial output'})
    conn = sqlite3.connect(svc.data_dir / 'flysec.db')
    try:
        response,metadata = conn.execute('SELECT tool_response_json,metadata_json FROM tool_records WHERE session_id=?',(sid,)).fetchone()
        assert json.loads(response)==original
        assert json.loads(metadata)['error']=='The tool failed after producing partial output'
        assert execution_status(json.loads(response),json.loads(metadata))=='error'
    finally:conn.close()


@pytest.mark.asyncio
async def test_busy_queue_lock_has_durable_fallback_and_replays(live_service, monkeypatch):
    import contextlib
    import hook.common as common
    svc = live_service; sid = 'busy-queue'; await _ensure_project(svc,sid)
    cfg = common.HookConfig(svc.api_base,svc.data_dir,svc.token)
    original_lock = common._queue_lock
    @contextlib.contextmanager
    def busy_lock(*args, **kwargs):
        raise TimeoutError('controlled append lock contention')
        yield
    monkeypatch.setattr(common,'_queue_lock',busy_lock)
    payload = {'session_id':sid, 'call_key':'busy-1', 'tool_name':'Read', 'tool_input':{}, 'tool_response':'中文🙂'*1000, 'metadata':{}}
    common.enqueue_ingest(cfg,payload)
    files = list((svc.data_dir/'queue/spool').glob('*.json'))
    assert len(files)==1 and json.loads(files[0].read_text())['payload']==payload
    assert files[0].stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(common,'_queue_lock',original_lock)
    delivered = await asyncio.to_thread(common.flush_queue,cfg)
    assert delivered==1 and not files[0].exists()
    conn=sqlite3.connect(svc.data_dir/'flysec.db')
    try:
        assert json.loads(conn.execute('SELECT tool_response_json FROM tool_records WHERE session_id=?',(sid,)).fetchone()[0])==payload['tool_response']
    finally:conn.close()
