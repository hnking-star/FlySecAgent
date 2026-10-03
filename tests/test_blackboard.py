"""blackboard.merge 的纯函数单元测试。"""

from __future__ import annotations

from service import blackboard
from service.schemas import (
    ApiEntry,
    ApiParam,
    ApiTest,
    Assessment,
    Attempt,
    Guidance,
    SubmitInput,
)


def _asmt(**overrides):
    base = dict(
        id="recon-auth",
        subject="auth recon",
        status="tried-hit",
        role="direction",
        conclusion="found /api/login",
        basis="record:1 列出路径",
        uncertainty=None,
        evidenceRefs=["record:1"],
        attempts=[Attempt(id="crawl", action="爬 /api", result="ok",
                          evidenceRefs=["record:1"])],
        dependsOn=[],
        apiIds=[],
    )
    base.update(overrides)
    return Assessment(**base)


def _submit(**overrides) -> SubmitInput:
    base = dict(baseRevision=None, upserts=[], retireIds=[], apis=[], guidance=None)
    base.update(overrides)
    return SubmitInput(**base)


def test_initial_state_shape():
    s = blackboard.initial_state()
    assert s["schema_version"] == 1
    assert s["revision"] is None
    assert s["assessments"] == []
    assert s["retired"] == []
    assert s["apis"] == []
    assert s["guidance"]["angleIds"] == []


def test_merge_adds_new_assessment():
    outcome = blackboard.merge(blackboard.initial_state(), _submit(upserts=[_asmt()]))
    assert not outcome.unchanged
    ids = [a["id"] for a in outcome.new_state["assessments"]]
    assert ids == ["recon-auth"]


def test_merge_updates_existing_assessment():
    old = blackboard.initial_state()
    first = blackboard.merge(old, _submit(upserts=[_asmt()])).new_state

    updated = _asmt(conclusion="refined", attempts=[
        Attempt(id="crawl", action="爬 /api", result="still ok", evidenceRefs=["record:1"]),
        Attempt(id="headers", action="headers", result="ok", evidenceRefs=["record:1"]),
    ])
    outcome = blackboard.merge(first, _submit(upserts=[updated]))
    assert not outcome.unchanged
    row = outcome.new_state["assessments"][0]
    assert row["conclusion"] == "refined"
    ids = sorted(a["id"] for a in row["attempts"])
    assert ids == ["crawl", "headers"]


def test_merge_retires_assessment_moves_to_retired():
    old = blackboard.merge(
        blackboard.initial_state(), _submit(upserts=[_asmt()])
    ).new_state
    outcome = blackboard.merge(old, _submit(retireIds=["recon-auth"]))
    assert outcome.new_state["assessments"] == []
    assert outcome.new_state["retired"][0]["id"] == "recon-auth"


def test_merge_apis_dedup_by_id_params_union():
    api1 = ApiEntry(
        id="api-1", endpoint="GET /api/x", purpose="x",
        parameters=[ApiParam(name="a")],
        tests=[ApiTest(id="t1", action="run", result="ok", record_ids=[1])],
    )
    api1_extra = ApiEntry(
        id="api-1", endpoint="GET /api/x", purpose="x",
        parameters=[ApiParam(name="a", description="first"), ApiParam(name="b")],
        tests=[ApiTest(id="t2", action="run2", result="ok", record_ids=[2])],
    )
    state = blackboard.merge(
        blackboard.initial_state(), _submit(apis=[api1])
    ).new_state
    state = blackboard.merge(state, _submit(apis=[api1_extra])).new_state

    api = state["apis"][0]
    assert sorted(p["name"] for p in api["parameters"]) == ["a", "b"]
    assert [p for p in api["parameters"] if p["name"] == "a"][0]["description"] == "first"
    assert sorted(t["id"] for t in api["tests"]) == ["t1", "t2"]


def test_node_api_links_survive_later_api_test_append():
    node = _asmt(apiIds=["api-1"])
    discovered = ApiEntry(
        id="api-1", endpoint="GET /api/x", purpose="x", parameters=[], tests=[]
    )
    state = blackboard.merge(
        blackboard.initial_state(), _submit(upserts=[node], apis=[discovered])
    ).new_state

    tested = ApiEntry(
        id="api-1", endpoint="GET /api/x", purpose="x", parameters=[],
        tests=[ApiTest(id="idor", action="replace id", result="blocked", record_ids=[2])],
    )
    state = blackboard.merge(state, _submit(apis=[tested])).new_state

    assert state["assessments"][0]["apiIds"] == ["api-1"]
    assert [test["id"] for test in state["apis"][0]["tests"]] == ["idor"]


def test_merge_detects_conflicting_attempt():
    old = blackboard.merge(
        blackboard.initial_state(), _submit(upserts=[_asmt()])
    ).new_state
    conflict = _asmt(attempts=[
        Attempt(id="crawl", action="DIFFERENT action", result="still ok",
                evidenceRefs=["record:1"]),
    ])
    outcome = blackboard.merge(old, _submit(upserts=[conflict]))
    assert outcome.conflicts
    assert outcome.conflicts[0].code == "conflicting_update"


def test_normalize_endpoint():
    norm, warnings = blackboard.normalize_endpoint("  post /api/x/?a=1 ")
    assert norm == "POST /api/x"
    assert warnings == []

    norm, warnings = blackboard.normalize_endpoint("WEIRD /x")
    assert norm == "WEIRD /x"
    assert any("unknown" in w for w in warnings)

    # 根路径保留
    norm, _ = blackboard.normalize_endpoint("get /")
    assert norm == "GET /"


def test_content_hash_ignores_revision():
    s1 = blackboard.initial_state()
    s2 = {**blackboard.initial_state(), "revision": "x"}
    assert blackboard._content_hash(s1) == blackboard._content_hash(s2)


def test_merge_unchanged_when_no_delta():
    outcome = blackboard.merge(blackboard.initial_state(), _submit())
    assert outcome.unchanged


def test_guidance_filters_dangling_ids():
    old = blackboard.merge(
        blackboard.initial_state(), _submit(upserts=[_asmt()])
    ).new_state
    # 带 guidance 指向两个节点，一个已不存在
    old["guidance"] = {
        "hypothesis": None, "lock": None,
        "angleIds": ["recon-auth", "ghost"],
        "confirmedIds": ["ghost"],
        "tension": [],
    }
    outcome = blackboard.merge(old, _submit())
    assert outcome.new_state["guidance"]["angleIds"] == ["recon-auth"]
    assert outcome.new_state["guidance"]["confirmedIds"] == []


def test_guidance_overrides_when_provided():
    old = blackboard.merge(
        blackboard.initial_state(), _submit(upserts=[_asmt()])
    ).new_state
    g = Guidance(hypothesis="h", lock="l",
                 angleIds=[], confirmedIds=["recon-auth"], tension=[])
    outcome = blackboard.merge(old, _submit(guidance=g))
    assert outcome.new_state["guidance"]["hypothesis"] == "h"


def test_submission_hash_stable_regardless_of_order():
    api_a = ApiEntry(
        id="a", endpoint="GET /x", purpose="x",
        parameters=[ApiParam(name="n")],
        tests=[ApiTest(id="t", action="run", result="ok", record_ids=[1])],
    )
    s1 = _submit(baseRevision="r1", apis=[api_a])
    s2 = _submit(baseRevision="r1", apis=[api_a])
    assert blackboard.submission_hash(s1, "r1") == blackboard.submission_hash(s2, "r1")


def test_is_reserved_id():
    assert blackboard.is_reserved_id("host-flag-1")
    assert not blackboard.is_reserved_id("recon-auth")
