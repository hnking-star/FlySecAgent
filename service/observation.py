"""两书签窗口推进的事务原语。

对应 docs/details/03-窗口推进.md。本模块只碰 projects / tool_records / observations 三张表，
不解析 state_json 的内容（合并逻辑在 Task 5 的 blackboard.py）。

四个事务原语 + 一个崩溃恢复：
- start_observation : 触发时固定窗口上界并建 running 行；已有 running 则合并
- commit_publish    : 发布成功；推进 processed、清 pending、设 current
- commit_unchanged  : 无变化；推进 processed、清 pending，不动 current
- mark_failed       : 失败；只记 error，书签全不动
- recover_pending   : 启动时按 observations 实际状态对账 pending
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from . import db


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def start_observation(
    conn: sqlite3.Connection, session_id: str, trigger: str
) -> int:
    """触发一轮观察。固定窗口上界 + 新建 running 行；已有 running 则返回其 id。

    并发触发（5min 和 Stop 同时命中）会合并到同一个 running 行。
    """
    with db.transaction(conn):
        existing = conn.execute(
            "SELECT id FROM observations "
            "WHERE session_id = ? AND status = 'running' "
            "ORDER BY id DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if existing is not None:
            return int(existing["id"])

        row = conn.execute(
            "SELECT processed_record_id, pending_window_end, current_observation_id "
            "FROM projects WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"session {session_id!r} not found in projects")

        processed = int(row["processed_record_id"])
        pending = row["pending_window_end"]
        base_obs = row["current_observation_id"]

        if pending is None:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(id), ?) AS end FROM tool_records "
                "WHERE session_id = ?",
                (processed, session_id),
            ).fetchone()
            end = int(max_row["end"])
            conn.execute(
                "UPDATE projects SET pending_window_end = ? WHERE session_id = ?",
                (end, session_id),
            )
        else:
            end = int(pending)

        now = _now_iso()
        cursor = conn.execute(
            "INSERT INTO observations "
            "(session_id, trigger, status, start_record_id, end_record_id, "
            " base_observation_id, state_json, map_text, error, "
            " tool_logs_json, started_at, finished_at) "
            "VALUES (?, ?, 'running', ?, ?, ?, NULL, NULL, NULL, '[]', ?, NULL)",
            (session_id, trigger, processed, end, base_obs, now),
        )
        return int(cursor.lastrowid)


def commit_publish(
    conn: sqlite3.Connection,
    session_id: str,
    obs_id: int,
    state_json: str,
    map_text: str,
) -> None:
    """发布成功：observations.published + 推进 processed + 清 pending + 设 current。

    state_json 由上层生成（Task 5 的合并逻辑），本函数不解析其内容。
    顺序必须是先 UPDATE observations，再 UPDATE projects，否则 projects 指针会短暂指到 running 行。
    """
    with db.transaction(conn):
        pending_row = conn.execute(
            "SELECT pending_window_end FROM projects WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if pending_row is None:
            raise ValueError(f"session {session_id!r} not found in projects")
        if pending_row["pending_window_end"] is None:
            raise RuntimeError(
                f"commit_publish on session {session_id!r} but pending_window_end is NULL"
            )

        obs_row = conn.execute(
            "SELECT id, session_id, status,trigger FROM observations WHERE id = ?",
            (obs_id,),
        ).fetchone()
        if obs_row is None:
            raise ValueError(f"observation {obs_id} not found")
        if obs_row["session_id"] != session_id:
            raise RuntimeError(
                f"observation {obs_id} belongs to a different session"
            )

        if obs_row["status"] != "running":
            raise RuntimeError("only a running observation can publish")
        if obs_row["trigger"] == "observation_close":
            conn.execute("UPDATE projects SET final_summary_requested=0 WHERE session_id=?", (session_id,))
        now = _now_iso()
        conn.execute(
            "UPDATE observations "
            "SET status = 'published', state_json = ?, map_text = ?, finished_at = ? "
            "WHERE id = ?",
            (state_json, map_text, now, obs_id),
        )
        conn.execute(
            "UPDATE projects "
            "SET processed_record_id    = pending_window_end, "
            "    pending_window_end     = NULL, "
            "    current_observation_id = ? "
            "WHERE session_id = ?",
            (obs_id, session_id),
        )


def commit_unchanged(
    conn: sqlite3.Connection, session_id: str, obs_id: int
) -> None:
    """无变化：observations.unchanged + 推进 processed + 清 pending。

    不更新 current_observation_id；map_text / state_json 保持 NULL。
    """
    with db.transaction(conn):
        pending_row = conn.execute(
            "SELECT pending_window_end FROM projects WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if pending_row is None:
            raise ValueError(f"session {session_id!r} not found in projects")
        if pending_row["pending_window_end"] is None:
            raise RuntimeError(
                f"commit_unchanged on session {session_id!r} but pending_window_end is NULL"
            )

        obs_row = conn.execute(
            "SELECT id, session_id, status,trigger FROM observations WHERE id = ?", (obs_id,)
        ).fetchone()
        if obs_row is None:
            raise ValueError(f"observation {obs_id} not found")
        if obs_row["session_id"] != session_id:
            raise RuntimeError(
                f"observation {obs_id} belongs to a different session"
            )

        if obs_row["status"] != "running":
            raise RuntimeError("only a running observation can complete")
        if obs_row["trigger"] == "observation_close":
            conn.execute("UPDATE projects SET final_summary_requested=0 WHERE session_id=?", (session_id,))
        now = _now_iso()
        conn.execute(
            "UPDATE observations SET status = 'unchanged', finished_at = ? WHERE id = ?",
            (now, obs_id),
        )
        conn.execute(
            "UPDATE projects "
            "SET processed_record_id = pending_window_end, "
            "    pending_window_end  = NULL "
            "WHERE session_id = ?",
            (session_id,),
        )


def mark_failed(
    conn: sqlite3.Connection, session_id: str, obs_id: int, error: str
) -> None:
    """失败：只记 error，书签完全不动，pending_window_end 保留给下轮复用。"""
    obs_row = conn.execute(
        "SELECT id, session_id, status,trigger FROM observations WHERE id = ?", (obs_id,)
    ).fetchone()
    if obs_row is None:
        raise ValueError(f"observation {obs_id} not found")
    if obs_row["session_id"] != session_id:
        raise RuntimeError(
            f"observation {obs_id} belongs to a different session"
        )
    conn.execute(
        "UPDATE observations SET status = 'failed', error = ?, finished_at = ? WHERE id = ?",
        (error, _now_iso(), obs_id),
    )


def recover_pending(conn: sqlite3.Connection, session_id: str) -> None:
    """启动时按 observations 实际状态对账 pending_window_end。

    - published 行存在 → 补推进 + 更新 current_observation_id
    - unchanged 行存在 → 仅补推进（current 不变）
    - running / failed / 不存在 → 保留 pending，下轮复用窗口
    """
    row = conn.execute(
        "SELECT pending_window_end FROM projects WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    if row is None or row["pending_window_end"] is None:
        return
    pending_end = int(row["pending_window_end"])

    obs = conn.execute(
        "SELECT id, status FROM observations "
        "WHERE session_id = ? AND end_record_id = ? "
        "ORDER BY id DESC LIMIT 1",
        (session_id, pending_end),
    ).fetchone()

    if obs is None or obs["status"] in ("running", "failed"):
        return  # 保留 pending

    with db.transaction(conn):
        if obs["status"] == "published":
            conn.execute(
                "UPDATE projects "
                "SET processed_record_id    = pending_window_end, "
                "    pending_window_end     = NULL, "
                "    current_observation_id = ? "
                "WHERE session_id = ?",
                (int(obs["id"]), session_id),
            )
        elif obs["status"] == "unchanged":
            conn.execute(
                "UPDATE projects "
                "SET processed_record_id = pending_window_end, "
                "    pending_window_end  = NULL "
                "WHERE session_id = ?",
                (session_id,),
            )
