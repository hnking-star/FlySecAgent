"""Hook 脚本单元测试：对接真起的 FastAPI 子服务。

由于 Hook 是同步子进程，我们用 run_in_executor 在线程里跑，
让 pytest 的 asyncio event loop 能继续响应 HTTP 请求。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn

from service.app import create_app
from service.config import Config
from service.db import init_db


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _LiveService:
    def __init__(self, cfg: Config, port: int, server, task: asyncio.Task) -> None:
        self.cfg = cfg
        self.port = port
        self.server = server
        self._task = task

    @property
    def api_base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def token(self) -> str:
        return (self.cfg.data_dir / ".secret").read_text().strip()

    @property
    def data_dir(self) -> Path:
        return self.cfg.data_dir

    async def shutdown(self) -> None:
        self.server.should_exit = True
        await self._task


@pytest_asyncio.fixture
async def live_service(tmp_path):
    port = _free_port()
    cfg = Config(port=port, data_dir=tmp_path)
    init_db(cfg.data_dir)
    app = create_app(cfg)
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_config=None, lifespan="on"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)
    svc = _LiveService(cfg=cfg, port=port, server=server, task=task)
    try:
        yield svc
    finally:
        await svc.shutdown()


async def _run_hook(
    module: str, env: dict[str, str], event: dict
) -> subprocess.CompletedProcess:
    repo_root = Path(__file__).resolve().parent.parent
    merged_env = {**os.environ, **env}

    def _invoke() -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", f"hook.{module}"],
            input=json.dumps(event),
            env=merged_env,
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=15,
        )

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _invoke)


async def _ensure_project(svc: _LiveService, session_id: str) -> None:
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"{svc.api_base}/hook/project.ensure",
            headers={"X-FlySec-Token": svc.token},
            json={
                "session_id": session_id,
                "hint": {"target": "example.com", "objective": "test"},
            },
        )
        assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_start_no_crash_without_project(live_service):
    svc = live_service
    env = {
        "FLYSEC_API_BASE": svc.api_base,
        "FLYSEC_DATA_DIR": str(svc.data_dir),
    }
    result = await _run_hook("on_session_start", env, {"session_id": "sess-x"})
    assert result.returncode == 0
    # 不输出 additionalContext
    assert result.stdout.strip() == ""


@pytest.mark.asyncio
async def test_user_prompt_begins_agent_turn(live_service):
    svc = live_service
    await _ensure_project(svc, "sess-up")

    env = {
        "FLYSEC_API_BASE": svc.api_base,
        "FLYSEC_DATA_DIR": str(svc.data_dir),
    }
    result = await _run_hook(
        "on_user_prompt", env, {"session_id": "sess-up", "prompt_text": "hi"}
    )
    assert result.returncode == 0

    async with httpx.AsyncClient() as c:
        r = await c.get(
            f"{svc.api_base}/hook/map.pending",
            params={"session_id": "sess-up"},
            headers={"X-FlySec-Token": svc.token},
        )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_post_tool_use_ingests_record(live_service):
    svc = live_service
    await _ensure_project(svc, "sess-pt")

    env = {
        "FLYSEC_API_BASE": svc.api_base,
        "FLYSEC_DATA_DIR": str(svc.data_dir),
    }
    event = {
        "session_id": "sess-pt",
        "tool_name": "bash",
        "tool_use_id": "call-1",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "a"},
        "turn_id": "t-1",
        "cwd": "/w",
    }
    result = await _run_hook("post_tool_use", env, event)
    assert result.returncode == 0

    import sqlite3

    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        row = conn.execute(
            "SELECT tool_name, call_key FROM tool_records WHERE session_id = ?",
            ("sess-pt",),
        ).fetchone()
    finally:
        conn.close()
    assert row == ("bash", "call-1")


@pytest.mark.asyncio
async def test_post_tool_use_queues_when_service_down(tmp_path):
    (tmp_path / ".secret").write_text("fake-token-does-not-matter\n")
    unused_port = _free_port()

    env = {
        "FLYSEC_API_BASE": f"http://127.0.0.1:{unused_port}",
        "FLYSEC_DATA_DIR": str(tmp_path),
    }
    event = {
        "session_id": "sess-off",
        "tool_name": "bash",
        "tool_use_id": "call-off",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "a"},
    }
    result = await _run_hook("post_tool_use", env, event)
    assert result.returncode == 0

    queue_file = tmp_path / "queue" / "pending.jsonl"
    assert queue_file.exists()
    lines = queue_file.read_text().strip().split("\n")
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["payload"]["call_key"] == "call-off"


@pytest.mark.asyncio
async def test_flush_queue_replays_pending(live_service):
    svc = live_service
    await _ensure_project(svc, "sess-fl")

    queue_file = svc.data_dir / "queue" / "pending.jsonl"
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    with queue_file.open("w", encoding="utf-8") as fh:
        for i in range(2):
            fh.write(
                json.dumps(
                    {
                        "ts": "2026-01-01T00:00:00Z",
                        "payload": {
                            "session_id": "sess-fl",
                            "tool_name": "bash",
                            "call_key": f"flushed-{i}",
                            "tool_input": {},
                            "tool_response": {},
                            "metadata": {},
                        },
                    }
                )
                + "\n"
            )

    env = {
        "FLYSEC_API_BASE": svc.api_base,
        "FLYSEC_DATA_DIR": str(svc.data_dir),
    }
    await _run_hook("on_session_start", env, {"session_id": "sess-fl"})

    assert queue_file.read_text().strip() == ""
    import sqlite3

    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM tool_records WHERE session_id = ?",
            ("sess-fl",),
        ).fetchone()[0]
    finally:
        conn.close()
    assert count == 2


@pytest.mark.asyncio
async def test_stop_calls_agent_turn_stop(live_service):
    svc = live_service
    await _ensure_project(svc, "sess-st")

    env = {
        "FLYSEC_API_BASE": svc.api_base,
        "FLYSEC_DATA_DIR": str(svc.data_dir),
    }
    await _run_hook("on_user_prompt", env, {"session_id": "sess-st"})
    result = await _run_hook("on_stop", env, {"session_id": "sess-st"})
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_hook_without_secret_noop(tmp_path):
    env = {
        "FLYSEC_API_BASE": "http://127.0.0.1:1",
        "FLYSEC_DATA_DIR": str(tmp_path),
    }
    for script in ("on_session_start", "on_user_prompt", "post_tool_use", "on_stop"):
        result = await _run_hook(
            script, env, {"session_id": "x", "tool_name": "bash"}
        )
        assert result.returncode == 0, f"{script} failed: {result.stderr}"


@pytest.mark.asyncio
async def test_hook_pending_map_injected(live_service):
    svc = live_service
    await _ensure_project(svc, "sess-map")

    import sqlite3

    state_json = json.dumps({"revision": "map-rev-1", "assessments": [], "apis": []})
    conn = sqlite3.connect(svc.data_dir / "flysec.db")
    try:
        cur = conn.execute(
            "INSERT INTO observations "
            "(session_id, trigger, status, start_record_id, end_record_id, "
            " state_json, map_text, tool_logs_json, started_at, finished_at) "
            "VALUES ('sess-map', 'timer_5min', 'published', 0, 0, ?, "
            "        '<observer-map>hi</observer-map>', '[]', "
            "        '2026-10-02T00:00:00Z', '2026-10-02T00:00:00Z')",
            (state_json,),
        )
        obs_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE projects SET current_observation_id = ? WHERE session_id = 'sess-map'",
            (obs_id,),
        )
        conn.commit()
    finally:
        conn.close()

    env = {
        "FLYSEC_API_BASE": svc.api_base,
        "FLYSEC_DATA_DIR": str(svc.data_dir),
    }
    event = {
        "session_id": "sess-map",
        "tool_name": "bash",
        "tool_use_id": "call-m",
        "tool_input": {"c": "x"},
        "tool_response": {"r": "y"},
    }
    result = await _run_hook("post_tool_use", env, event)
    assert result.returncode == 0
    assert "<observer-map>hi</observer-map>" in result.stdout
    out = json.loads(result.stdout.strip())
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    assert (
        out["hookSpecificOutput"]["additionalContext"]
        == "<observer-map>hi</observer-map>"
    )
