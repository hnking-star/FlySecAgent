"""Coco 0.121.x command hook. Identity comes from runtime stdin, never model args."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

from .common import (load_config, read_event, event_session_id, http_post,
                     flush_queue, enqueue_ingest, deliver_pending_map)
from .post_tool_use import _build_payload


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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--api-base")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    if args.api_base:
        os.environ["FLYSEC_API_BASE"] = args.api_base
    if args.data_dir:
        os.environ["FLYSEC_DATA_DIR"] = args.data_dir
    event = read_event()
    sid = event_session_id(event)
    cfg = load_config()
    if not sid or not cfg:
        return 0
    kind = event.get("hook_event_name", event.get("event_type", ""))
    kind = {"session_start": "SessionStart", "user_prompt_submit": "UserPromptSubmit",
            "post_tool_use": "PostToolUse", "post_tool_use_failure": "PostToolUseFailure",
            "stop": "Stop", "session_end": "SessionEnd", "interrupt": "Interrupt",
            "sessionStart": "SessionStart", "sessionEnd": "SessionEnd", "userPromptSubmit": "UserPromptSubmit",
            "postToolUse": "PostToolUse", "postToolUseFailure": "PostToolUseFailure"}.get(kind, kind)
    # A launch with explicit target/objective opts in.
    target, objective = os.environ.get("FLYSEC_TARGET"), os.environ.get("FLYSEC_OBJECTIVE")
    if kind == "SessionStart" and target and objective:
        http_post(cfg, "/hook/project.ensure", {"session_id": sid, "hint": {"target": target, "objective": objective}})

    # UserPromptSubmit may opt in with an explicit phrase. The prompt is parsed
    # transiently and is never written to metadata, the queue, or the database.
    activated = False
    if kind == "UserPromptSubmit" and not session_enabled(cfg.data_dir, sid):
        start = parse_start_prompt(event.get("prompt"))
        if start:
            target, objective = start
            status, body = http_post(
                cfg,
                "/hook/project.ensure",
                {"session_id": sid, "hint": {"target": target, "objective": objective}},
            )
            activated = status == 200 and bool(body and body.get("ok"))
    if not session_enabled(cfg.data_dir, sid):
        return 0

    # Persist metadata only after explicit enrollment. Never save chat contents.
    directory = cfg.data_dir / "hook-sessions"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / (hashlib.sha256(sid.encode()).hexdigest() + ".json")
    info = {"session_id": sid, "cwd": event.get("cwd"), "event": kind, "seen_at": time.time()}
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)

    flush_queue(cfg, max_items=10)
    if kind == "UserPromptSubmit":
        http_post(cfg, "/control/agent-turn/begin", {"session_id": sid})
        if activated:
            from .common import write_additional_context
            write_additional_context(
                kind,
                f"FlySecAgent 证据记忆已开启：target={target}。工具记录按宿主真实会话隔离。最终回答请分开说明结论、已完成工作、关键证据、限制和未完成事项，不把读取结果等同平台验收。",
            )
        else:
            deliver_pending_map(cfg, kind, sid)
    elif kind in {"PostToolUse", "PostToolUseFailure"}:
        # A real host tool event can restore liveness after a service restart.
        try:
            conn = sqlite3.connect((cfg.data_dir / "flysec.db").as_uri() + "?mode=ro", uri=True, timeout=.1)
            try: activity = conn.execute("SELECT agent_activity_known FROM projects WHERE session_id=?", (sid,)).fetchone()
            finally: conn.close()
            if activity and not activity[0]: http_post(cfg, "/control/agent-turn/begin", {"session_id": sid})
        except sqlite3.Error:
            pass  # Older service or unavailable state: never guess liveness from a late receipt.
        payload = _build_payload(event, sid)
        if payload:
            payload["metadata"].update({"hook_event_name": kind, "agent_id": event.get("agent_id"), "source": "host-hook"})
            if kind == "PostToolUseFailure":
                payload["metadata"].update({"is_error": True, "error": event.get("error")})
                if payload["tool_response"] is None:
                    payload["tool_response"] = {"error": event.get("error"), "is_error": True}
            for key in ("truncated", "output_truncated", "is_error"):
                if key in event:
                    payload["metadata"][key] = event[key]
            status, _ = http_post(cfg, "/hook/record.ingest", payload)
            if status == 0 or status >= 500:
                enqueue_ingest(cfg, payload)
        deliver_pending_map(cfg, kind, sid)
    elif kind in {"Stop", "SessionEnd", "Interrupt"}:
        # SessionEnd also covers print-mode errors that never emit Stop.
        http_post(cfg, "/control/agent-turn/stop", {"session_id": sid})
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[flysec-hook] {type(exc).__name__}: hook could not complete", file=sys.stderr)
        raise SystemExit(0)
