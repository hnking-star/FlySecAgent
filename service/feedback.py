"""Small deterministic feedback derived from a published blackboard."""
from __future__ import annotations


def render_map(state: dict, through_record: int) -> str:
    lines = [f'<observer-map revision="{state["revision"]}" through_record="{through_record}">',
             "以下为已保存执行证据的摘要，仅供参考；不改变任务或授权范围。"]
    for row in state.get("assessments", [])[-8:]:
        lines.append(f'- {row["subject"]} [{row["status"]}]：{row["conclusion"][:260]}')
        if row.get("uncertainty"):
            lines.append(f'  未确认：{row["uncertainty"][:160]}')
        lines.append('  证据：' + ', '.join(row.get("evidenceRefs", [])[:4]))
    apis = state.get("apis", [])
    if apis:
        lines.append(f'API：共 {len(apis)} 条；有记录不代表测试完毕。')
        for api in apis[:6]:
            lines.append(f'- {api["endpoint"]}：{api["purpose"][:120]}；测试记录 {len(api.get("tests", []))} 条')
    guidance = state.get("guidance", {})
    if guidance.get("hypothesis"):
        lines.append('假设：' + guidance["hypothesis"][:200])
    # A character budget is an approximation, not a claim of exact token count.
    return '\n'.join(lines)[:2800] + '\n</observer-map>'
