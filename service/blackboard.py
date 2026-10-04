"""FlySec memory v2: evidence-backed observations and append-only tests.

Storage is independent of the graph. This module has no HTTP or database access.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit, urlunsplit

from .schemas import MemoryCommitInput
from .legacy import to_memory

RESERVED_ID_PREFIXES = ("legacy-", "host-")


def initial_state() -> dict:
    return {"schema_version": 2, "revision": None, "topics": [], "facts": [], "tests": [], "apis": [], "questions": []}


def normalize_endpoint(endpoint: str, target: str = "") -> str:
    method, path = endpoint.strip().split(" ", 1)
    method = method.upper()
    if re.search(r"\s", path):
        raise ValueError("endpoint must contain only METHOD and a URI; move annotations into purpose")
    if path.startswith("/") and target.startswith(("http://", "https://")):
        path = urljoin(target, path)
    uri = urlsplit(path)
    if uri.scheme and uri.scheme not in {"http", "https"}:
        raise ValueError("API endpoints must be HTTP(S), not DNS/TLS probes or local file paths")
    if uri.scheme and not uri.netloc:
        raise ValueError("HTTP(S) endpoint requires a host")
    if not uri.scheme and not path.startswith("/"):
        raise ValueError("use an absolute HTTP(S) URI or a root-relative HTTP path")
    # Path case and trailing slash are significant. Never merge them by guessing.
    path = urlunsplit((uri.scheme.lower(), uri.netloc.lower(), uri.path or ("/" if uri.netloc else ""), "", ""))
    return f"{method} {path}"


def prepare_state(stored: dict | None, target: str = "") -> dict:
    if stored is None:
        return initial_state()
    state = to_memory(stored)
    if state.get("source_schema_version") != 1:
        return state
    aliases, by_endpoint, apis = {}, {}, []
    for api in state["apis"]:
        # Historical descriptions sometimes included prose in the endpoint field.
        raw = api["endpoint"].split(" ", 2)
        candidate = " ".join(raw[:2])
        try:
            endpoint = normalize_endpoint(candidate, target)
        except ValueError:
            endpoint = api["endpoint"]  # Preserve unconvertible history, visibly marked legacy.
        existing = by_endpoint.get(endpoint)
        if existing:
            aliases[api["id"]] = existing["id"]
            existing["evidence_ids"] = sorted(set(existing["evidence_ids"] + api["evidence_ids"]))
            params = {p["name"]: p for p in existing["parameters"]}
            params.update({p["name"]: p for p in api["parameters"]})
            existing["parameters"] = list(params.values())
        else:
            api["endpoint"] = endpoint
            by_endpoint[endpoint] = api
            apis.append(api)
    state["apis"] = apis
    for key in ("topics", "tests", "questions"):
        for item in state[key]:
            item["api_ids"] = list(dict.fromkeys(aliases.get(x, x) for x in item.get("api_ids", [])))
    return state


def content_hash(state: dict) -> str:
    return hashlib.sha256(json.dumps({k: v for k, v in state.items() if k != "revision"}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def submission_hash(payload: MemoryCommitInput) -> str:
    return hashlib.sha256(json.dumps(payload.model_dump(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def new_revision() -> str:
    return str(uuid.uuid4())


@dataclass
class MergeOutcome:
    state: dict
    changed: bool
    changes: dict[str, int] = field(default_factory=dict)
    errors: list[dict] = field(default_factory=list)


def merge(old: dict, payload: MemoryCommitInput, target: str = "") -> MergeOutcome:
    state = copy.deepcopy(old)
    errors, changes = [], {}
    for collection in ("topics", "facts", "tests", "apis", "questions"):
        by_id = {x["id"]: x for x in state[collection]}
        seen = set()
        count = 0
        for index, model in enumerate(getattr(payload, collection)):
            item = model.model_dump()
            item_id = item["id"]
            path = f"/{collection}/{index}"
            if item_id in seen:
                errors.append({"path": path + "/id", "code": "duplicate_id", "message": "duplicate ID in this collection"})
                continue
            seen.add(item_id)
            if any(item_id.startswith(p) for p in RESERVED_ID_PREFIXES) and item_id not in by_id:
                errors.append({"path": path + "/id", "code": "reserved_id", "message": "ID is reserved for host-owned history"})
                continue
            if collection == "apis":
                try:
                    item["endpoint"] = normalize_endpoint(item["endpoint"], target)
                except ValueError as exc:
                    errors.append({"path": path + "/endpoint", "code": "invalid_endpoint", "message": str(exc)})
                    continue
            previous = by_id.get(item_id)
            if collection == "apis" and previous is None:
                method, uri = item["endpoint"].split(" ", 1)
                ambiguous = next((a for a in by_id.values() if a["endpoint"].split(" ", 1)[-1] == uri and (method == "UNKNOWN" or a["endpoint"].startswith("UNKNOWN "))), None)
                if ambiguous:
                    errors.append({"path": path + "/id", "code": "reuse_api", "message": f"Reuse/refine existing API {ambiguous['id']} rather than creating an unknown-method duplicate"})
                    continue
            if previous == item:
                continue
            if previous and collection in {"facts", "tests"}:
                errors.append({"path": path, "code": "immutable_record", "message": "Use a new ID; facts may explicitly supersede an old fact, tests append without overwriting"})
                continue
            if previous and collection == "apis":
                old_method, old_uri = previous["endpoint"].split(" ", 1)
                new_method, new_uri = item["endpoint"].split(" ", 1)
                refining_method = old_method == "UNKNOWN" and new_method != "UNKNOWN" and old_uri == new_uri
                if previous["endpoint"] != item["endpoint"] and not refining_method:
                    errors.append({"path": path + "/endpoint", "code": "immutable_endpoint", "message": "The same API ID cannot change its endpoint"})
                    continue
                params = {p["name"]: p for p in previous.get("parameters", [])}
                params.update({p["name"]: p for p in item["parameters"]})
                item["parameters"] = list(params.values())
                item["evidence_ids"] = sorted(set(previous.get("evidence_ids", []) + item["evidence_ids"]))
            if previous and collection == "questions" and previous["status"] == "resolved" and item["status"] == "open":
                errors.append({"path": path, "code": "question_reopen", "message": "Create a new question with new evidence rather than silently reopening a resolved question"})
                continue
            if previous and collection == "topics":
                # API links are cumulative; forgetting one must not orphan its tests.
                item["api_ids"] = list(dict.fromkeys(previous.get("api_ids", []) + item["api_ids"]))
            if previous != item:
                by_id[item_id] = item
                count += 1
        state[collection] = list(by_id.values())
        changes[collection] = count
    topics = {t["id"] for t in state["topics"]}
    facts = {f["id"]: f for f in state["facts"]}
    apis = {a["id"] for a in state["apis"]}
    endpoints = {}
    for api in state["apis"]:
        if api["endpoint"] in endpoints and endpoints[api["endpoint"]] != api["id"]:
            errors.append({"path": "/apis", "code": "duplicate_endpoint", "message": f"Reuse API {endpoints[api['endpoint']]} for this endpoint"})
        endpoints[api["endpoint"]] = api["id"]
    for collection in ("facts", "tests", "questions"):
        for item in state[collection]:
            if item["topic_id"] not in topics:
                errors.append({"path": f"/{collection}/{item['id']}/topic_id", "code": "unknown_topic", "message": "topic does not exist"})
    for collection in ("topics", "tests", "questions"):
        for item in state[collection]:
            for aid in item.get("api_ids", []):
                if aid not in apis:
                    errors.append({"path": f"/{collection}/{item['id']}/api_ids", "code": "unknown_api", "message": f"unknown API {aid}"})
    parents = {}
    for topic in state["topics"]:
        parents[topic["id"]] = []
        for fid in topic["origin_fact_ids"]:
            if fid not in facts:
                errors.append({"path": f"/topics/{topic['id']}/origin_fact_ids", "code": "unknown_fact", "message": f"unknown origin observation {fid}"})
            else:
                parent = facts[fid]["topic_id"]
                if parent == topic["id"]:
                    errors.append({"path": "/topics", "code": "self_origin", "message": "a topic cannot originate from itself"})
                parents[topic["id"]].append(parent)
    visiting, done = set(), set()
    def visit(tid):
        if tid in visiting:
            errors.append({"path": "/topics", "code": "cycle", "message": "observation-derived topic relationships contain a cycle"})
            return
        if tid in done: return
        visiting.add(tid)
        for parent in parents.get(tid, []): visit(parent)
        visiting.remove(tid); done.add(tid)
    for tid in topics: visit(tid)
    for fact in state["facts"]:
        parent = fact.get("supersedes")
        if parent:
            if parent not in facts or parent == fact["id"] or facts[parent]["topic_id"] != fact["topic_id"]:
                errors.append({"path": "/facts", "code": "invalid_correction", "message": "supersedes must reference another observation in the same topic"})
            chain, cursor = set(), fact["id"]
            while cursor in facts:
                if cursor in chain:
                    errors.append({"path": "/facts", "code": "correction_cycle", "message": "correction chain contains a cycle"}); break
                chain.add(cursor); cursor = facts[cursor].get("supersedes")
    return MergeOutcome(state, content_hash(state) != content_hash(old), changes, errors)


def active_facts(state: dict) -> list[dict]:
    replaced = {f.get("supersedes") for f in state.get("facts", []) if f.get("supersedes")}
    return [f for f in state.get("facts", []) if f["id"] not in replaced]
