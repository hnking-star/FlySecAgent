"""Memory Curator 调度器：每会话一个状态机，决定何时下发触发指令给 Pi 子进程。

- 5 分钟定时器仅在 agent_turn_active 期间计时
- Stop 事件立刻排一个 agent_stop 触发
- 并发触发合并（pending_trigger 覆盖）
- Pi 正跑时新 trigger 排队，Pi 回报 run_done 后立刻下发

本模块不启 Pi 子进程本身——通过 pi_dispatcher 回调把触发送给 PiRunner。
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

_logger = logging.getLogger("flysec.scheduler")

DEFAULT_INTERVAL = 300.0


def default_timer_interval() -> float:
    """从 env 读 FLYSEC_TIMER_INTERVAL（秒）。"""
    val = os.environ.get("FLYSEC_TIMER_INTERVAL")
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return DEFAULT_INTERVAL


@dataclass
class SessionState:
    session_id: str
    agent_turn_active: bool = False
    curator_paused: bool = False
    observation_enabled: bool = True
    pending_trigger: str | None = None
    next_timer_fire_at: float | None = None
    running: bool = False  # Pi 正在跑一轮


# 回调签名：Scheduler → PiRunner.dispatch(session_id, trigger)
PiDispatcher = Callable[[str, str], Awaitable[None]]


class Scheduler:
    def __init__(
        self,
        dispatcher: PiDispatcher,
        *,
        timer_interval: float | None = None,
        tick_seconds: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._dispatcher = dispatcher
        self._interval = timer_interval if timer_interval is not None else default_timer_interval()
        self._tick = tick_seconds
        self._clock = clock
        self._sessions: dict[str, SessionState] = {}
        self._lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task | None = None
        self._dispatch_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._tick_loop(), name="flysec-scheduler-tick")

    async def stop(self) -> None:
        self._stop.set()
        for task in self._dispatch_tasks:
            task.cancel()
        await asyncio.gather(*self._dispatch_tasks, return_exceptions=True)
        if self._loop_task is not None:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except (asyncio.CancelledError, Exception):
                pass
            self._loop_task = None

    # ------------------------------------------------------------------
    # 外部事件
    # ------------------------------------------------------------------

    async def agent_turn_begin(self, session_id: str) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            if not state.observation_enabled or state.curator_paused:
                return
            if state.agent_turn_active:
                return
            state.agent_turn_active = True
            state.next_timer_fire_at = self._clock() + self._interval
            _logger.info("session=%s agent_turn_begin; timer armed", session_id)

    async def agent_turn_stop(self, session_id: str) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            state.agent_turn_active = False
            state.next_timer_fire_at = None
            if state.observation_enabled and not state.curator_paused:
                state.pending_trigger = "agent_stop"
            _logger.info("session=%s agent_turn_stop", session_id)
        await self._maybe_dispatch(session_id)

    async def curator_paused(self, session_id: str) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            state.curator_paused = True
            state.pending_trigger = None
            state.next_timer_fire_at = None

    async def curator_resumed(self, session_id: str, *, agent_turn_active: bool | None = None) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            state.curator_paused = False
            if agent_turn_active is not None:
                state.agent_turn_active = agent_turn_active
            if state.agent_turn_active and state.observation_enabled:
                state.next_timer_fire_at = self._clock() + self._interval

    async def request_summary(self, session_id: str, trigger: str) -> None:
        async with self._lock:
            self._ensure(session_id).pending_trigger = trigger
        await self._maybe_dispatch(session_id)

    async def observation_opened(self, session_id: str, paused: bool) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            state.observation_enabled = True
            state.curator_paused = paused

    async def observation_closed(self, session_id: str) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            state.pending_trigger = "observation_close"
            state.observation_enabled = False
            state.next_timer_fire_at = None
            state.agent_turn_active = False
        await self._maybe_dispatch(session_id)

    async def pi_run_done(self, session_id: str) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            state.running = False
        await self._maybe_dispatch(session_id)

    async def pi_run_failed(self, session_id: str) -> None:
        async with self._lock:
            state = self._ensure(session_id)
            state.running = False
            # 失败不自动重触发；等下一次 timer 或 stop 事件
        _logger.warning("session=%s pi run failed", session_id)

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def snapshot(self, session_id: str) -> SessionState | None:
        state = self._sessions.get(session_id)
        if state is None:
            return None
        # 返回拷贝以便测试断言
        return SessionState(**state.__dict__)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _ensure(self, session_id: str) -> SessionState:
        state = self._sessions.get(session_id)
        if state is None:
            state = SessionState(session_id=session_id)
            self._sessions[session_id] = state
        return state

    async def _maybe_dispatch(self, session_id: str) -> None:
        """没有在跑且有 pending → 下发给 Pi。"""
        async with self._lock:
            if self._stop.is_set():
                return
            state = self._sessions.get(session_id)
            if state is None:
                return
            if state.running:
                return
            if state.curator_paused:
                return
            trigger = state.pending_trigger
            if trigger is None:
                return
            state.pending_trigger = None
            state.running = True
        task = asyncio.create_task(self._dispatch(session_id, trigger))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._dispatch_tasks.discard)
        await asyncio.sleep(0)

    async def _dispatch(self, session_id: str, trigger: str) -> None:
        try:
            await self._dispatcher(session_id, trigger)
        except Exception as exc:
            _logger.exception("session=%s dispatch failed: %s", session_id, exc)
            async with self._lock:
                st = self._sessions.get(session_id)
                if st is not None:
                    st.running = False
                    st.pending_trigger = trigger  # 还给调度器

    async def _tick_loop(self) -> None:
        try:
            while not self._stop.is_set():
                await self._tick_once()
                await asyncio.sleep(self._tick)
        except asyncio.CancelledError:
            return

    async def _tick_once(self) -> None:
        now = self._clock()
        due: list[str] = []
        async with self._lock:
            for sid, state in self._sessions.items():
                if (
                    state.agent_turn_active
                    and state.observation_enabled
                    and not state.curator_paused
                    and state.next_timer_fire_at is not None
                    and now >= state.next_timer_fire_at
                ):
                    state.pending_trigger = "timer_5min"
                    state.next_timer_fire_at = now + self._interval
                    due.append(sid)
        for sid in due:
            await self._maybe_dispatch(sid)
