"""启动入口：python -m service。

Task 1：读配置、建 data 目录、生成/读取服务级 token。
Task 2：首启建表 + 启动时对账 pending_window_end。
Task 3：起 HTTP 服务，绑定 127.0.0.1。
Task 6：启用 lifespan 把 Scheduler + PiRunner 跑起来。
"""

from __future__ import annotations

import uvicorn

from .app import create_app, startup_recover
from .bootstrap import ensure_data_dir, ensure_service_token
from .config import load_config
from .auth import token_fingerprint


def main() -> None:
    cfg = load_config()
    ensure_data_dir(cfg.data_dir)
    token = ensure_service_token(cfg.data_dir)
    recovered = startup_recover(cfg)
    print(
        f"[flysec] data_dir={cfg.data_dir} port={cfg.port} "
        f"token_fp={token_fingerprint(token)} recovered={recovered}"
    )
    app = create_app(cfg, start_background=True)
    uvicorn.run(app, host="127.0.0.1", port=cfg.port, log_config=None)


if __name__ == "__main__":
    main()
