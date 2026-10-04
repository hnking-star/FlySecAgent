"""Memory reader: fixed windows, pagination, complete UTF-8 evidence, isolation."""
import json
import pytest
from service import db, observation


def headers(app,sid):
    return {'X-FlySec-Token':app.state.token_registry.issue(sid)}


def setup_project(cfg,sid='A',records=3):
    conn=db.connect(cfg.data_dir)
    try:
        conn.execute("INSERT INTO projects(session_id,target,objective,created_at) VALUES(?, 'https://fixture.test', 'test', '2026-10-04T00:00:00Z')",(sid,))
        ids=[]
        for i in range(records):
            cur=conn.execute("INSERT INTO tool_records(session_id,tool_name,tool_input_json,tool_response_json,metadata_json,received_at) VALUES(?, 'Read', '{}', ?, '{}','2026-10-04T00:00:01Z')",(sid,json.dumps({'stdout':'中文🙂'+str(i)},ensure_ascii=False)))
            ids.append(cur.lastrowid)
        observation.start_observation(conn,sid,'timer_5min')
        return ids
    finally:conn.close()


@pytest.mark.asyncio
async def test_summary_returns_memory_not_strategy(client):
    c,app=client;setup_project(app.state.cfg)
    r=await c.post('/memory/read',headers=headers(app,'A'),json={})
    assert r.status_code==200
    data=r.json();assert data['window']['record_count']==3 and data['memory']['schema_version']==2
    assert not {'assessments','guidance','session_id','project_id'} & set(data)


@pytest.mark.asyncio
async def test_pagination_keeps_fixed_window(client):
    c,app=client;ids=setup_project(app.state.cfg)
    conn=db.connect(app.state.cfg.data_dir)
    conn.execute("INSERT INTO tool_records(session_id,tool_name,tool_input_json,tool_response_json,metadata_json,received_at) VALUES('A','Read','{}','{}','{}','late')");conn.close()
    first=(await c.post('/memory/read',headers=headers(app,'A'),json={'mode':'records','limit':2})).json()
    assert [r['id'] for r in first['records']]==ids[:2] and first['has_more']
    last=(await c.post('/memory/read',headers=headers(app,'A'),json={'mode':'records','after_id':first['next_after_id'],'limit':2})).json()
    assert [r['id'] for r in last['records']]==ids[2:] and not last['has_more']
    future=await c.post('/memory/read',headers=headers(app,'A'),json={'mode':'record','record_id':ids[-1]+1})
    assert future.status_code==400


@pytest.mark.asyncio
async def test_utf8_roundtrip_and_ownership(client):
    c,app=client;ids=setup_project(app.state.cfg);setup_project(app.state.cfg,'B',0)
    fragments=[];offset=0
    while True:
        r=await c.post('/memory/read',headers=headers(app,'A'),json={'mode':'record','record_id':ids[0],'offset':offset,'length':7})
        assert r.status_code==200
        segment=r.json()['segment'];fragments.append(segment['data']);offset=segment['next_offset']
        if not segment['has_more']:break
    assert json.loads(''.join(fragments).split('\n---\n')[1])['stdout']=='中文🙂0'
    cross=await c.post('/memory/read',headers=headers(app,'B'),json={'mode':'record','record_id':ids[0]})
    assert cross.status_code==404


@pytest.mark.asyncio
async def test_invalid_fields_and_history_expansion(client):
    c,app=client;ids=setup_project(app.state.cfg)
    for body in [{'mode':'record'},{'length':0},{'session_id':'B'},{'project_id':'B'}]:
        r=await c.post('/memory/read',headers=headers(app,'A'),json=body)
        assert r.status_code in {400,403}
    r=await c.post('/memory/read',headers=headers(app,'A'),json={'mode':'state'})
    assert r.json()['schema_version']==2
