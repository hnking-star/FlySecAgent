"""/control/agent-turn/* 的端到端测试。"""

from __future__ import annotations

import pytest
from service import db


def _hdr(app) -> dict[str, str]:
    return {"X-FlySec-Token": app.state.service_token}


async def _ensure(client, app, session_id: str = "sess-c") -> None:
    r = await client.post(
        "/hook/project.ensure",
        headers=_hdr(app),
        json={
            "session_id": session_id,
            "hint": {"target": "t", "objective": "o"},
        },
    )
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_agent_turn_begin_arms_scheduler(client):
    c, app = client
    await _ensure(c, app)
    r = await c.post(
        "/control/agent-turn/begin",
        headers=_hdr(app),
        json={"session_id": "sess-c"},
    )
    assert r.status_code == 200
    state = app.state.scheduler.snapshot("sess-c")
    assert state is not None
    assert state.agent_turn_active is True
    assert state.next_timer_fire_at is not None


@pytest.mark.asyncio
async def test_agent_turn_stop_fires_agent_stop(client):
    c, app = client
    await _ensure(c, app)
    await c.post(
        "/control/agent-turn/begin",
        headers=_hdr(app),
        json={"session_id": "sess-c"},
    )
    r = await c.post(
        "/control/agent-turn/stop",
        headers=_hdr(app),
        json={"session_id": "sess-c"},
    )
    assert r.status_code == 200
    state = app.state.scheduler.snapshot("sess-c")
    assert state.agent_turn_active is False


@pytest.mark.asyncio
async def test_agent_turn_requires_service_token(client):
    c, _ = client
    r = await c.post(
        "/control/agent-turn/begin",
        json={"session_id": "x"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_agent_turn_begin_404_without_project(client):
    c, app = client
    r = await c.post(
        "/control/agent-turn/begin",
        headers=_hdr(app),
        json={"session_id": "ghost"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_agent_turn_begin_409_when_disabled(client):
    c, app = client
    await _ensure(c, app)
    # 手动关闭 observation
    from service import db

    conn = db.connect(app.state.cfg.data_dir)
    try:
        conn.execute(
            "UPDATE projects SET observation_enabled = 0 WHERE session_id = 'sess-c'"
        )
    finally:
        conn.close()
    r = await c.post(
        "/control/agent-turn/begin",
        headers=_hdr(app),
        json={"session_id": "sess-c"},
    )
    assert r.status_code == 409
    assert r.json()["code"] == "observation_disabled"


@pytest.mark.asyncio
async def test_restart_activity_is_unknown_until_a_host_event(client):
    c,app=client;headers={'X-FlySec-Token':app.state.service_token}
    await c.post('/hook/project.ensure',headers=headers,json={'session_id':'new-live','hint':{'target':'fixture','objective':'local'}})
    p=(await c.get('/web/project/new-live')).json()['project']
    assert p['agent_activity_known']==0
    await c.post('/control/agent-turn/begin',headers=headers,json={'session_id':'new-live'})
    p=(await c.get('/web/project/new-live')).json()['project']
    assert p['agent_activity_known']==1 and p['agent_turn_active']==1


@pytest.mark.asyncio
async def test_paused_close_does_not_run_or_lose_final_request(client):
    c,app=client;h={'X-FlySec-Token':app.state.service_token};sid='paused-final'
    await c.post('/hook/project.ensure',headers=h,json={'session_id':sid,'hint':{'target':'fixture','objective':'local'}})
    assert (await c.post('/control/curator.pause',headers=h,json={'session_id':sid})).json()['ok']
    close=(await c.post('/control/observation.close',headers=h,json={'session_id':sid})).json()
    assert close['final_summary_pending']
    p=(await c.get('/web/project/'+sid)).json()['project']
    assert p['observer_paused']==1 and p['observation_enabled']==0 and p['final_summary_requested']==1
    conn=db.connect(app.state.cfg.data_dir)
    assert conn.execute('SELECT count(*) FROM observations WHERE session_id=?',(sid,)).fetchone()[0]==0;conn.close()
    assert (await c.post('/control/observation.open',headers=h,json={'session_id':sid})).json()['observer_paused']
