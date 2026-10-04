"""/health 与中间件行为测试。"""

from __future__ import annotations

import json
import logging

import pytest


@pytest.mark.asyncio
async def test_health_no_auth(client):
    c, _ = client
    r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["version"]


@pytest.mark.asyncio
async def test_health_ignores_token(client):
    c, _ = client
    # /health 不校验 token，带错 token 也能通过
    r = await c.get("/health", headers={"X-FlySec-Token": "whatever"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_unknown_route_still_authorized(client):
    """未注册的 /hook/* 路由走到 404，但先过鉴权。无 token 应 401。"""
    c, _ = client
    r = await c.get("/hook/not-exist")
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_unknown_route_404_with_valid_token(client):
    c, app = client
    r = await c.get(
        "/hook/not-exist", headers={"X-FlySec-Token": app.state.service_token}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_control_health_stub(client):
    c, app = client
    r = await c.post(
        "/control/health-stub",
        headers={"X-FlySec-Token": app.state.service_token},
    )
    assert r.status_code == 200
    assert r.json()["stub"] == "control"


@pytest.mark.asyncio
async def test_log_contains_request_fields(client, caplog):
    c, _ = client
    with caplog.at_level(logging.INFO, logger="flysec.request"):
        await c.get("/health")

    records = [r for r in caplog.records if r.name == "flysec.request"]
    assert records, "no flysec.request log emitted"
    fields = getattr(records[-1], "fields", {})
    assert fields["route"] == "GET /health"
    assert fields["status"] == 200
    assert "duration_ms" in fields
    assert "request_id" in fields


@pytest.mark.asyncio
async def test_log_contains_session_id_for_observer(client, caplog):
    c, app = client
    token = app.state.token_registry.issue("sess-log")
    with caplog.at_level(logging.INFO, logger="flysec.request"):
        await c.post(
            "/observer/context",
            headers={"X-FlySec-Token": token},
            json={"mode": "summary"},
        )

    records = [r for r in caplog.records if r.name == "flysec.request"]
    assert records
    fields = getattr(records[-1], "fields", {})
    assert fields.get("session_id") == "sess-log"
    assert len(fields.get("token_fp", "")) == 8
