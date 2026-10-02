"""启动配置：从环境变量读取端口和数据目录。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEFAULT_PORT = 8787
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class Config:
    port: int
    data_dir: Path


def load_config() -> Config:
    port_env = os.environ.get("FLYSEC_PORT")
    port = int(port_env) if port_env else DEFAULT_PORT

    data_env = os.environ.get("FLYSEC_DATA_DIR")
    data_dir = Path(data_env).expanduser().resolve() if data_env else DEFAULT_DATA_DIR

    return Config(port=port, data_dir=data_dir)
