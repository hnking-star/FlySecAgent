"""Read-only projections. Graph, API ledger and reports share one memory snapshot."""
from __future__ import annotations

from .blackboard import active_facts, prepare_state


def memory_view(stored: dict, target: str = "") -> dict:
    state = prepare_state(stored, target)
    facts = active_facts(state)
    by_fact = {f["id"]: f for f in state["facts"]}
    nodes = []
    for topic in state["topics"]:
        tid = topic["id"]
        own_facts = [f for f in facts if f["topic_id"] == tid]
        own_tests = [t for t in state["tests"] if t["topic_id"] == tid]
        own_questions = [q for q in state["questions"] if q["topic_id"] == tid and q["status"] == "open"]
        origins = [by_fact[x] for x in topic["origin_fact_ids"] if x in by_fact]
        nodes.append({"id": tid, "title": topic["title"], "summary": topic["summary"], "api_ids": topic["api_ids"],
                      "parents": list(dict.fromkeys(f["topic_id"] for f in origins if f["topic_id"] != tid)),
                      "origin_facts": origins, "facts": own_facts, "tests": own_tests, "questions": own_questions,
                      "fact_count": sum(f["kind"] == "observation" for f in own_facts),
                      "hypothesis_count": sum(f["kind"] == "hypothesis" for f in own_facts),
                      "legacy_count": sum(f["kind"] == "legacy_summary" for f in own_facts),
                      "test_count": len(own_tests), "open_count": len(own_questions),
                      "execution_issues": sum(t["execution"] in {"error", "denied", "interrupted"} for t in own_tests)})
    apis = []
    for api in state["apis"]:
        tests = [t for t in state["tests"] if api["id"] in t.get("api_ids", [])]
        # record_ids is a display convenience, not a second stored test copy.
        apis.append({**api, "completed_tests": sum(t["execution"] == "completed" for t in tests), "execution_issues": sum(t["execution"] in {"error", "denied", "interrupted"} for t in tests), "tests": [{**t, "record_ids": t["evidence_ids"]} for t in tests]})
    return {"protocol": 2, "nodes": nodes, "apis": apis,
            "counts": {"topics": len(nodes), "observations": sum(f["kind"] == "observation" for f in facts),
                       "hypotheses": sum(f["kind"] == "hypothesis" for f in facts),
                       "legacy_notes": sum(f["kind"] == "legacy_summary" for f in facts),
                       "tests": len(state["tests"]), "open_questions": sum(q["status"] == "open" for q in state["questions"]),
                       "execution_issues": sum(t["execution"] in {"error", "denied", "interrupted"} for t in state["tests"])}}
