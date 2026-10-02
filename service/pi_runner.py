"""Pi 子进程生命周期 + stdin/stdout JSONL RPC。

每个 session_id 一个子进程，内部用 asyncio.subprocess。
崩溃自动重启（指数退避），超过 5 次放弃。

默认启动真 Pi 扩展（pi_ext/dist/index.js）；若 dist 不存在则回退到
service.pi_stub 占位实现，便于 CI / 无 Node 环境运行。可通过
环境变量 FLYSEC_PI_COMMAND 覆盖（空格分隔）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from .auth import TokenRegistry

_logger = logging.getLogger("flysec.pi")
_pi_log = logging.getLogger("flysec.pi.stdio")

MAX_RESTARTS = 5
SHUTDOWN_GRACE_S = 2.0
SIGTERM_GRACE_S = 1.0


def _default_pi_command() -> list[str]:
    override = os.environ.get("FLYSEC_PI_COMMAND")
    if override:
        return shlex.split(override)
    entry = Path(__file__).resolve().parent.parent / "pi_ext" / "dist" / "index.js"
    if entry.exists():
        return ["node", str(entry)]
    raise RuntimeError("Pi extension is not built; run npm run build in pi_ext, or explicitly select the test stub")


# 回调签名：PiRunner → Scheduler
OnRunDone = Callable[[str, dict], Awaitable[None]]  # session_id, msg


@dataclass
class _PiHandle:
    session_id: str
    token: str
    proc: asyncio.subprocess.Process
    writer_closed: bool = False


class PiRunner:
    def __init__(
        self,
        *,
        api_base: str,
        token_registry: TokenRegistry,
        on_run_done: OnRunDone,
        on_run_started: Callable[[str, dict], Awaitable[None]] | None = None,
        command: list[str] | None = None,
        on_ready: Callable[[str, dict], Awaitable[None]] | None = None,
    ) -> None:
        self._api_base = api_base
        self._registry = token_registry
        self._on_run_done = on_run_done
        self._on_run_started = on_run_started
        self._on_ready = on_ready
        self._command = command if command is not None else _default_pi_command()
        self._handles: dict[str, _PiHandle] = {}
        self._supervisors: dict[str, asyncio.Task] = {}
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self._ready_evts: dict[str, asyncio.Event] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def ensure_running(self, session_id: str) -> None:
        """为该 session 启一个 Pi 子进程（若未启）；supervisor 会自动重启。"""
        async with self._lock:
            current = self._supervisors.get(session_id)
            if current is not None and not current.done():
                return
            self._ready_evts[session_id] = asyncio.Event()
            task = asyncio.create_task(
                self._supervise(session_id), name=f"flysec-pi-{session_id}"
            )
            self._supervisors[session_id] = task

    async def stop_session(self, session_id: str) -> None:
        """停止该 session 的 Pi 子进程并取消 supervisor。"""
        async with self._lock:
            supervisor = self._supervisors.pop(session_id, None)
            handle = self._handles.pop(session_id, None)
            self._ready_evts.pop(session_id, None)
        if handle is not None:
            await self._shutdown_handle(handle)
        if supervisor is not None:
            supervisor.cancel()
            try:
                await supervisor
            except (asyncio.CancelledError, Exception):
                pass
        self._registry.revoke(session_id)

    async def stop_all(self) -> None:
        self._stop.set()
        sessions = list(self._supervisors.keys())
        for sid in sessions:
            await self.stop_session(sid)

    # ------------------------------------------------------------------
    # 对外下发（Scheduler 调）
    # ------------------------------------------------------------------

    async def dispatch(self, session_id: str, trigger: str) -> None:
        """给对应 session 的 Pi 子进程发 run_observation。

        若子进程还没 ready，等待最多 10s；超时直接报 failed 回调。
        """
        await self.ensure_running(session_id)
        ready = self._ready_evts.get(session_id)
        if ready is not None:
            try:
                await asyncio.wait_for(ready.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                _logger.warning("session=%s pi did not become ready in 10s", session_id)
                await self._on_run_done(session_id, {"op": "run_done", "ok": False,
                                                     "errors": [{"path": "/", "code": "pi_not_ready",
                                                                 "message": "pi process never signaled ready"}]})
                return

        handle = self._handles.get(session_id)
        if handle is None:
            await self._on_run_done(session_id, {"op": "run_done", "ok": False,
                                                 "errors": [{"path": "/", "code": "pi_unavailable",
                                                             "message": "pi handle missing"}]})
            return

        payload = json.dumps({"op": "run_observation", "trigger": trigger}) + "\n"
        try:
            handle.proc.stdin.write(payload.encode("utf-8"))
            await handle.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            _logger.warning("session=%s stdin write failed: %s", session_id, exc)
            await self._on_run_done(session_id, {"op": "run_done", "ok": False,
                                                 "errors": [{"path": "/", "code": "pi_stdin_broken",
                                                             "message": str(exc)}]})

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _supervise(self, session_id: str) -> None:
        restart_count = 0
        while not self._stop.is_set():
            try:
                handle = await self._spawn(session_id)
            except Exception as exc:  # noqa: BLE001
                _logger.exception("session=%s pi spawn failed: %s", session_id, exc)
                return
            self._handles[session_id] = handle

            reader_task = asyncio.create_task(
                self._read_stdout(session_id, handle), name=f"flysec-pi-stdout-{session_id}"
            )
            err_task = asyncio.create_task(
                self._read_stderr(session_id, handle), name=f"flysec-pi-stderr-{session_id}"
            )

            try:
                rc = await handle.proc.wait()
                await asyncio.gather(reader_task, err_task, return_exceptions=True)
            except asyncio.CancelledError:
                await self._shutdown_handle(handle)
                reader_task.cancel()
                err_task.cancel()
                await asyncio.gather(reader_task, err_task, return_exceptions=True)
                raise
            finally:
                self._registry.revoke(session_id)
            self._handles.pop(session_id, None)
            ready = self._ready_evts.get(session_id)
            if ready is not None:
                ready.clear()

            if self._stop.is_set():
                return
            if rc == 0:
                _logger.info("session=%s pi exited cleanly", session_id)
                return
            restart_count += 1
            if restart_count > MAX_RESTARTS:
                _logger.error(
                    "session=%s pi exited %d times (rc=%s); giving up",
                    session_id, restart_count, rc,
                )
                return
            delay = min(2 ** restart_count, 30)
            _logger.warning(
                "session=%s pi rc=%s; restart in %ds (attempt %d/%d)",
                session_id, rc, delay, restart_count, MAX_RESTARTS,
            )
            await asyncio.sleep(delay)

    async def _spawn(self, session_id: str) -> _PiHandle:
        token = self._registry.issue(session_id)
        env = {
            **os.environ,
            "FLYSEC_TOKEN": token,
            "FLYSEC_API": self._api_base,
            "FLYSEC_SESSION_ID": session_id,
        }
        proc = await asyncio.create_subprocess_exec(
            *self._command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _logger.info("session=%s spawned pi pid=%s", session_id, proc.pid)
        return _PiHandle(session_id=session_id, token=token, proc=proc)

    async def _read_stdout(self, session_id: str, handle: _PiHandle) -> None:
        stdout = handle.proc.stdout
        assert stdout is not None
        while True:
            line = await stdout.readline()
            if not line:
                return
            try:
                msg = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                _pi_log.warning("session=%s invalid RPC output", session_id)

                continue
            op = msg.get("op")
            if op == "ready":
                ready = self._ready_evts.get(session_id)
                if ready is not None:
                    ready.set()
                if self._on_ready is not None:
                    await self._on_ready(session_id, msg)
                continue
            if op == "run_started" and self._on_run_started is not None:
                await self._on_run_started(session_id, msg)
                continue
            if op == "run_done":
                print(f"[pi:{session_id}] run_done: {msg}", flush=True)
                await self._on_run_done(session_id, msg)
                continue
            if op == "log":
                _pi_log.info("session=%s pi: %s", session_id, msg.get("message"))
                continue

    async def _read_stderr(self, session_id: str, handle: _PiHandle) -> None:
        stderr = handle.proc.stderr
        assert stderr is not None
        while True:
            line = await stderr.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            _pi_log.warning("session=%s pi stderr: %s", session_id, text)


    async def _shutdown_handle(self, handle: _PiHandle) -> None:
        if handle.proc.returncode is not None:
            return
        # 1) 发 shutdown 消息
        try:
            handle.proc.stdin.write(b'{"op":"shutdown"}\n')
            await handle.proc.stdin.drain()
            handle.proc.stdin.close()
        except (BrokenPipeError, ConnectionResetError, RuntimeError):
            pass
        # 2) 等 SHUTDOWN_GRACE_S
        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=SHUTDOWN_GRACE_S)
            return
        except asyncio.TimeoutError:
            pass
        # 3) SIGTERM
        try:
            handle.proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=SIGTERM_GRACE_S)
            return
        except asyncio.TimeoutError:
            pass
        # 4) SIGKILL
        try:
            handle.proc.kill()
        except ProcessLookupError:
            return
        await handle.proc.wait()
