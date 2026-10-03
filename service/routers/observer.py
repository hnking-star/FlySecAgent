"""/observer/* 两个工具的路由。

模型通过 Pi 扩展间接调用。身份由 Task 3 的中间件从会话级 token 反查，
写入 request.state.session_id；body 不含身份字段（Task 3 已拦截）。
"""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from .. import blackboard, observation
from ..db import connect
from ..feedback import render_map
from ..schemas import ContextInput, SubmitInput

router = APIRouter(prefix="/observer", tags=["observer"])

PREVIEW_BYTES = 2048
SUBMIT_CACHE_TTL = 300  # 秒
PLACEHOLDER_MAP = "<observer-map/>"


# ---------------------------------------------------------------------------
# SubmitCache（进程内存，重启失效）
# ---------------------------------------------------------------------------


class SubmitCache:
    def __init__(self, ttl_seconds: int = SUBMIT_CACHE_TTL) -> None:
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    def get(self, session_id: str, h: str) -> dict[str, Any] | None:
        key = (session_id, h)
        row = self._store.get(key)
        if row is None:
            return None
        ts, body = row
        if time.time() - ts > self._ttl:
            self._store.pop(key, None)
            return None
        return body

    def clear_session(self, session_id: str) -> None:
        for key in list(self._store):
            if key[0] == session_id:
                self._store.pop(key, None)

    def put(self, session_id: str, h: str, response: dict[str, Any]) -> None:
        self._store[(session_id, h)] = (time.time(), response)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _raise(status: int, code: str, message: str, path: str | None = None) -> None:
    detail: dict[str, Any] = {"code": code, "message": message}
    if path:
        detail["path"] = path
    raise HTTPException(status_code=status, detail=detail)


def _conn(request: Request) -> sqlite3.Connection:
    return connect(request.app.state.cfg.data_dir)


