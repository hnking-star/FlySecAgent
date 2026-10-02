"""Scheduler 状态机单元测试：全程 mock Pi dispatcher。"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from service.scheduler import Scheduler


class _ClockDouble:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, delta: float) -> None:
        self.now += delta


class _MockDispatcher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.block_event: asyncio.Event | None = None

    async def __call__(self, session_id: str, trigger: str) -> None:
        self.calls.append((session_id, trigger))
        if self.block_event is not None:
            await self.block_event.wait()


async def _make_scheduler(
    *,
    interval: float = 300.0,
    tick: float = 0.01,
) -> tuple[Scheduler, _ClockDouble, _MockDispatcher]:
    clock = _ClockDouble()
    dispatcher = _MockDispatcher()
    sched = Scheduler(
        dispatcher=dispatcher,
        timer_interval=interval,
        tick_seconds=tick,
        clock=clock,
    )
    await sched.start()
    return sched, clock, dispatcher


async def _flush_ticks(seconds: float = 0.05) -> None:
    await asyncio.sleep(seconds)


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_turn_begin_arms_timer():
    sched, clock, dispatcher = await _make_scheduler(interval=300.0)
    try:
        await sched.agent_turn_begin("s1")
        state = sched.snapshot("s1")
        assert state.agent_turn_active is True
        assert state.next_timer_fire_at == pytest.approx(1000.0 + 300.0)
        assert dispatcher.calls == []
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_timer_fires_when_due():
    sched, clock, dispatcher = await _make_scheduler(interval=5.0)
    try:
        await sched.agent_turn_begin("s1")
        clock.advance(6.0)
        await _flush_ticks(0.1)
        assert dispatcher.calls == [("s1", "timer_5min")]
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_timer_queues_while_pi_running():
    sched, clock, dispatcher = await _make_scheduler(interval=5.0)
    dispatcher.block_event = asyncio.Event()
    try:
        await sched.agent_turn_begin("s1")
        clock.advance(6.0)
        await _flush_ticks(0.1)
        # 第一发已经下发；running=True 卡在 block_event
        assert dispatcher.calls == [("s1", "timer_5min")]

        # 再推进 5 秒，timer 又到期但 Pi 还在跑
        clock.advance(6.0)
        await _flush_ticks(0.1)
        assert len(dispatcher.calls) == 1  # 仍只有一次

        # Pi 完成
        dispatcher.block_event.set()
        await asyncio.sleep(0)
        await sched.pi_run_done("s1")
        await _flush_ticks(0.05)
        # pending 应被取出并下发
        assert len(dispatcher.calls) == 2
        assert dispatcher.calls[1] == ("s1", "timer_5min")
    finally:
        if dispatcher.block_event:
            dispatcher.block_event.set()
        await sched.stop()


@pytest.mark.asyncio
async def test_agent_turn_stop_fires_immediately():
    sched, clock, dispatcher = await _make_scheduler(interval=300.0)
    try:
        await sched.agent_turn_begin("s1")
        await sched.agent_turn_stop("s1")
        await _flush_ticks(0.02)
        assert dispatcher.calls == [("s1", "agent_stop")]
        state = sched.snapshot("s1")
        assert state.next_timer_fire_at is None
        assert state.agent_turn_active is False
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_begin_after_stop_resumes_timer():
    sched, clock, dispatcher = await _make_scheduler(interval=10.0)
    try:
        await sched.agent_turn_begin("s1")
        await sched.agent_turn_stop("s1")
        await _flush_ticks(0.02)
        await sched.pi_run_done("s1")

        clock.advance(100.0)  # 空闲 100 秒
        await _flush_ticks(0.05)
        # 空闲期间不应再触发
        assert len(dispatcher.calls) == 1

        await sched.agent_turn_begin("s1")
        state = sched.snapshot("s1")
        assert state.next_timer_fire_at == pytest.approx(clock.now + 10.0)
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_paused_cancels_pending():
    sched, clock, dispatcher = await _make_scheduler(interval=5.0)
    try:
        await sched.agent_turn_begin("s1")
        clock.advance(6.0)
        # 还没来得及 tick 下发前先暂停
        await sched.observer_paused("s1")
        await _flush_ticks(0.1)
        assert dispatcher.calls == []  # pause 清掉了 pending
        state = sched.snapshot("s1")
        assert state.next_timer_fire_at is None
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_observation_closed_fires_close_trigger():
    sched, clock, dispatcher = await _make_scheduler(interval=300.0)
    try:
        await sched.agent_turn_begin("s1")
        await sched.observation_closed("s1")
        await _flush_ticks(0.02)
        assert dispatcher.calls == [("s1", "observation_close")]
        state = sched.snapshot("s1")
        assert state.observation_enabled is False
    finally:
        await sched.stop()


@pytest.mark.asyncio
async def test_dispatcher_failure_restores_pending():
    """dispatcher 抛错时 pending 应保留，running 应回退，等下次重试。"""
    class _FailingDispatcher:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []
            self.should_fail = True

        async def __call__(self, session_id: str, trigger: str) -> None:
            self.calls.append((session_id, trigger))
            if self.should_fail:
                raise RuntimeError("boom")

    clock = _ClockDouble()
    bad = _FailingDispatcher()
    sched = Scheduler(dispatcher=bad, timer_interval=300.0, tick_seconds=0.01, clock=clock)
    await sched.start()
    try:
        await sched.agent_turn_stop("s1")
        await _flush_ticks(0.05)
        state = sched.snapshot("s1")
        assert state.pending_trigger == "agent_stop"
        assert state.running is False
        bad.should_fail = False
        await sched.pi_run_done("s1")  # 走一次 _maybe_dispatch
        await _flush_ticks(0.05)
        assert bad.calls[-1][1] == "agent_stop"
    finally:
        await sched.stop()
