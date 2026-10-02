"""启动入口：python -m service。

Task 1 只做：读配置、建 data 目录、生成/读取服务级 token，打印指纹。
HTTP 服务与路由在 Task 3 加。
"""

from __future__ import annotations

from .bootstrap import ensure_data_dir, ensure_service_token
from .config import load_config


def main() -> None:
    cfg = load_config()
    ensure_data_dir(cfg.data_dir)
    token = ensure_service_token(cfg.data_dir)
    print(
        f"[flysec] data_dir={cfg.data_dir} port={cfg.port} "
        f"token_fp={token[:8]}"
    )


if __name__ == "__main__":
    main()
