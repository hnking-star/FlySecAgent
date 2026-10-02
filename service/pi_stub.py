"""占位子进程：模拟 Pi 扩展的行为。

Task 7 用真的 Node/TS 版本替换 PiRunner 的启动命令，本模块就不再被调用。

协议：
  stdin  (服务→Pi): {"op":"run_observation","trigger":"..."}  / {"op":"shutdown"}
  stdout (Pi→服务): {"op":"ready"} / {"op":"run_started","trigger":...}
                    {"op":"run_done","ok":bool,"revision":..?,"errors":..?}

本 stub 收到 run_observation：
  1. 调 /observer/context (mode=summary) 读 revision
  2. 发空 submit → unchanged
  3. 回报 run_done
"""

from __future__ import annotations

import json
import os
import sys
import time

import httpx


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _recv() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return _recv()
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return None


def _run_one(client: httpx.Client, token: str, trigger: str) -> dict:
    headers = {"X-FlySec-Token": token}
    ctx = client.post(
        "/observer/context", headers=headers, json={"mode": "summary"}
    )
    if ctx.status_code != 200:
        return {"ok": False, "errors": [{"path": "/", "code": "context_failed",
                                          "message": ctx.text}]}
    base = ctx.json().get("blackboard", {}).get("revision")
    r = client.post(
        "/observer/submit",
        headers=headers,
        json={"baseRevision": base},
    )
    body = r.json() if r.content else {}
    return body


def main() -> int:
    token = os.environ.get("FLYSEC_TOKEN")
    api = os.environ.get("FLYSEC_API")
    if not token or not api:
        print("missing FLYSEC_TOKEN / FLYSEC_API", file=sys.stderr)
        return 1

    _send({"op": "ready"})

    with httpx.Client(base_url=api, timeout=30.0) as client:
        while True:
            msg = _recv()
            if msg is None:
                return 0
            op = msg.get("op")
            if op == "shutdown":
                return 0
            if op != "run_observation":
                continue

            trigger = msg.get("trigger", "unknown")
            _send({"op": "run_started", "trigger": trigger})

            try:
                result = _run_one(client, token, trigger)
            except Exception as exc:  # noqa: BLE001
                _send({"op": "run_done", "ok": False,
                       "errors": [{"path": "/", "code": "stub_error",
                                   "message": str(exc)}]})
                continue

            _send({
                "op": "run_done",
                "ok": bool(result.get("ok")),
                "revision": result.get("revision"),
                "errors": result.get("errors"),
                "unchanged": result.get("unchanged"),
            })


if __name__ == "__main__":
    sys.exit(main())
