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
    assert "观察已开启" in output["hookSpecificOutput"]["additionalContext"]
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
async def test_observer_utf8_and_window_isolation(client):
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
    observer_headers = {"X-FlySec-Token": app.state.token_registry.issue("A")}
    chunks, offset = [], 0
    while True:
        response = await c.post("/observer/context", headers=observer_headers, json={"mode": "record_detail", "record_id": record_id, "offset": offset, "length": 511})
        assert response.status_code == 200
        segment = response.json()["segment"]
        chunks.append(segment["data"])
        offset = segment["next_offset"]
        if not segment["has_more"]:
            break
    decoded = json.loads("".join(chunks).split("\n---\n", 1)[1])
    assert decoded == original
    other_headers = {"X-FlySec-Token": app.state.token_registry.issue("B")}
    cross = await c.post("/observer/context", headers=other_headers, json={"mode": "history_record", "record_id": record_id})
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
    assert {p["call_key"] for p in payloads} == {f"parallel-{i}" for i in range(12)}
    for _ in range(3):
        await _run_hook("coco", env_for(svc), {"hook_event_name": "SessionStart", "session_id": "parallel"})
    assert queue.read_text() == ""
    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        assert conn.execute("SELECT COUNT(*) FROM tool_records WHERE session_id='parallel'").fetchone()[0] == 12
    finally:
        conn.close()
