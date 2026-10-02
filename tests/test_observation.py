"""两书签窗口推进的事务原语单元测试。

覆盖：publish / unchanged / failed / 并发合并 / 崩溃恢复三分支 / 空窗口。
全部用 sqlite3 直接查断言，不走 HTTP。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from service import db, observation


SESSION = "test-session-1"


@pytest.fixture()
def conn(tmp_path: Path):
    db.init_db(tmp_path)
    c = db.connect(tmp_path)
    _insert_project(c, SESSION)
    try:
        yield c
    finally:
        c.close()


def _insert_project(c: sqlite3.Connection, session_id: str) -> None:
    c.execute(
        "INSERT INTO projects (session_id, target, objective, created_at) "
        "VALUES (?, 'example.com', '测试目的', '2026-10-02T00:00:00Z')",
        (session_id,),
    )


def _insert_tool_record(c: sqlite3.Connection, session_id: str) -> int:
    cur = c.execute(
        "INSERT INTO tool_records "
        "(session_id, call_key, tool_name, tool_input_json, tool_response_json, "
        " metadata_json, received_at) "
        "VALUES (?, NULL, 'bash', '{}', '{}', '{}', '2026-10-02T00:00:00Z')",
        (session_id,),
    )
    return int(cur.lastrowid)


def _project(c: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    return c.execute(
        "SELECT * FROM projects WHERE session_id = ?", (session_id,)
    ).fetchone()


def _observation(c: sqlite3.Connection, obs_id: int) -> sqlite3.Row:
    return c.execute(
        "SELECT * FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()


# ---------------------------------------------------------------------------
# §1 窗口固定
# ---------------------------------------------------------------------------


def test_start_fixes_window(conn):
    for _ in range(5):
        _insert_tool_record(conn, SESSION)

    obs_id = observation.start_observation(conn, SESSION, "timer_5min")

    proj = _project(conn, SESSION)
    assert proj["pending_window_end"] == 5
    assert proj["processed_record_id"] == 0

    obs = _observation(conn, obs_id)
    assert obs["status"] == "running"
    assert obs["start_record_id"] == 0
    assert obs["end_record_id"] == 5


def test_start_empty_tool_records(conn):
    """空库触发：窗口 (0, 0]，仍新建 running 行。"""
    obs_id = observation.start_observation(conn, SESSION, "timer_5min")

    proj = _project(conn, SESSION)
    assert proj["pending_window_end"] == 0
    assert proj["processed_record_id"] == 0

    obs = _observation(conn, obs_id)
    assert obs["status"] == "running"
    assert obs["start_record_id"] == 0
    assert obs["end_record_id"] == 0


# ---------------------------------------------------------------------------
# §2 发布 / 无变化
# ---------------------------------------------------------------------------


def test_publish_advances(conn):
    for _ in range(5):
        _insert_tool_record(conn, SESSION)
    obs_id = observation.start_observation(conn, SESSION, "timer_5min")

    observation.commit_publish(conn, SESSION, obs_id, '{"revision": "v1"}', "<map/>")

    proj = _project(conn, SESSION)
    assert proj["processed_record_id"] == 5
    assert proj["pending_window_end"] is None
    assert proj["current_observation_id"] == obs_id

    obs = _observation(conn, obs_id)
    assert obs["status"] == "published"
    assert obs["state_json"] == '{"revision": "v1"}'
    assert obs["map_text"] == "<map/>"
    assert obs["finished_at"] is not None


def test_unchanged_advances_but_no_version(conn):
    for _ in range(3):
        _insert_tool_record(conn, SESSION)
    obs_id = observation.start_observation(conn, SESSION, "timer_5min")

    observation.commit_unchanged(conn, SESSION, obs_id)

    proj = _project(conn, SESSION)
    assert proj["processed_record_id"] == 3
    assert proj["pending_window_end"] is None
    assert proj["current_observation_id"] is None

    obs = _observation(conn, obs_id)
    assert obs["status"] == "unchanged"
    assert obs["state_json"] is None
    assert obs["map_text"] is None


# ---------------------------------------------------------------------------
# §3 失败保留窗口
# ---------------------------------------------------------------------------


def test_failed_keeps_pending(conn):
    for _ in range(5):
        _insert_tool_record(conn, SESSION)
    obs_id = observation.start_observation(conn, SESSION, "timer_5min")

    observation.mark_failed(conn, SESSION, obs_id, "schema_invalid")

    proj = _project(conn, SESSION)
    assert proj["pending_window_end"] == 5
    assert proj["processed_record_id"] == 0

    obs = _observation(conn, obs_id)
    assert obs["status"] == "failed"
    assert obs["error"] == "schema_invalid"

    # 期间入库 id=6，不应被下轮窗口吸收
    _insert_tool_record(conn, SESSION)
    assert conn.execute(
        "SELECT MAX(id) FROM tool_records WHERE session_id = ?", (SESSION,)
    ).fetchone()[0] == 6

    # 第二轮触发：原 running 已变 failed，会新建 running 行，但窗口上界复用原 pending=5
    obs_id_2 = observation.start_observation(conn, SESSION, "timer_5min")
    assert obs_id_2 != obs_id
    obs_2 = _observation(conn, obs_id_2)
    assert obs_2["status"] == "running"
    assert obs_2["end_record_id"] == 5  # 不扩进 id=6
    assert obs_2["start_record_id"] == 0

    proj_after = _project(conn, SESSION)
    assert proj_after["pending_window_end"] == 5


def test_running_merges_within_same_round(conn):
    """已有 running 行时，再次 start 应合并到同一 obs（并发触发）。"""
    for _ in range(3):
        _insert_tool_record(conn, SESSION)
    obs_id_1 = observation.start_observation(conn, SESSION, "timer_5min")
    obs_id_2 = observation.start_observation(conn, SESSION, "agent_stop")
    assert obs_id_1 == obs_id_2



# ---------------------------------------------------------------------------
# §4 并发合并
# ---------------------------------------------------------------------------


def test_concurrent_start_merges(conn):
    for _ in range(3):
        _insert_tool_record(conn, SESSION)

    obs_id_1 = observation.start_observation(conn, SESSION, "timer_5min")
    obs_id_2 = observation.start_observation(conn, SESSION, "agent_stop")

    assert obs_id_1 == obs_id_2  # 合并到同一行
    rows = conn.execute(
        "SELECT COUNT(*) FROM observations WHERE session_id = ? AND status = 'running'",
        (SESSION,),
    ).fetchone()[0]
    assert rows == 1


# ---------------------------------------------------------------------------
# §5 崩溃恢复
# ---------------------------------------------------------------------------


def _simulate_crash_before_recover(c: sqlite3.Connection, status: str) -> int:
    """手动制造：pending_window_end 已固定但 projects 书签还没推进。"""
    for _ in range(4):
        _insert_tool_record(c, SESSION)
    obs_id = observation.start_observation(c, SESSION, "timer_5min")
    # 用底层 UPDATE 模拟"事务做了一半崩溃"：observations 行已置目标状态，
    # 但 projects.pending_window_end 没清、processed 没推进。
    c.execute(
        "UPDATE observations SET status = ?, finished_at = '2026-10-02T00:00:00Z' WHERE id = ?",
        (status, obs_id),
    )
    if status == "published":
        c.execute(
            "UPDATE observations SET state_json = '{}', map_text = '<map/>' WHERE id = ?",
            (obs_id,),
        )
    return obs_id


def test_recover_published(conn):
    obs_id = _simulate_crash_before_recover(conn, "published")

    observation.recover_pending(conn, SESSION)

    proj = _project(conn, SESSION)
    assert proj["processed_record_id"] == 4
    assert proj["pending_window_end"] is None
    assert proj["current_observation_id"] == obs_id


def test_recover_unchanged(conn):
    _simulate_crash_before_recover(conn, "unchanged")

    observation.recover_pending(conn, SESSION)

    proj = _project(conn, SESSION)
    assert proj["processed_record_id"] == 4
    assert proj["pending_window_end"] is None
    assert proj["current_observation_id"] is None


def test_recover_running_keeps_pending(conn):
    _simulate_crash_before_recover(conn, "running")

    observation.recover_pending(conn, SESSION)

    proj = _project(conn, SESSION)
    assert proj["pending_window_end"] == 4
    assert proj["processed_record_id"] == 0
    assert proj["current_observation_id"] is None


def test_recover_failed_keeps_pending(conn):
    _simulate_crash_before_recover(conn, "failed")

    observation.recover_pending(conn, SESSION)

    proj = _project(conn, SESSION)
    assert proj["pending_window_end"] == 4
    assert proj["processed_record_id"] == 0
