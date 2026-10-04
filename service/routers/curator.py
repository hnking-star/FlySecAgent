"""Memory read/commit API. Identity comes exclusively from the host-issued token."""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, HTTPException, Request

from .. import blackboard, observation
from ..db import connect, transaction
from ..evidence import execution_status
from ..feedback import render_digest
from ..schemas import MemoryReadInput, MemoryCommitInput

router = APIRouter(prefix="/memory", tags=["memory"])
PREVIEW_BYTES = 2048


class SubmitCache:
    """Per-window delivery retry receipts, not model-generated memory."""
    def __init__(self, ttl_seconds=300):
        self.ttl = ttl_seconds
        self.entries = {}

    def get(self, sid, key):
        item = self.entries.get((sid, key))
        if not item or time.monotonic() - item[0] > self.ttl: return None
        return item[1]

    def put(self, sid, key, result):
        self.entries[sid, key] = (time.monotonic(), result)

    def clear_session(self, sid):
        for key in list(self.entries):
            if key[0] == sid: self.entries.pop(key)


def fail(status, code, message):
    raise HTTPException(status, {"code": code, "message": message})


def project_row(conn, sid):
    row = conn.execute("SELECT * FROM projects WHERE session_id=?", (sid,)).fetchone()
    if row is None: fail(404, "session_not_found", "no project is bound to this runtime")
    return row


def window(project):
    return int(project["processed_record_id"]), int(project["pending_window_end"] if project["pending_window_end"] is not None else project["processed_record_id"])


def stored_state(conn, project):
    if project["current_observation_id"] is None: return None
    row = conn.execute("SELECT state_json FROM observations WHERE id=? AND session_id=? AND status='published'", (project["current_observation_id"], project["session_id"])).fetchone()
    if row is None: fail(409, "invalid_memory", "current published snapshot is unavailable")
    try:
        data = json.loads(row[0])
        if not isinstance(data, dict): raise ValueError()
        return data
    except (ValueError, TypeError):
        fail(409, "invalid_memory", "published state is not a valid object")


def load_state(conn, project):
    try: return blackboard.prepare_state(stored_state(conn, project), project["target"])
    except ValueError as exc: fail(409, "unsupported_schema", str(exc))


