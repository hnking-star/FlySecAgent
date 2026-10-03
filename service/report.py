"""Render a complete, human-readable Markdown report from one blackboard snapshot."""

from __future__ import annotations

from collections import Counter


def render_report(project: dict, observation: dict | None, state: dict | None) -> str:
    if observation is None or state is None:
        return "# FlySecAgent 观察报告\n\n尚未生成黑板。\n"

    assessments = state.get("assessments", [])
    apis = state.get("apis", [])
    guidance = state.get("guidance", {}) or {}
    statuses = Counter(item.get("status", "unknown") for item in assessments)
    untested = [api for api in apis if not api.get("tests")]

    lines = [
        "# FlySecAgent 观察报告",
        "",
        f"- 会话：{project['session_id']}",
        f"- 目标：{project['target']}",
        f"- 目的：{project['objective']}",
        f"- 版本：{state.get('revision') or observation['id']}",
        f"- 证据位置：record:{observation['end_record_id']}",
        "",
        "## 探索概览",
        "",
        f"- 判断：{len(assessments)}（" + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())) + "）",
        f"- API：{len(apis)}，其中 {len(untested)} 条尚无测试记录",
    ]
    confirmed = set(guidance.get("confirmedIds", []))
    angles = set(guidance.get("angleIds", []))

    lines.extend(["", "## 已确认判断", ""])
    selected = [a for a in assessments if a.get("id") in confirmed] or assessments
    if not selected:
        lines.append("（无）")
    for item in selected:
        lines.extend(_assessment_lines(item))

    lines.extend(["", "## 待验证方向", ""])
    pending = [a for a in assessments if a.get("id") in angles]
    if not pending:
        lines.append("（无）")
    for item in pending:
        lines.append(f"- [{item.get('id')}] {item.get('subject')}：{item.get('conclusion')}")

    lines.extend(["", "## API 台账", ""])
    if not apis:
        lines.append("（无）")
    for api in apis:
        lines.extend([f"### {api.get('endpoint')} · {api.get('purpose')}", ""])
        params = api.get("parameters", [])
        lines.append("- 参数：" + (", ".join(p.get("name", "?") for p in params) if params else "无已知参数"))
        tests = api.get("tests", [])
        if not tests:
            lines.append("- 测试：尚无测试记录")
        for test in tests:
            refs = ", ".join(f"record:{rid}" for rid in test.get("record_ids", []))
            lines.append(f"- [{test.get('id')}] {test.get('action')} → {test.get('result')}（{refs}）")
        lines.append("")

    tension = guidance.get("tension", [])
    if tension:
        lines.extend(["## 冲突或待对账", "", *(f"- {value}" for value in tension), ""])

    retired = state.get("retired", [])
    if retired:
        lines.extend(["## 已存档判断", ""])
        lines.extend(f"- [{item.get('id')}] {item.get('subject')}：{item.get('conclusion')}" for item in retired)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _assessment_lines(item: dict) -> list[str]:
    lines = [
        f"### {item.get('subject')}（{item.get('id')}）",
        "",
        f"- 状态：{item.get('status')}",
        f"- 结论：{item.get('conclusion')}",
        f"- 依据：{item.get('basis')}",
        f"- 不确定性：{item.get('uncertainty') or '无'}",
        "- 证据：" + ", ".join(item.get("evidenceRefs", [])),
    ]
    for attempt in item.get("attempts", []):
        lines.append(f"- 尝试 [{attempt.get('id')}]：{attempt.get('action')} → {attempt.get('result')}")
    lines.append("")
    return lines
