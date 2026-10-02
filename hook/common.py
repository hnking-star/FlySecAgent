"""Hook 脚本共享工具：HTTP 封装、token 读取、本地暂存队列、stdin/stdout 协议。

设计原则：
- 永远不抛未捕获异常；错误走 stderr 一行日志。
- HTTP 调用默认 1 秒超时，不阻塞 Codex。
- record.ingest 失败落 queue；其他端点失败直接丢。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sys
import time
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib import error, parse, request

_logger = logging.getLogger("flysec.hook")
if not _logger.handlers:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("[flysec-hook] %(message)s"))
    _logger.addHandler(handler)
    _logger.setLevel(logging.INFO)
    _logger.propagate = False

DEFAULT_API = "http://127.0.0.1:8787"
DEFAULT_TIMEOUT = 0.5
QUEUE_FILENAME = "pending.jsonl"
QUEUE_MAX_BYTES = 10 * 1024 * 1024


@dataclass
class HookConfig:
    api_base: str
    data_dir: Path
    token: str
    deadline: float = field(default_factory=lambda: time.monotonic() + 2.0)

    @property
    def queue_dir(self) -> Path:
        return self.data_dir / "queue"


def load_config() -> HookConfig | None:
    """从环境变量读配置；缺失或 token 不可读则返回 None。"""
    api_base = os.environ.get("FLYSEC_API_BASE", DEFAULT_API).rstrip("/")
    data_dir_str = os.environ.get("FLYSEC_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data"))
    data_dir = Path(data_dir_str).expanduser().resolve()
    endpoint = parse.urlsplit(api_base)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost", "::1"}:
        _logger.warning("service address must be local HTTP")
        return None

    secret = data_dir / ".secret"
    if not secret.exists():
        _logger.warning("secret file missing at %s; hook will no-op", secret)
        return None
    try:
        token = secret.read_text().strip()
    except OSError as exc:
        _logger.warning("cannot read %s: %s", secret, exc)
        return None
    if not token:
        _logger.warning("secret file %s is empty", secret)
        return None
    return HookConfig(api_base=api_base, data_dir=data_dir, token=token)


def read_event() -> dict[str, Any]:
    """从 stdin 读一行或整个 JSON；非 JSON 返回 {}。"""
    try:
        raw = sys.stdin.read()
    except Exception as exc:
        _logger.warning("stdin read failed: %s", exc)
        return {}
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
        _logger.warning("event payload is not a dict: %r", type(parsed).__name__)
        return {}
    except json.JSONDecodeError as exc:
        _logger.warning("invalid event json: %s", exc)
        return {}


def write_additional_context(hook_event: str, context: str) -> None:
    """按 Codex Hook 契约输出 hookSpecificOutput 到 stdout。"""
    payload = {
        "hookSpecificOutput": {
            "hookEventName": hook_event,
            "additionalContext": context,
        }
    }
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def _request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict | None]:
    req = request.Request(url, data=body, method=method, headers=headers)
    try:
        # Local credentials must never follow an HTTP redirect or a system proxy.
        class NoRedirect(request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except error.HTTPError as http_err:
        try:
            raw = http_err.read()
        except Exception:
            raw = b""
        status = http_err.code
    except (error.URLError, TimeoutError, OSError) as exc:
        _logger.warning("http %s %s failed: %s", method, url, exc)
        return 0, None

    data: dict | None = None
    if raw:
        try:
            parsed = json.loads(raw.decode("utf-8"))
            data = parsed if isinstance(parsed, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
    return status, data


def http_post(
    cfg: HookConfig, path: str, body: dict, *, timeout: float = DEFAULT_TIMEOUT
) -> tuple[int, dict | None]:
    timeout = min(timeout, cfg.deadline - time.monotonic())
    if timeout <= 0:
        return 0, None
    url = f"{cfg.api_base}{path}"
    data = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-FlySec-Token": cfg.token,
    }
    return _request("POST", url, headers, data, timeout)


def http_get(
    cfg: HookConfig,
    path: str,
    params: dict | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> tuple[int, dict | None]:
    timeout = min(timeout, cfg.deadline - time.monotonic())
    if timeout <= 0:
        return 0, None
    url = f"{cfg.api_base}{path}"
    if params:
        url = f"{url}?{parse.urlencode(params)}"
    headers = {"X-FlySec-Token": cfg.token}
    return _request("GET", url, headers, None, timeout)


# ---------------------------------------------------------------------------
# 本地暂存队列
# ---------------------------------------------------------------------------


def _queue_path(cfg: HookConfig) -> Path:
    return cfg.queue_dir / QUEUE_FILENAME


@contextmanager
def _queue_lock(cfg: HookConfig, name: str, wait: float = 0.15):
    cfg.queue_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (cfg.queue_dir / name).open("a") as fh:
        until = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= until:
                    raise TimeoutError("queue busy")
                time.sleep(0.005)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def enqueue_ingest(cfg: HookConfig, payload: dict) -> None:
    """Keep failed records durably; no automatic rotation/deletion of evidence."""
    try:
        with _queue_lock(cfg, ".write.lock"):
            path = _queue_path(cfg)
            with path.open("a", encoding="utf-8") as fh:
                os.chmod(path, 0o600)
                fh.write(json.dumps({"ts": _now_iso(), "payload": payload}, ensure_ascii=False) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
    except OSError:
        _logger.warning("queue write failed; event was not saved")


def flush_queue(cfg: HookConfig, *, max_items: int = 10) -> int:
    """Bound replay time and attempts; keep rejected records for inspection/retry."""
    path = _queue_path(cfg)
    if not path.exists():
        return 0
    delivered = 0
    try:
        # Never wait for another flusher; appends use a different short-lived lock.
        with _queue_lock(cfg, ".flush.lock", wait=0):
            with _queue_lock(cfg, ".write.lock"):
                lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            accepted: list[str] = []
            replay_until = min(cfg.deadline - 0.5, time.monotonic() + 0.75)
            for line in lines[:max_items]:
                if time.monotonic() >= replay_until:
                    break
                try:
                    row = json.loads(line)
                    payload = row["payload"]
                except (ValueError, KeyError, TypeError):
                    break  # Preserve malformed evidence; report rather than discard.
                status, _ = http_post(cfg, "/hook/record.ingest", payload,
                                      timeout=max(0.01, replay_until-time.monotonic()))
                if not 200 <= status < 300:
                    break
                accepted.append(line)
                delivered += 1
            if accepted:
                with _queue_lock(cfg, ".write.lock"):
                    current = path.read_text(encoding="utf-8").splitlines(keepends=True)
                    for line in accepted:
                        if line in current:
                            current.remove(line)
                    tmp = path.with_suffix(f".{os.getpid()}.tmp")
                    with tmp.open("w", encoding="utf-8") as fh:
                        os.chmod(tmp, 0o600)
                        fh.writelines(current)
                        fh.flush()
                        os.fsync(fh.fileno())
                    os.replace(tmp, path)
    except (OSError, UnicodeError):
        pass  # Another hook owns replay, or data remains on disk for a later try.
    return delivered


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------------------
# 便捷：Codex 事件字段抽取
# ---------------------------------------------------------------------------


def event_session_id(event: dict) -> str | None:
    for key in ("session_id", "sessionId", "session"):
        v = event.get(key)
        if isinstance(v, str) and v:
            return v
    return None


def deliver_pending_map(cfg: HookConfig, hook_event: str, session_id: str) -> None:
    """查新 map，若有未交付版本就 additionalContext 注入并 ack。"""
    status, body = http_get(cfg, "/hook/map.pending", {"session_id": session_id})
    if status != 200 or not body:
        return
    revision = body.get("revision")
    map_text = body.get("map_text")
    if not revision or not map_text:
        return
    write_additional_context(hook_event, str(map_text))
    http_post(
        cfg,
        "/hook/map.ack",
        {"session_id": session_id, "revision": revision},
    )
