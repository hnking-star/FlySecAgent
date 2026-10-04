"""PostToolUse Hook：工具调用入库 + 交付未交付的新 map。"""

from __future__ import annotations

import sys
from typing import Any

from .common import (
    deliver_pending_map,
    enqueue_ingest,
    event_session_id,
    flush_queue,
    http_post,
    load_config,
    read_event,
)


HOOK_EVENT = "PostToolUse"


def _pick(event: dict, keys: list[str]) -> Any:
    for key in keys:
        if key in event and event[key] is not None:
            return event[key]
    return None


def _build_payload(event: dict, session_id: str) -> dict | None:
    tool_name = _pick(event, ["tool_name", "toolName"])
    if not isinstance(tool_name, str) or not tool_name:
        return None
    call_key = _pick(event, ["tool_use_id", "toolUseId", "call_id"])
    tool_input = _pick(event, ["tool_input", "toolInput", "input"])
    tool_response = _pick(event, ["tool_response", "toolResponse", "output", "result"])
    metadata = {
        "turn_id": _pick(event, ["turn_id", "turnId"]),
        "cwd": _pick(event, ["cwd", "working_directory"]),
        "received_at": _pick(event, ["timestamp", "ts"]),
    }
    metadata = {k: v for k, v in metadata.items() if v is not None}

    payload: dict = {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_response": tool_response,
        "metadata": metadata,
    }
    if isinstance(call_key, str) and call_key:
        payload["call_key"] = call_key
    return payload


def main() -> int:
    cfg = load_config()
    if cfg is None:
        return 0
    event = read_event()
    flush_queue(cfg)

    sid = event_session_id(event)
    if not sid:
        return 0

    payload = _build_payload(event, sid)
    if payload is None:
        return 0

    status, body = http_post(cfg, "/hook/record.ingest", payload)
    # 2xx 或 409 session_not_ready 都不排队
    if not (200 <= status < 300):
        code = (body or {}).get("code")
        if status == 0 or code not in {"session_not_ready"}:
            enqueue_ingest(cfg, payload)

    deliver_pending_map(cfg, HOOK_EVENT, sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
