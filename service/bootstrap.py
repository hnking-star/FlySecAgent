"""启动初始化：确保 data 目录与服务级 token 文件存在。"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

SECRET_FILENAME = ".secret"


def ensure_data_dir(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(data_dir, 0o700)
    except PermissionError:
        # Running under a managed environment that owns dir perms; non-fatal.
        pass


def ensure_service_token(data_dir: Path) -> str:
    """返回服务级 token（64 字符十六进制）；不存在时首启生成。

    用于 /hook/* 与 /control/* 鉴权。文件权限强制 0600。
    """
    path = data_dir / SECRET_FILENAME
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token

    token = secrets.token_hex(32)  # 32 bytes → 64 hex chars
    path.write_text(token)
    os.chmod(path, 0o600)
    return token
