"""state_json 的读写与增量合并（纯函数核心）。

本模块不碰 HTTP、不碰 SQLite。所有函数接受/返回 dict 或 pydantic model。
业务校验（evidence 是否属于本会话、dependsOn 查环等）由路由层做。
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field

from .schemas import SubmitInput

SCHEMA_VERSION = 1
RESERVED_ID_PREFIXES: tuple[str, ...] = ("host-",)

_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "UNKNOWN"}


def initial_state() -> dict:
    """空黑板。revision 为 None 表示尚未发布过。"""
    return {
        "schema_version": SCHEMA_VERSION,
        "revision": None,
        "assessments": [],
        "retired": [],
        "apis": [],
        "guidance": {
            "hypothesis": None,
            "lock": None,
            "angleIds": [],
            "confirmedIds": [],
            "tension": [],
        },
    }


# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------


def normalize_endpoint(ep: str) -> tuple[str, list[str]]:
    """返回 (规范化后的 endpoint, warnings)。

    - 方法大写
    - path 去末尾 '/'（根 '/' 除外）
    - 去除 query string
    - 未知方法保留原文，但产生一条 warning
    """
    warnings: list[str] = []
    m = re.match(r"^(\S+)\s+(\S.*)$", ep.strip())
    if not m:
        return ep.strip(), warnings
    method = m.group(1).upper()
    path = m.group(2).split("?", 1)[0]
    if method not in _METHODS:
        warnings.append(f"unknown HTTP method: {method}")
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return f"{method} {path}", warnings


# ---------------------------------------------------------------------------
# 合并
# ---------------------------------------------------------------------------


@dataclass
class MergeConflict:
    path: str
    code: str
    message: str


@dataclass
class MergeOutcome:
    new_state: dict
    unchanged: bool
    warnings: list[str] = field(default_factory=list)
    conflicts: list[MergeConflict] = field(default_factory=list)


def merge(old: dict, submit: SubmitInput) -> MergeOutcome:
    """按 docs/details/05 §合并规则 的顺序走。

    发现 attempt / test 冲突时记录在 conflicts，由路由层决定是否报 conflicting_update。
    """
    new = copy.deepcopy(old)
    warnings: list[str] = []
    conflicts: list[MergeConflict] = []

    # 1. upserts 按 id merge
    assessments_by_id = {a["id"]: a for a in new["assessments"]}
    retired_by_id = {a["id"]: a for a in new["retired"]}

    for idx, upsert in enumerate(submit.upserts):
        new_a = upsert.model_dump()
        existing = assessments_by_id.get(new_a["id"])
        if existing is None:
            assessments_by_id[new_a["id"]] = new_a
            continue
        # 存在：按 attempt.id merge
        merged_attempts, attempt_conflicts = _merge_attempts(
            existing.get("attempts", []), new_a["attempts"], base_path=f"/upserts/{idx}/attempts"
        )
        new_a["attempts"] = merged_attempts
        assessments_by_id[new_a["id"]] = new_a
        conflicts.extend(attempt_conflicts)

    # 2. retireIds：从 assessments 搬到 retired；retireIds 的存在性由路由层校验
    for rid in submit.retireIds:
        row = assessments_by_id.pop(rid, None)
        if row is not None:
            retired_by_id[rid] = row

    new["assessments"] = list(assessments_by_id.values())
    new["retired"] = list(retired_by_id.values())

    # 3. apis 按 id merge（规范化 endpoint 作为同一 id 的内容更新；endpoint 冲突由上层发现）
    apis_by_id = {a["id"]: a for a in new["apis"]}
    for idx, new_api in enumerate(submit.apis):
        api_dict = new_api.model_dump()
        api_dict["endpoint"], ep_warnings = normalize_endpoint(api_dict["endpoint"])
        warnings.extend(ep_warnings)

        existing = apis_by_id.get(api_dict["id"])
        if existing is None:
            apis_by_id[api_dict["id"]] = api_dict
            continue
        existing["endpoint"] = api_dict["endpoint"]
        existing["purpose"] = api_dict.get("purpose", existing.get("purpose"))
        existing["parameters"] = _merge_params(
            existing.get("parameters", []), api_dict["parameters"]
        )
        existing["tests"], test_conflicts = _merge_tests(
            existing.get("tests", []), api_dict["tests"], base_path=f"/apis/{idx}/tests"
        )
        conflicts.extend(test_conflicts)

    new["apis"] = list(apis_by_id.values())

    # 4. guidance
    if submit.guidance is not None:
        new["guidance"] = submit.guidance.model_dump()
    else:
        active_ids = {a["id"] for a in new["assessments"]}
        g = new["guidance"]
        g["angleIds"] = [i for i in g["angleIds"] if i in active_ids]
        g["confirmedIds"] = [i for i in g["confirmedIds"] if i in active_ids]

    # 5. unchanged 判定（revision 不参与 hash）
    unchanged = _content_hash(new) == _content_hash(old)
    return MergeOutcome(
        new_state=new, unchanged=unchanged, warnings=warnings, conflicts=conflicts
    )


def _merge_attempts(
    old_attempts: list[dict], new_attempts: list[dict], base_path: str
) -> tuple[list[dict], list[MergeConflict]]:
    merged: dict[str, dict] = {a["id"]: a for a in old_attempts}
    conflicts: list[MergeConflict] = []
    for idx, a in enumerate(new_attempts):
        existing = merged.get(a["id"])
        if existing is not None and existing.get("action") != a.get("action"):
            conflicts.append(
                MergeConflict(
                    path=f"{base_path}/{idx}/action",
                    code="conflicting_update",
                    message=(
                        f"attempt {a['id']!r} 已存在但 action 不同；"
                        f"换一个 attempt.id 或恢复原 action"
                    ),
                )
            )
            continue
        merged[a["id"]] = a
    return list(merged.values()), conflicts


def _merge_params(old: list[dict], new: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {p["name"]: dict(p) for p in old}
    for p in new:
        if p["name"] in merged:
            if p.get("description"):
                merged[p["name"]]["description"] = p["description"]
        else:
            merged[p["name"]] = dict(p)
    return list(merged.values())


def _merge_tests(
    old: list[dict], new: list[dict], base_path: str
) -> tuple[list[dict], list[MergeConflict]]:
    merged: dict[str, dict] = {t["id"]: t for t in old}
    conflicts: list[MergeConflict] = []
    for idx, t in enumerate(new):
        existing = merged.get(t["id"])
        if existing is not None and existing.get("action") != t.get("action"):
            conflicts.append(
                MergeConflict(
                    path=f"{base_path}/{idx}/action",
                    code="conflicting_update",
                    message=(
                        f"test {t['id']!r} 已存在但 action 不同；"
                        f"换一个 test.id 或恢复原 action"
                    ),
                )
            )
            continue
        merged[t["id"]] = t
    return list(merged.values()), conflicts


def _content_hash(state: dict) -> str:
    clone = {k: v for k, v in state.items() if k != "revision"}
    return hashlib.sha256(
        json.dumps(clone, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def submission_hash(submit: SubmitInput, base_revision: str | None) -> str:
    payload = {
        "baseRevision": base_revision,
        "upserts": [u.model_dump() for u in submit.upserts],
        "retireIds": list(submit.retireIds),
        "apis": [a.model_dump() for a in submit.apis],
        "guidance": submit.guidance.model_dump() if submit.guidance else None,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def new_revision() -> str:
    return str(uuid.uuid4())


def is_reserved_id(value: str) -> bool:
    return any(value.startswith(prefix) for prefix in RESERVED_ID_PREFIXES)
