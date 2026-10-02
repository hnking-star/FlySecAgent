"""TokenRegistry + 鉴权 + 身份注入 的单元/端到端测试。"""

from __future__ import annotations

import pytest

from service.auth import TokenRegistry, token_fingerprint


# ---------- TokenRegistry 单元 ----------


def test_registry_issue_resolve_revoke():
    reg = TokenRegistry()
    t = reg.issue("sess-1")
    assert reg.resolve(t) == "sess-1"
    reg.revoke("sess-1")
    assert reg.resolve(t) is None


def test_registry_reissue_overrides_old_token():
    reg = TokenRegistry()
    t1 = reg.issue("sess-1")
    t2 = reg.issue("sess-1")
    assert t1 != t2
    assert reg.resolve(t1) is None
    assert reg.resolve(t2) == "sess-1"


def test_token_fingerprint_eight_hex():
    fp = token_fingerprint("abcdef")
    assert len(fp) == 8
    assert all(c in "0123456789abcdef" for c in fp)


# ---------- 端到端：鉴权 ----------


@pytest.mark.asyncio
async def test_hook_requires_service_token(client):
    c, _ = client
    r = await c.post(
        "/hook/project.ensure",
        json={"session_id": "s1", "hint": {"target": "t", "objective": "o"}},
    )
    assert r.status_code == 401
    assert r.json()["code"] == "unauthorized"


@pytest.mark.asyncio
async def test_hook_wrong_token(client):
    c, _ = client
    r = await c.post(
        "/hook/project.ensure",
        headers={"X-FlySec-Token": "wrong"},
        json={"session_id": "s1", "hint": {"target": "t", "objective": "o"}},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_hook_valid_token(client):
    c, app = client
    token = app.state.service_token
    r = await c.post(
        "/hook/project.ensure",
        headers={"X-FlySec-Token": token},
        json={"session_id": "s1", "hint": {"target": "t", "objective": "o"}},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "created": True}


@pytest.mark.asyncio
async def test_observer_requires_session_token(client):
    c, app = client
    # 用服务级 token 调 observer → 应 401（registry 查不到）
    token = app.state.service_token
    r = await c.post(
        "/observer/context",
        headers={"X-FlySec-Token": token},
        json={"mode": "summary"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_observer_valid_session_token(client):
    """带正确会话级 token 能过中间件。
    真实路由会走业务检查（本会话无 project → 404），说明鉴权没有拦下这条请求。
    """
    c, app = client
    token = app.state.token_registry.issue("sess-42")
    r = await c.post(
        "/observer/context",
        headers={"X-FlySec-Token": token},
        json={"mode": "summary"},
    )
    assert r.status_code == 404
    assert r.json()["code"] == "session_not_found"


# ---------- 端到端：身份注入 ----------


@pytest.mark.asyncio
async def test_observer_rejects_body_session_id(client):
    c, app = client
    token = app.state.token_registry.issue("sess-1")
    r = await c.post(
        "/observer/submit",
        headers={"X-FlySec-Token": token},
        json={"session_id": "evil", "baseRevision": None},
    )
    assert r.status_code == 403
    assert r.json()["code"] == "identity_field_not_allowed"


@pytest.mark.asyncio
async def test_observer_rejects_body_project_id(client):
    c, app = client
    token = app.state.token_registry.issue("sess-1")
    r = await c.post(
        "/observer/submit",
        headers={"X-FlySec-Token": token},
        json={"project_id": "any"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_observer_rejects_camel_case_identity(client):
    c, app = client
    token = app.state.token_registry.issue("sess-1")
    r = await c.post(
        "/observer/submit",
        headers={"X-FlySec-Token": token},
        json={"sessionId": "evil"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_observer_rejects_invalid_json_body(client):
    c, app = client
    token = app.state.token_registry.issue("sess-1")
    r = await c.post(
        "/observer/submit",
        headers={
            "X-FlySec-Token": token,
            "Content-Type": "application/json",
        },
        content=b"not-json",
    )
    assert r.status_code == 400
    assert r.json()["code"] == "schema_invalid"
