"""Shared explicit enrollment rules. Chat is parsed transiently, never persisted."""
from __future__ import annotations
import re
import sqlite3
from pathlib import Path

_START_RE = re.compile(
    r"^(?:请)?(?:开始(?:进行)?(?:授权)?(?:安全|渗透)?测试\s*[:：]?\s*|开始对\s*)"
    r"(?P<target>https?://[^\s，。；;]+|(?:[A-Za-z0-9_-]+\.)+[A-Za-z]{2,}(?::\d+)?(?:/[^\s，。；;]*)?)"
    r"(?P<tail>.*)$", re.IGNORECASE | re.DOTALL,
)


def parse_start_prompt(prompt: object) -> tuple[str, str] | None:
    """Recognize an explicit chat opt-in without persisting the prompt text."""
    if not isinstance(prompt, str):
        return None
    match = _START_RE.search(prompt.strip())
    if not match:
        return None
    target = match.group("target").rstrip(",.;:!?，。；：！？)]】")
    tail = match.group("tail").strip(" \t\n,.;:!?，。；：！？")
    tail = re.sub(r"^(?:进行)?(?:安全|渗透)?测试\s*[,，。:：]?\s*", "", tail)
    objective = tail[:2000] if tail else "记录授权测试过程，梳理攻击面、API 与测试结果"
    return target, objective


def session_enabled(data_dir: Path, sid: str) -> bool:
    """Local read works while HTTP is down; an unknown session is not enrolled."""
    path = data_dir / "flysec.db"
    if not path.exists():
        return False
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0.1)
    try:
        row = conn.execute("SELECT observation_enabled FROM projects WHERE session_id=?", (sid,)).fetchone()
        return bool(row and row[0])
    finally:
        conn.close()


