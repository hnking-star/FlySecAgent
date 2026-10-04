"""SQLite 连接工厂、首启建表、显式事务上下文。

本文件只负责"开连接 / 建表 / 包事务"。所有业务 SQL 由上层模块写。

重要约定：
- `sqlite3` 的 `with conn: ...` 在 isolation_level=None 下不会自动 commit/close，
  所以本项目所有 connect 调用都走 `try/finally close()`，不要用 `with connect(...)`。
- 事务用显式的 `with transaction(conn): ...` 包，内部走 BEGIN IMMEDIATE / COMMIT / ROLLBACK。
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

DB_FILENAME = "flysec.db"


def db_path(data_dir: Path) -> Path:
    return data_dir / DB_FILENAME


def connect(data_dir: Path) -> sqlite3.Connection:
    """开一个新连接。调用方负责显式 close()。"""
    conn = sqlite3.connect(
        db_path(data_dir),
        isolation_level=None,  # autocommit；事务由 transaction() 显式管
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(data_dir: Path) -> None:
    """首启建表 + PRAGMA。幂等。"""
    sql = (Path(__file__).parent / "schema.sql").read_text()
    conn = connect(data_dir)
    try:
        conn.executescript(sql)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(projects)")}
        if "agent_activity_known" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN agent_activity_known INTEGER NOT NULL DEFAULT 0")
        if "final_summary_requested" not in columns:
            conn.execute("ALTER TABLE projects ADD COLUMN final_summary_requested INTEGER NOT NULL DEFAULT 0")
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """BEGIN IMMEDIATE / COMMIT / ROLLBACK 上下文。

    连接必须是 isolation_level=None 的 autocommit 模式。
    """
    nested = conn.in_transaction
    savepoint = "sp_" + uuid.uuid4().hex
    conn.execute(f"SAVEPOINT {savepoint}" if nested else "BEGIN IMMEDIATE;")
    try:
        yield
        conn.execute(f"RELEASE SAVEPOINT {savepoint}" if nested else "COMMIT;")
    except Exception:
        if nested:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        else:
            conn.execute("ROLLBACK;")
        raise
