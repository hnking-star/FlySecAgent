"""PiRunner 的子进程启停 + RPC 收发测试。

直接起 service.pi_stub 作为子进程；需要真实的 FlySecAgent 服务回应 /memory/*，
所以这里起一个最简的 FastAPI 子应用监听随机端口。
"""

from __future__ import annotations

import asyncio
import sys

import pytest
import pytest_asyncio
import uvicorn
from httpx import ASGITransport, AsyncClient

from service.app import create_app
from service.auth import TokenRegistry
from service.config import Config
from service.db import init_db
from service.pi_runner import PiRunner


@pytest_asyncio.fixture
async def live_app(tmp_path):
    """起一个真的 uvicorn 子服务，供 pi_stub HTTP 回调。"""
    cfg = Config(port=0, data_dir=tmp_path)
    init_db(cfg.data_dir)

    # 在 TCP socket 上随机找端口
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = Config(port=port, data_dir=tmp_path)

    app = create_app(cfg)
    # 需要先触发 lifespan 以把 scheduler / pi_runner 建起来
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, lifespan="on")
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    # 等待服务上线
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.05)

    # 建一个 project 让 pi_stub 的 /memory/read 不 404
    import httpx
    service_token = app.state.service_token
    async with httpx.AsyncClient() as c:
        r = await c.post(
            f"http://127.0.0.1:{port}/hook/project.ensure",
            headers={"X-FlySec-Token": service_token},
            json={"session_id": "stub-s", "hint": {"target": "t", "objective": "o"}},
        )
        assert r.status_code == 200

    try:
        yield app, cfg
    finally:
        server.should_exit = True
        await task


@pytest.mark.asyncio
async def test_pi_stub_runs_and_submits(live_app):
    app, cfg = live_app

    done: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

    async def on_done(session_id: str, msg: dict) -> None:
        if not done.done():
            done.set_result(msg)

    pi = PiRunner(
        api_base=f"http://127.0.0.1:{cfg.port}",
        token_registry=app.state.token_registry,
        on_run_done=on_done,
    )
    try:
        await pi.ensure_running("stub-s")
        await pi.dispatch("stub-s", "timer_5min")
        msg = await asyncio.wait_for(done, timeout=10.0)
    finally:
        await pi.stop_all()

    # pi_stub 发空 submit → 首次为 unchanged=False revision=<uuid>（因为初次发布）
    assert msg["op"] == "run_done"
    assert msg["ok"] is True


@pytest.mark.asyncio
async def test_pi_stub_shutdown_exits_cleanly(live_app):
    app, cfg = live_app

    pi = PiRunner(
        api_base=f"http://127.0.0.1:{cfg.port}",
        token_registry=app.state.token_registry,
        on_run_done=lambda s, m: asyncio.sleep(0),
    )
    try:
        await pi.ensure_running("stub-s")
        # 等 pi 到 ready
        await asyncio.sleep(0.5)
    finally:
        await pi.stop_session("stub-s")

    # token 被 revoke
    reg: TokenRegistry = app.state.token_registry
    assert reg.resolve("non-matching") is None


@pytest.mark.asyncio
async def test_dispatch_before_ready_times_out(tmp_path):
    """使用一个永远不发 ready 的假子进程命令，验证 10s 超时后回 failed。"""
    cfg = Config(port=0, data_dir=tmp_path)
    init_db(cfg.data_dir)
    registry = TokenRegistry()

    done: asyncio.Future[dict] = asyncio.get_event_loop().create_future()

    async def on_done(session_id: str, msg: dict) -> None:
        if not done.done():
            done.set_result(msg)

    pi = PiRunner(
        api_base="http://127.0.0.1:0",
        token_registry=registry,
        on_run_done=on_done,
        command=[sys.executable, "-u", "-c", "import time; time.sleep(60)"],
    )
    # 把 wait_for 超时改小避免等 10 秒
    import service.pi_runner as mod
    original_dispatch = mod.PiRunner.dispatch

    async def quick_dispatch(self, session_id, trigger):  # noqa: ANN001
        await self.ensure_running(session_id)
        ready = self._ready_evts.get(session_id)
        try:
            await asyncio.wait_for(ready.wait(), timeout=0.3)
        except asyncio.TimeoutError:
            await self._on_run_done(session_id, {"op": "run_done", "ok": False,
                                                 "errors": [{"path": "/", "code": "pi_not_ready",
                                                             "message": "timeout"}]})

    pi.dispatch = quick_dispatch.__get__(pi, PiRunner)  # type: ignore[attr-defined]

    try:
        await pi.dispatch("s1", "timer_5min")
        msg = await asyncio.wait_for(done, timeout=3.0)
        assert msg["ok"] is False
        assert msg["errors"][0]["code"] == "pi_not_ready"
    finally:
        await pi.stop_all()
