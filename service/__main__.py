"""启动入口：python -m service。

Task 1：读配置、建 data 目录、生成/读取服务级 token。
Task 2：首启建表 + 启动时对账 pending_window_end。

HTTP 服务与路由在 Task 3 加。
"""

from __future__ import annotations

from . import db, observation
from .bootstrap import ensure_data_dir, ensure_service_token
from .config import load_config


def main() -> None:
    cfg = load_config()
    ensure_data_dir(cfg.data_dir)
    token = ensure_service_token(cfg.data_dir)
    db.init_db(cfg.data_dir)

    recovered = 0
    conn = db.connect(cfg.data_dir)
    try:
        rows = conn.execute(
            "SELECT session_id FROM projects WHERE pending_window_end IS NOT NULL"
        ).fetchall()
        for row in rows:
            observation.recover_pending(conn, row["session_id"])
            recovered += 1
    finally:
        conn.close()

    print(
        f"[flysec] data_dir={cfg.data_dir} port={cfg.port} "
        f"token_fp={token[:8]} recovered={recovered}"
    )


if __name__ == "__main__":
    main()
