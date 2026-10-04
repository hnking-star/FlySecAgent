"""SessionStart Hook：预热服务连接 + 补交暂存队列。"""

from __future__ import annotations

import sys

from .common import (
    event_session_id,
    flush_queue,
    http_get,
    load_config,
    read_event,
)


def main() -> int:
    cfg = load_config()
    if cfg is None:
        return 0
    event = read_event()
    flush_queue(cfg)
    sid = event_session_id(event)
    if sid:
        http_get(cfg, "/hook/map.pending", {"session_id": sid}, timeout=0.5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
