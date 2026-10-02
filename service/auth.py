"""Token 鉴权。

两套 token：
- 服务级：data/.secret 里的那个，`/hook/*` `/control/*` `/web/*` 都用它
- 会话级：Python 内存注册表，每个 Pi 扩展进程一个，`/observer/*` 用它

会话级 token 不落盘：服务重启后全部失效，Pi 扩展必须重新向服务申请。
"""

from __future__ import annotations

import hashlib
import secrets
import threading


class TokenRegistry:
    """会话级 token 的内存注册表。进程生命周期内有效。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token_to_session: dict[str, str] = {}
        self._session_to_token: dict[str, str] = {}

    def issue(self, session_id: str) -> str:
        """为 session 生成新 token（64 字符 hex）。重复发放会撤销旧值。"""
        token = secrets.token_hex(32)
        with self._lock:
            old_token = self._session_to_token.get(session_id)
            if old_token is not None:
                self._token_to_session.pop(old_token, None)
            self._session_to_token[session_id] = token
            self._token_to_session[token] = session_id
        return token

    def resolve(self, token: str) -> str | None:
        """给定 token 返回绑定的 session_id；无匹配返回 None。"""
        with self._lock:
            return self._token_to_session.get(token)

    def revoke(self, session_id: str) -> None:
        """停用该 session 的 token。幂等。"""
        with self._lock:
            old_token = self._session_to_token.pop(session_id, None)
            if old_token is not None:
                self._token_to_session.pop(old_token, None)


def token_fingerprint(token: str) -> str:
    """`sha256(token)[:8]`。用于日志，不泄露原值。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