def append_log(conn, sid, op, args, result):
    row = conn.execute("SELECT id,tool_logs_json FROM observations WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if row is None: return
    logs = json.loads(row["tool_logs_json"] or "[]")
    logs.append({"op": op, "arguments": args, "ok": result.get("ok", True), "response": result})
    conn.execute("UPDATE observations SET tool_logs_json=? WHERE id=? AND session_id=?", (json.dumps(logs, ensure_ascii=False), row["id"], sid))


def last_errors(conn, sid):
    row = conn.execute("SELECT tool_logs_json FROM observations WHERE session_id=? ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
    if row:
        for item in reversed(json.loads(row[0] or "[]")):
            if item.get("op") in {"curator_commit", "memory_commit"}:
                return item.get("response", {}).get("errors", [])
    return []


def read_record(conn, sid, payload, window_end):
    row = conn.execute("SELECT * FROM tool_records WHERE session_id=? AND id=?", (sid, payload.record_id)).fetchone()
    if not row: fail(404, "record_not_found", "record does not belong to this session")
    if row["id"] > window_end: fail(400, "record_after_window", "record is newer than this fixed evidence window")
    raw = (row["tool_input_json"] + "\n---\n" + row["tool_response_json"]).encode()
    offset, length = payload.offset, payload.length
    if offset < len(raw) and raw[offset] & 0xC0 == 0x80: fail(400, "invalid_offset", "offset must be a UTF-8 boundary")
    segment = raw[offset:offset + length].decode("utf-8", errors="ignore")
    size = len(segment.encode())
    if not size and offset < len(raw): fail(400, "invalid_length", "length cannot contain the next UTF-8 character")
    return {"record_id": row["id"], "tool_name": row["tool_name"], "execution": execution_status(json.loads(row["tool_response_json"]), json.loads(row["metadata_json"])),
            "segment": {"offset": offset, "length": size, "next_offset": offset + size, "data": segment, "has_more": offset + size < len(raw)}}


@router.post("/read")
async def curator_read(payload: MemoryReadInput, request: Request):
    sid = request.state.session_id
    conn = connect(request.app.state.cfg.data_dir)
    try:
        project = project_row(conn, sid)
        if project["observer_paused"]: fail(409, "curator_paused", "Memory Curator is paused")
        start, end = window(project)
        if payload.mode == "record":
            result = read_record(conn, sid, payload, end)
        elif payload.mode == "records":
            rows = conn.execute("SELECT * FROM tool_records WHERE session_id=? AND id>? AND id<=? ORDER BY id LIMIT ?", (sid, max(start, payload.after_id), end, payload.limit + 1)).fetchall()
            records = []
            for row in rows[:payload.limit]:
                records.append({"id": row["id"], "tool_name": row["tool_name"], "received_at": row["received_at"],
                                "input_preview": row["tool_input_json"].encode()[:PREVIEW_BYTES].decode("utf-8", errors="ignore"),
                                "response_preview": row["tool_response_json"].encode()[:PREVIEW_BYTES].decode("utf-8", errors="ignore"),
                                "preview_truncated": any(len(row[k].encode()) > PREVIEW_BYTES for k in ("tool_input_json", "tool_response_json")),
                                "source_truncated": bool(json.loads(row["metadata_json"]).get("truncated") or json.loads(row["metadata_json"]).get("output_truncated")),
                                "execution": execution_status(json.loads(row["tool_response_json"]), json.loads(row["metadata_json"]))})
            result = {"window": {"start": start, "end": end}, "records": records, "has_more": len(rows) > payload.limit,
                      "next_after_id": records[-1]["id"] if records else max(start, payload.after_id)}
        else:
            state = load_state(conn, project)
            if payload.mode == "state":
                result = state
            else:
                record_count = conn.execute("SELECT count(*) FROM tool_records WHERE session_id=? AND id>? AND id<=?", (sid, start, end)).fetchone()[0]
                expanded = set(payload.topic_ids)
                topics = []
                for topic in state["topics"]:
                    tid = topic["id"]
                    facts = [f for f in blackboard.active_facts(state) if f["topic_id"] == tid]
                    tests = [t for t in state["tests"] if t["topic_id"] == tid]
                    topics.append({**topic, "fact_count": len(facts), "test_count": len(tests),
                                   "facts": facts if tid in expanded else facts[-4:], "tests": tests if tid in expanded else tests[-3:]})
                result = {"project": {"target": project["target"], "objective": project["objective"]},
                          "window": {"start": start, "end": end, "record_count": record_count},
                          "memory": {"schema_version": 2, "revision": state["revision"], "contains_legacy_notes": any(f["kind"] == "legacy_summary" for f in state["facts"])},
                          "topics": topics, "apis": state["apis"], "questions": [q for q in state["questions"] if q["status"] == "open"],
                          "last_errors": last_errors(conn, sid)}
        row = conn.execute("SELECT id FROM observations WHERE session_id=? AND status='running'", (sid,)).fetchone()
        if row: append_log(conn, sid, "curator_read", payload.model_dump(), result)
        return result
    finally: conn.close()


@router.post("/commit")
async def curator_commit(payload: MemoryCommitInput, request: Request):
    sid = request.state.session_id
    conn = connect(request.app.state.cfg.data_dir)
    try:
        project = project_row(conn, sid)
        if project["observer_paused"]: fail(409, "curator_paused", "Memory Curator is paused")
        # The host owns the fixed window. Commits cannot manufacture a second active job.
        active = conn.execute("SELECT id FROM observations WHERE session_id=? AND status='running'", (sid,)).fetchone()
        if not project["observation_enabled"] and not active: fail(409, "observation_disabled", "observation is disabled")
        key = blackboard.submission_hash(payload)
        cached = request.app.state.submit_cache.get(sid, key)
        if cached: return cached
        with transaction(conn):
            project = project_row(conn, sid)
            if project["observer_paused"]: fail(409, "curator_paused", "Memory Curator is paused")
            active = conn.execute("SELECT id FROM observations WHERE session_id=? AND status='running'", (sid,)).fetchone()
            if not project["observation_enabled"] and not active: fail(409, "observation_disabled", "observation is disabled")
            obs_id = observation.start_observation(conn, sid, "curator_commit")
            project = project_row(conn, sid)
            stored = stored_state(conn, project)
            old = load_state(conn, project)
            errors = []
            start, end = window(project)
            count = conn.execute("SELECT count(*) FROM tool_records WHERE session_id=? AND id>? AND id<=?", (sid, start, end)).fetchone()[0]
            if count and not any(getattr(payload, key) for key in ("topics", "facts", "tests", "apis", "questions")) and not payload.unchanged_reason:
                errors.append({"path": "/unchanged_reason", "code": "empty_delta", "message": "New receipts exist. Record their observations/tests, or explain explicitly why they add no memory"})
            if payload.revision != old["revision"]:
                errors.append({"path": "/revision", "code": "stale_revision", "message": "Read memory again and use its current revision"})
            outcome = blackboard.merge(old, payload, project["target"])
            errors.extend(outcome.errors)
            # Validate only incoming references; archived imports may lack references.
            refs = set()
            for collection in ("facts", "tests", "apis", "questions"):
                for item in getattr(payload, collection): refs.update(item.evidence_ids)
            valid = set()
            if refs:
                marks = ",".join("?" for _ in refs)
                valid = {r[0] for r in conn.execute(f"SELECT id FROM tool_records WHERE session_id=? AND id IN ({marks})", (sid, *sorted(refs)))}
            for rid in sorted(refs - valid):
                errors.append({"path": "/evidence_ids", "code": "unknown_evidence", "message": f"record:{rid} is missing or belongs to another session"})
            for rid in sorted(refs & valid):
                if rid > end:
                    errors.append({"path": "/evidence_ids", "code": "record_after_window", "message": f"record:{rid} is newer than the fixed evidence window"})
            # A local tool failure must never be converted into a negative target test.
            for index, test in enumerate(payload.tests):
                rows = conn.execute("SELECT tool_response_json,metadata_json FROM tool_records WHERE session_id=? AND id IN (" + ",".join("?" for _ in test.evidence_ids) + ")", (sid, *test.evidence_ids)).fetchall()
                statuses = [execution_status(json.loads(r[0]), json.loads(r[1])) for r in rows]
                if test.execution == "completed" and statuses and all(s in {"error", "denied", "interrupted"} for s in statuses):
                    errors.append({"path": f"/tests/{index}/execution", "code": "execution_mismatch", "message": "Evidence only contains local execution failures, not a completed target test"})
            if errors:
                result = {"ok": False, "errors": errors}
                append_log(conn, sid, "curator_commit", payload.model_dump(), result)
                return result
            promote = stored is not None and stored.get("schema_version", 1) != 2
            state = outcome.state
            if outcome.changed or promote:
                state["revision"] = blackboard.new_revision()
                result = {"ok": True, "revision": state["revision"], "published": True, "changes": outcome.changes}
            else:
                result = {"ok": True, "revision": old["revision"], "published": False, "changes": outcome.changes}
            if result["published"]:
                observation.commit_publish(conn, sid, obs_id, json.dumps(state, ensure_ascii=False), render_digest(state, project["pending_window_end"], project["objective"]))
            else:
                observation.commit_unchanged(conn, sid, obs_id)
            append_log(conn, sid, "curator_commit", payload.model_dump(), result)
        request.app.state.submit_cache.put(sid, key, result)
        return result
    finally: conn.close()
