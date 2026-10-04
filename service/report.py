"""Human report: observations, executed tests, scope and unanswered questions."""
from __future__ import annotations

from .blackboard import active_facts, prepare_state

KINDS = {"observation": "观察记录（附证据）", "hypothesis": "解释/假设（未独立验证）", "legacy_summary": "历史摘要（未重新核验）"}
EXECUTION = {"completed": "已执行", "error": "执行报错", "denied": "本地调用被拒", "interrupted": "执行被中断", "unknown": "执行状态未确认"}
OUTCOMES = {"supports": "支持本次假设", "contradicts": "不支持本次假设", "inconclusive": "结果不可判定", "not_evaluated": "未评价目标"}


def refs(ids):
    return ", ".join(f"record:{i}" for i in ids) or "历史项未提供证据引用"


def render_report(project: dict, observation: dict | None, state: dict | None) -> str:
    if observation is None or state is None:
        return "# FlySecAgent 证据记忆报告\n\n尚未生成黑板。\n"
    state = prepare_state(state, project["target"])
    topics = {t["id"]: t for t in state["topics"]}
    tests = state["tests"]
    lines = ["# FlySecAgent 证据记忆报告", "", "## 目标与记录范围", "",
             f"- 会话：{project['session_id']}", f"- 目标：{project['target']}", f"- 目的：{project['objective']}",
             f"- 快照版本：{state.get('revision') or observation['id']}", f"- 快照截至：record:{observation['end_record_id']}",
             "- 本报告整理已有工具证据，不独立复测；观察与解释分开，不推断未记录的测试已完成。",
             "", "## 观察与解释", ""]
    for topic in state["topics"]:
        lines.extend([f"### {topic['title']}", "", topic.get("summary", ""), ""])
        for fact in [f for f in active_facts(state) if f["topic_id"] == topic["id"]]:
            lines.extend([f"- [{fact['id']}] {KINDS.get(fact['kind'], fact['kind'])}：{fact['statement']}",
                          f"  - 条件与边界：{fact.get('scope') or '仅限所引证据，未补充未知条件'}", f"  - 证据：{refs(fact.get('evidence_ids', []))}"])
    lines.extend(["", "## 已执行测试与执行问题", ""])
    for test in tests:
        lines.extend([f"### {test['id']} · {topics.get(test['topic_id'], {}).get('title', test['topic_id'])}",
                      f"- 动作：{test['action']}", f"- 返回：{test['result']}",
                      f"- 执行状态：{EXECUTION.get(test['execution'], test['execution'])}", f"- 本次结果：{OUTCOMES.get(test['outcome'], test['outcome'])}",
                      f"- 条件与边界：{test.get('scope') or '仅限所引证据条件'}", f"- 证据：{refs(test.get('evidence_ids', []))}", ""])
    lines.extend(["## API 与测试记录", ""])
    for api in state["apis"]:
        related = [t for t in tests if api["id"] in t.get("api_ids", [])]
        lines.extend([f"### {api['endpoint']} · {api['purpose']}",
                      "- 参数：" + (", ".join(p['name'] for p in api.get('parameters', [])) or "无已知参数"),
                      "- 关联主题：" + (", ".join(t['title'] for t in state['topics'] if api['id'] in t.get('api_ids', [])) or "无"),
                      f"- 已完成尝试：{sum(t['execution'] == 'completed' for t in related)}；已有记录：{len(related)}。记录数量不代表全面覆盖。",
                      *[f"- 测试 [{t['id']}]：{t['action']} → {t['result']}（{refs(t['evidence_ids'])}）" for t in related], ""])
    lines.extend(["## 未完成与未确认事项", ""])
    pending = [q for q in state["questions"] if q["status"] == "open"]
    lines.extend([f"- [{q['id']}] {q['question']}（{refs(q.get('evidence_ids', []))}）" for q in pending] or ["无已记录的未确认问题；这不证明所有测试已完成。"])
    lines.extend(["", "## 观察修正历史", ""])
    corrections = [f for f in state["facts"] if f.get("supersedes")]
    lines.extend([f"- {f['id']} 修正 {f['supersedes']}：{f['statement']}" for f in corrections] or ["无观察修正记录。"])
    lines.extend(["", "## 结果验收边界", "", "读取某个结果与靶场/平台验收是两个事件；没有平台回执时不得宣称已提交或已验收。", ""])
    return "\n".join(lines)
