"""Execution diagnostics derived from runtime receipts, not target HTTP statuses."""
from __future__ import annotations

import re


def execution_status(response, metadata: dict | None = None) -> str:
    metadata = metadata or {}
    if metadata.get("execution_status") in {"completed", "error", "denied", "interrupted", "unknown"}:
        return metadata["execution_status"]
    fields = response if isinstance(response, dict) else {}
    error = str(fields.get("error") or metadata.get("error") or "")
    if re.search(r"tool call rejected|rejected by user|approval denied", error, re.I):
        return "denied"
    if fields.get("interrupted") or re.search(r"Killed:\s*9", str(fields.get("stderr", ""))):
        return "interrupted"
    if fields.get("is_error") or metadata.get("is_error") or metadata.get("hook_event_name") == "PostToolUseFailure" or error:
        return "error"
    if not isinstance(response, dict):
        return "unknown"
    code = response.get("exit_code", response.get("exitCode"))
    if isinstance(code, int):
        return "completed" if code == 0 else "error"
    if re.search(r"SyntaxError:|TypeError:|IndentationError:|Traceback \(most recent", str(response.get("stderr", ""))):
        return "error"
    if "stdout" in response or "results" in response or metadata.get("hook_event_name") == "PostToolUse":
        return "completed"
    return "unknown"
