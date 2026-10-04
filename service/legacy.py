"""Read-only conversion of archived v1 snapshots. Never upgrades a claim to a fact.

Old SQL rows remain untouched. Converted notes are labelled legacy_summary, and
historical tests keep unknown execution status rather than pretending verification.
"""
from __future__ import annotations

import hashlib
import copy


def legacy_id(kind: str, *parts: str) -> str:
    return f"legacy-{kind}-" + hashlib.sha256("\0".join(parts).encode()).hexdigest()[:20]


def evidence_ids(refs: list) -> list[int]:
    return sorted({int(r.split(":", 1)[1]) for r in refs if isinstance(r, str) and r.startswith("record:") and r.split(":", 1)[1].isdigit()})


def to_memory(state: dict) -> dict:
    if state.get("schema_version") == 2:
        for key in ("topics", "facts", "tests", "apis", "questions"):
            if not isinstance(state.get(key), list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in state[key]):
                raise ValueError(f"invalid stored memory collection: {key}")
        return copy.deepcopy(state)
    if state.get("schema_version", 1) != 1:
        raise ValueError("unsupported stored schema version")
    topics, facts, tests, apis, questions = [], [], [], [], []
    rows = state.get("assessments", []) + state.get("retired", [])
    note_ids = {a["id"]: legacy_id("fact", a["id"]) for a in rows}
    api_topic = {}
    for row in rows:
        rid = row["id"]
        topics.append({"id": rid, "title": row.get("subject", rid), "summary": row.get("conclusion", ""),
                       "api_ids": list(row.get("apiIds", [])),
                       "origin_fact_ids": [note_ids[p] for p in row.get("dependsOn", []) if p in note_ids and p != rid]})
        refs = evidence_ids(row.get("evidenceRefs", []))
        facts.append({"id": note_ids[rid], "topic_id": rid, "statement": row.get("conclusion", ""),
                      "kind": "legacy_summary", "scope": row.get("uncertainty") or "历史摘要；执行条件未结构化，未重新核验。",
                      "evidence_ids": refs, "supersedes": None})
        for api_id in row.get("apiIds", []):
            api_topic.setdefault(api_id, rid)
        for attempt in row.get("attempts", []):
            tests.append({"id": legacy_id("test", rid, attempt.get("id", "")), "topic_id": rid,
                          "api_ids": list(row.get("apiIds", [])), "action": attempt.get("action", ""),
                          "result": attempt.get("result", ""), "execution": "unknown", "outcome": "not_evaluated",
                          "scope": "历史尝试，执行状态未重新核验。", "evidence_ids": evidence_ids(attempt.get("evidenceRefs", []))})
        if row.get("uncertainty"):
            questions.append({"id": legacy_id("question", rid), "topic_id": rid, "question": row["uncertainty"],
                              "api_ids": list(row.get("apiIds", [])), "evidence_ids": refs, "status": "open", "resolution": None})
    for api in state.get("apis", []):
        aid = api["id"]
        topic = api_topic.get(aid)
        if topic is None and api.get("tests"):
            topic = legacy_id("topic", aid)
            topics.append({"id": topic, "title": api.get("endpoint", aid), "summary": api.get("purpose", ""),
                           "api_ids": [aid], "origin_fact_ids": []})
        refs = set()
        for test in api.get("tests", []):
            ids = [int(x) for x in test.get("record_ids", []) if isinstance(x, int) and x > 0]
            refs.update(ids)
            if topic:
                # Deduplicate only identical historical content, not overlapping evidence.
                existing = next((t for t in tests if t["action"] == test.get("action") and t["result"] == test.get("result") and sorted(t["evidence_ids"]) == sorted(ids)), None)
                if existing:
                    if aid not in existing["api_ids"]: existing["api_ids"].append(aid)
                else:
                    tests.append({"id": legacy_id("api-test", aid, test.get("id", "")), "topic_id": topic,
                                  "api_ids": [aid], "action": test.get("action", ""), "result": test.get("result", ""),
                                  "execution": "unknown", "outcome": "not_evaluated", "scope": "历史 API 测试记录。", "evidence_ids": ids})
        apis.append({"id": aid, "endpoint": api.get("endpoint", "UNKNOWN /"), "purpose": api.get("purpose", ""),
                     "parameters": copy.deepcopy(api.get("parameters", [])), "evidence_ids": sorted(refs)})
    # Do not keep strategy/LOCK instructions in the active memory protocol.
    return {"schema_version": 2, "revision": state.get("revision"), "topics": topics, "facts": facts,
            "tests": tests, "apis": apis, "questions": questions, "source_schema_version": 1}
