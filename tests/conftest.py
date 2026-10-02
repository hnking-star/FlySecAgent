"""pytest 共享 fixture。"""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from service.app import create_app
from service.config import Config


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(port=0, data_dir=tmp_path)


@pytest_asyncio.fixture
async def client(cfg) -> AsyncIterator[tuple[AsyncClient, "FastAPI"]]:
    app = create_app(cfg)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://127.0.0.1") as c:
        yield c, app