def _require_project(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    proj = conn.execute(
        "SELECT * FROM projects WHERE session_id = ?", (session_id,)
    ).fetchone()
    if proj is None:
        _raise(404, "session_not_found", "no project for this session")
    return proj


def _load_state(conn: sqlite3.Connection, project: sqlite3.Row) -> dict:
    cur_obs = project["current_observation_id"]
    if cur_obs is None:
        return blackboard.initial_state()
    row = conn.execute(
        "SELECT state_json FROM observations WHERE id = ? AND session_id = ? AND status = 'published'", (int(cur_obs), project["session_id"])
    ).fetchone()
    if row is None or row["state_json"] is None:
        _raise(409, "invalid_blackboard", "current publication is unavailable")
    try:
        state = json.loads(row["state_json"])
        # apiIds 是 schema v1 的向后兼容扩展；旧快照按无关联 API 读取。
        for item in state.get("assessments", []):
            item.setdefault("apiIds", [])
        for item in state.get("retired", []):
            item.setdefault("apiIds", [])
        return state
    except json.JSONDecodeError:
        _raise(409, "invalid_blackboard", "current publication is not valid JSON")


def _window_bounds(project: sqlite3.Row) -> tuple[int, int]:
    start = int(project["processed_record_id"])
    end = project["pending_window_end"]
    if end is None:
        end = start
    return start, int(end)


# ---------------------------------------------------------------------------
# /observer/context
# ---------------------------------------------------------------------------


@router.post("/context")
async def observer_context(payload: ContextInput, request: Request) -> dict[str, Any]:
    session_id = request.state.session_id
    conn = _conn(request)
    try:
        project = _require_project(conn, session_id)
        if payload.mode == "summary":
            result = _ctx_summary(conn, session_id, project, payload)
        elif payload.mode == "window_records":
            result = _ctx_window_records(conn, session_id, project)
        elif payload.mode == "record_detail":
            result = _ctx_record_detail(
                conn, session_id, project, payload, in_window=True
            )
        elif payload.mode == "blackboard":
            result = _ctx_blackboard(conn, project)
        elif payload.mode == "history_record":
            result = _ctx_record_detail(
                conn, session_id, project, payload, in_window=False
            )
        else:
            _raise(400, "schema_invalid", f"unknown mode {payload.mode!r}")
        row = conn.execute("SELECT id FROM observations WHERE session_id=? AND status='running' ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        if row:
            entry = {"op": "observation_context", "arguments": payload.model_dump(), "response": result}
            conn.execute("UPDATE observations SET tool_logs_json=json_insert(tool_logs_json,'$[#]',json(?)) WHERE id=?", (json.dumps(entry, ensure_ascii=False), row["id"]))
        return result
    finally:
        conn.close()


def _ctx_summary(
    conn: sqlite3.Connection,
    session_id: str,
    project: sqlite3.Row,
    payload: ContextInput,
) -> dict[str, Any]:
    start, end = _window_bounds(project)
    tool_counts = {
        row["tool_name"]: int(row["c"])
        for row in conn.execute(
            "SELECT tool_name, COUNT(*) AS c FROM tool_records "
            "WHERE session_id = ? AND id > ? AND id <= ? "
            "GROUP BY tool_name",
            (session_id, start, end),
        )
    }
    record_count = sum(tool_counts.values())
    has_truncated = any(
        any(json.loads(r["metadata_json"]).get(k) for k in ("truncated", "output_truncated"))
        for r in conn.execute("SELECT metadata_json FROM tool_records WHERE session_id=? AND id>? AND id<=?", (session_id, start, end))
    )

    state = _load_state(conn, project)
    expand_ids = set(payload.assessment_ids)
    assessments_out: list[dict[str, Any]] = []
    for a in state["assessments"]:
        attempts = a.get("attempts", [])
        out = dict(a)
        out["attempts_total"] = len(attempts)
        if a["id"] in expand_ids:
            out["attempts"] = attempts
        else:
            out["attempts"] = attempts[-3:]
        assessments_out.append(out)

    apis_out: list[dict[str, Any]] = []
    for api in state.get("apis", []):
        tests = api.get("tests", [])
        out = dict(api)
        out["tests_total"] = len(tests)
        out["tests"] = tests[-3:]
        apis_out.append(out)

    return {
        "project": {
            "target": project["target"],
            "objective": project["objective"],
            "observer_paused": bool(int(project["observer_paused"])),
        },
        "window": {
            "start_record_id": start,
            "end_record_id": end,
            "record_count": record_count,
        },
        "records_overview": {
            "by_tool": tool_counts,
            "has_truncated": has_truncated,
        },
        "blackboard": {
            "revision": state.get("revision"),
            "node_count": len(state.get("assessments", [])),
            "api_count": len(state.get("apis", [])),
            "api_test_count": sum(
                len(a.get("tests", [])) for a in state.get("apis", [])
            ),
            "last_change_summary": None,
        },
        "assessments": assessments_out,
        "apis": apis_out,
        "guidance": state.get("guidance"),
        "last_errors": _last_errors(conn, session_id),
    }


def _ctx_window_records(
    conn: sqlite3.Connection, session_id: str, project: sqlite3.Row
) -> dict[str, Any]:
    start, end = _window_bounds(project)
    rows = conn.execute(
        "SELECT id, tool_name, received_at, "
        "       SUBSTR(tool_input_json, 1, ?) AS input_preview, "
        "       SUBSTR(tool_response_json, 1, ?) AS response_preview, "
        "       (LENGTH(tool_input_json) > ? OR LENGTH(tool_response_json) > ?) AS truncated "
        "FROM tool_records "
        "WHERE session_id = ? AND id > ? AND id <= ? "
        "ORDER BY id",
        (PREVIEW_BYTES, PREVIEW_BYTES, PREVIEW_BYTES, PREVIEW_BYTES,
         session_id, start, end),
    ).fetchall()
    return {
        "window": {"start_record_id": start, "end_record_id": end},
        "records": [
            {
                "id": int(r["id"]),
                "tool_name": r["tool_name"],
                "received_at": r["received_at"],
                "input_preview": r["input_preview"],
                "response_preview": r["response_preview"],
                "truncated": bool(r["truncated"]),
            }
            for r in rows
        ],
    }


def _ctx_record_detail(
    conn: sqlite3.Connection,
    session_id: str,
    project: sqlite3.Row,
    payload: ContextInput,
    in_window: bool,
) -> dict[str, Any]:
    record_id = int(payload.record_id or 0)
    start, end = _window_bounds(project)

    row = conn.execute(
        "SELECT id, tool_name, tool_input_json, tool_response_json "
        "FROM tool_records WHERE id = ? AND session_id = ?",
        (record_id, session_id),
    ).fetchone()
    if row is None:
        _raise(404, "record_not_found", f"record:{record_id} not in this session")

    if in_window and not (start < record_id <= end):
        _raise(
            400,
            "record_out_of_window",
            f"record:{record_id} is outside the current window ({start}, {end}]",
        )

    data = (row["tool_input_json"] or "") + "\n---\n" + (row["tool_response_json"] or "")
    data_bytes = data.encode("utf-8")
    offset = payload.offset
    length = payload.length
    if offset < len(data_bytes) and data_bytes[offset] & 0xC0 == 0x80:
        _raise(400, "invalid_offset", "offset must be a UTF-8 character boundary")
    chunk = data_bytes[offset : offset + length]
    # Return only complete UTF-8 characters, and advance by actual bytes returned.
    text = chunk.decode("utf-8", errors="ignore")
    chunk = text.encode("utf-8")
    if not chunk and offset < len(data_bytes):
        _raise(400, "invalid_length", "length is too small for the next UTF-8 character")
    has_more = offset + len(chunk) < len(data_bytes)
    return {
        "record_id": record_id,
        "tool_name": row["tool_name"],
        "segment": {
            "offset": offset,
            "length": len(chunk),
            "data": text,
            "next_offset": offset + len(chunk),
            "has_more": has_more,
        },
    }


def _ctx_blackboard(conn: sqlite3.Connection, project: sqlite3.Row) -> dict[str, Any]:
    return _load_state(conn, project)


# ---------------------------------------------------------------------------
# /observer/submit
# ---------------------------------------------------------------------------


@router.post("/submit")
async def observer_submit(payload: SubmitInput, request: Request) -> dict[str, Any]:
    session_id = request.state.session_id
    cache: SubmitCache = request.app.state.submit_cache

    sub_hash = blackboard.submission_hash(payload, payload.baseRevision)
    cached = cache.get(session_id, sub_hash)
    if cached is not None:
        return cached

    conn = _conn(request)
    try:
        project = _require_project(conn, session_id)
        if project["observer_paused"]:
            _raise(409, "observer_paused", "Observer is paused")
        obs_id = observation.start_observation(conn, session_id, trigger="observer_submit")
        project = _require_project(conn, session_id)
        old_state = _load_state(conn, project)

        if (payload.baseRevision or None) != (old_state.get("revision") or None):
            response = _failure(
                [
                    {
                        "path": "/baseRevision",
                        "code": "stale_revision",
                        "message": (
                            f"baseRevision does not match current revision "
                            f"{old_state.get('revision')!r}"
                        ),
                    }
                ]
            )
            _append_tool_log(conn, project, sub_hash, response, payload.model_dump())
            return response

        # 业务校验
        validation_errors = _validate_submit(conn, session_id, old_state, payload)
        if validation_errors:
            response = _failure(validation_errors)
            _append_tool_log(conn, project, sub_hash, response, payload.model_dump())
            return response

        # merge
        outcome = blackboard.merge(old_state, payload)
        if outcome.conflicts:
            response = _failure(
                [
                    {"path": c.path, "code": c.code, "message": c.message}
                    for c in outcome.conflicts
                ]
            )
            _append_tool_log(conn, project, sub_hash, response, payload.model_dump())
            return response

        # 确保有 running observation
        obs_id = observation.start_observation(
            conn, session_id, trigger="observer_submit"
        )

        if outcome.unchanged:
            observation.commit_unchanged(conn, session_id, obs_id)
            response = {
                "ok": True,
                "revision": old_state.get("revision"),
                "unchanged": True,
                "warnings": outcome.warnings,
            }
        else:
            new_state = outcome.new_state
            new_state["revision"] = blackboard.new_revision()
            observation.commit_publish(
                conn,
                session_id,
                obs_id,
                json.dumps(new_state, ensure_ascii=False),
                render_map(new_state, project["pending_window_end"]),
            )
            response = {
                "ok": True,
                "revision": new_state["revision"],
                "unchanged": False,
                "warnings": outcome.warnings,
            }

        _append_tool_log(conn, project, sub_hash, response, payload.model_dump())
        cache.put(session_id, sub_hash, response)
        return response
    finally:
        conn.close()


def _failure(errors: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ok": False, "errors": errors}


def _validate_submit(
    conn: sqlite3.Connection,
    session_id: str,
    old_state: dict,
    payload: SubmitInput,
) -> list[dict[str, Any]]:
    errors: list[dict[str, Any]] = []

    # 保留 ID
    for idx, u in enumerate(payload.upserts):
        if blackboard.is_reserved_id(u.id):
            errors.append(
                {
                    "path": f"/upserts/{idx}/id",
                    "code": "reserved_id",
                    "message": f"{u.id!r} 使用了宿主保留前缀",
                }
            )
    for idx, api in enumerate(payload.apis):
        if blackboard.is_reserved_id(api.id):
            errors.append(
                {
                    "path": f"/apis/{idx}/id",
                    "code": "reserved_id",
                    "message": f"{api.id!r} 使用了宿主保留前缀",
                }
            )

    # 本次 upserts 内部 duplicate
    seen_ids: dict[str, int] = {}
    for idx, u in enumerate(payload.upserts):
        if u.id in seen_ids:
            errors.append(
                {
                    "path": f"/upserts/{idx}/id",
                    "code": "duplicate_id",
                    "message": f"{u.id!r} 在本次提交中重复",
                }
            )
        seen_ids[u.id] = idx
        seen_attempt_ids: set[str] = set()
        for ai, att in enumerate(u.attempts):
            if att.id in seen_attempt_ids:
                errors.append(
                    {
                        "path": f"/upserts/{idx}/attempts/{ai}/id",
                        "code": "duplicate_id",
                        "message": f"attempt {att.id!r} 在同一 upsert 中重复",
                    }
                )
            seen_attempt_ids.add(att.id)
        seen_api_refs: set[str] = set()
        for ai, api_id in enumerate(u.apiIds):
            if api_id in seen_api_refs:
                errors.append(
                    {
                        "path": f"/upserts/{idx}/apiIds/{ai}",
                        "code": "duplicate_id",
                        "message": f"API 引用 {api_id!r} 在同一 upsert 中重复",
                    }
                )
            seen_api_refs.add(api_id)

    seen_api_ids: set[str] = set()
    for idx, api in enumerate(payload.apis):
        if api.id in seen_api_ids:
            errors.append(
                {
                    "path": f"/apis/{idx}/id",
                    "code": "duplicate_id",
                    "message": f"API {api.id!r} 在本次提交中重复",
                }
            )
        seen_api_ids.add(api.id)
        seen_test_ids: set[str] = set()
        for ti, test in enumerate(api.tests):
            if test.id in seen_test_ids:
                errors.append(
                    {
                        "path": f"/apis/{idx}/tests/{ti}/id",
                        "code": "duplicate_id",
                        "message": f"test {test.id!r} 在同一 API 中重复",
                    }
                )
            seen_test_ids.add(test.id)

    # retireIds 存在性（必须在当前 assessments 或 retired 中）
    active_ids = {a["id"] for a in old_state.get("assessments", [])}
    retired_ids = {a["id"] for a in old_state.get("retired", [])}
    for idx, rid in enumerate(payload.retireIds):
        if rid not in active_ids and rid not in retired_ids:
            errors.append(
                {
                    "path": f"/retireIds/{idx}",
                    "code": "unknown_id",
                    "message": f"{rid!r} 不在当前黑板的 assessments / retired 中",
                }
            )

    # evidenceRefs：收集全部，一次查 SQLite
    all_record_ids: set[int] = set()
    ref_locations: list[tuple[str, int]] = []  # (path, record_id)
    for idx, u in enumerate(payload.upserts):
        for ri, ref in enumerate(u.evidenceRefs):
            rid = int(ref.split(":", 1)[1])
            all_record_ids.add(rid)
            ref_locations.append((f"/upserts/{idx}/evidenceRefs/{ri}", rid))
        for ai, att in enumerate(u.attempts):
            for ri, ref in enumerate(att.evidenceRefs):
                rid = int(ref.split(":", 1)[1])
                all_record_ids.add(rid)
                ref_locations.append(
                    (f"/upserts/{idx}/attempts/{ai}/evidenceRefs/{ri}", rid)
                )
    for idx, api in enumerate(payload.apis):
        for ti, test in enumerate(api.tests):
            for ri, rec_id in enumerate(test.record_ids):
                all_record_ids.add(rec_id)
                ref_locations.append(
                    (f"/apis/{idx}/tests/{ti}/record_ids/{ri}", rec_id)
                )

    valid_record_ids: set[int] = set()
    if all_record_ids:
        placeholders = ",".join("?" * len(all_record_ids))
        rows = conn.execute(
            f"SELECT id FROM tool_records "
            f"WHERE session_id = ? AND id IN ({placeholders})",
            (session_id, *sorted(all_record_ids)),
        ).fetchall()
        valid_record_ids = {int(r["id"]) for r in rows}

    for path, rid in ref_locations:
        if rid not in valid_record_ids:
            errors.append(
                {
                    "path": path,
                    "code": "unknown_evidence",
                    "message": f"record:{rid} 不存在或不属于本会话",
                }
            )

    # dependsOn：合并后父节点必须存在；DFS 检环
    merged_ids = set(active_ids)
    for u in payload.upserts:
        merged_ids.add(u.id)
    for rid in payload.retireIds:
        merged_ids.discard(rid)

    # apiIds：允许引用旧 API，或本次 apis[] 同时新建的 API。
    merged_api_ids = {api["id"] for api in old_state.get("apis", [])}
    merged_api_ids.update(api.id for api in payload.apis)
    for idx, u in enumerate(payload.upserts):
        for ai, api_id in enumerate(u.apiIds):
            if api_id not in merged_api_ids:
                errors.append(
                    {
                        "path": f"/upserts/{idx}/apiIds/{ai}",
                        "code": "unknown_api",
                        "message": f"API {api_id!r} 不在当前黑板或本次 apis 中",
                    }
                )

    # 父节点存在性
    deps_map: dict[str, list[str]] = {}
    for a in old_state.get("assessments", []):
        deps_map[a["id"]] = list(a.get("dependsOn") or [])
    for idx, u in enumerate(payload.upserts):
        deps_map[u.id] = list(u.dependsOn)
        for di, parent in enumerate(u.dependsOn):
            if parent not in merged_ids:
                errors.append(
                    {
                        "path": f"/upserts/{idx}/dependsOn/{di}",
                        "code": "unknown_dependency",
                        "message": f"{parent!r} 不在合并后的 assessments 中",
                    }
                )
    # 环检查
    cycles = _find_cycles(deps_map, merged_ids)
    for node in cycles:
        errors.append(
            {
                "path": f"/upserts/*/dependsOn (id={node})",
                "code": "cycle",
                "message": f"dependsOn 从 {node!r} 构成环",
            }
        )

    return errors


def _find_cycles(deps_map: dict[str, list[str]], valid_ids: set[str]) -> set[str]:
    WHITE, GRAY, BLACK = 0, 1, 2
    color: dict[str, int] = {}
    cyclic: set[str] = set()

    def dfs(node: str) -> None:
        color[node] = GRAY
        for parent in deps_map.get(node, []):
            if parent not in valid_ids:
                continue
            c = color.get(parent, WHITE)
            if c == GRAY:
                cyclic.add(parent)
            elif c == WHITE:
                dfs(parent)
        color[node] = BLACK

    for node in deps_map:
        if node not in valid_ids:
            continue
        if color.get(node, WHITE) == WHITE:
            dfs(node)
    return cyclic


def _append_tool_log(
    conn: sqlite3.Connection,
    project: sqlite3.Row,
    submission_hash: str,
    response: dict[str, Any],
    arguments: dict | None = None,
) -> None:
    """把本次 submit 的摘要追加到当前 observation.tool_logs_json。

    这里是尽力而为的审计记录；写失败不影响主流程。
    使用项目当前的 current_observation_id；没有就跳过（第一次提交尚未发布）。
    """
    row = conn.execute("SELECT id FROM observations WHERE session_id=? ORDER BY id DESC LIMIT 1", (project["session_id"],)).fetchone()
    if row is None:
        return
    cur_obs = row["id"]

    entry = {
        "op": "observation_submit",
        "arguments": arguments,
        "response": response,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hash": submission_hash,
        "ok": bool(response.get("ok")),
        "errors": response.get("errors"),
        "warnings": response.get("warnings"),
        "revision": response.get("revision"),
    }
    try:
        conn.execute(
            "UPDATE observations "
            "SET tool_logs_json = json_insert(tool_logs_json, '$[#]', json(?)) "
            "WHERE id = ?",
            (json.dumps(entry, ensure_ascii=False), int(cur_obs)),
        )
    except sqlite3.OperationalError:
        pass  # json1 扩展不可用；保守跳过


def _last_errors(conn, session_id):
    row = conn.execute("SELECT tool_logs_json FROM observations WHERE session_id=? ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
    if not row:
        return []
    logs = json.loads(row["tool_logs_json"])
    for log in reversed(logs):
        if log.get("op") == "observation_submit":
            return log.get("errors") or []
    return []
