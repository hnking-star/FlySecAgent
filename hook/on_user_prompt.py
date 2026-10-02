"""UserPromptSubmit Hook：开启主 Agent 回合 + 交付未交付的新 map。

不采集用户 prompt 文本。只借 UserPromptSubmit 作为"回合开始"的触发器。
"""

from __future__ import annotations

import sys

from .common import (
    deliver_pending_map,
    event_session_id,
    flush_queue,
    http_post,
    load_config,
    read_event,
)


HOOK_EVENT = "UserPromptSubmit"


def main() -> int:
    cfg = load_config()
    if cfg is None:
        return 0
    event = read_event()
    flush_queue(cfg)
    sid = event_session_id(event)
    if not sid:
        return 0

    # agent-turn/begin：404 / 409 时静默跳过（用户未开启观察或已关闭）
    http_post(cfg, "/control/agent-turn/begin", {"session_id": sid})

    deliver_pending_map(cfg, HOOK_EVENT, sid)
    return 0


if __name__ == "__main__":
    sys.exit(main())
