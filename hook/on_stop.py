"""Stop Hook：主 Agent 一轮结束 → agent-turn/stop。"""

from __future__ import annotations

import sys

from .common import (
    event_session_id,
    http_post,
    load_config,
    read_event,
)


def main() -> int:
    cfg = load_config()
    if cfg is None:
        return 0
    event = read_event()
    sid = event_session_id(event)
    if not sid:
        return 0
    http_post(cfg, "/control/agent-turn/stop", {"session_id": sid})
    return 0


if __name__ == "__main__":
    sys.exit(main())
