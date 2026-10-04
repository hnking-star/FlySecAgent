"""结构化请求日志。

每次 HTTP 请求产出一行 JSON，字段：
  ts / request_id / route / session_id / token_fp / status / duration_ms

不记录 body 原文。
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any

_LOGGER_NAME = "flysec.request"


def setup_logging() -> None:
    """为 request logger 挂一个 JSON formatter 到 stdout。幂等。

    保留 propagate=True 让 pytest caplog 等上层 handler 可以捕获。
    """
    logger = logging.getLogger(_LOGGER_NAME)
    if not any(isinstance(h, _JsonHandler) for h in logger.handlers):
        handler = _JsonHandler(sys.stdout)
        handler.setFormatter(_JsonFormatter())
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)


class _JsonHandler(logging.StreamHandler):
    """标识用，便于 setup_logging 幂等去重。"""



def log_request(fields: dict[str, Any]) -> None:
    logging.getLogger(_LOGGER_NAME).info("request", extra={"fields": fields})


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        fields = getattr(record, "fields", {}) or {}
        payload = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            **fields,
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
