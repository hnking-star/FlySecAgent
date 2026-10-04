"""Memory commit: ownership, references, immutable history, atomic cursor receipts."""
import json
import pytest
from service import db
from tests.test_curator_read import setup_project,headers


def payload(rid,**extra):
    return {'revision':None,'topics':[{'id':'auth','title':'Authentication','api_ids':['me']}],
            'facts':[{'id':'f1','topic_id':'auth','statement':'This GET returned 401.','scope':'anonymous GET /api/me only','evidence_ids':[rid]}],
            'apis':[{'id':'me','endpoint':'GET /api/me','purpose':'Profile','parameters':[{'name':'id'}],'evidence_ids':[rid]}],
            'tests':[{'id':'t1','topic_id':'auth','api_ids':['me'],'action':'Anonymous GET /api/me','result':'401','execution':'completed','outcome':'supports','evidence_ids':[rid]}],**extra}


@pytest.mark.asyncio
async def test_publish_receipt_and_exact_replay(client):
    c,app=client;ids=setup_project(app.state.cfg);h=headers(app,'A');body=payload(ids[0])
    first=await c.post('/memory/commit',headers=h,json=body)
    assert first.status_code==200 and first.json()['ok'] and first.json()['published']
    again=await c.post('/memory/commit',headers=h,json=body)
    assert again.json()==first.json()
    conn=db.connect(app.state.cfg.data_dir)
    p=conn.execute("SELECT * FROM projects WHERE session_id='A'").fetchone()
    state=json.loads(conn.execute('SELECT state_json FROM observations WHERE id=?',(p['current_observation_id'],)).fetchone()[0])
    assert state['schema_version']==2 and p['processed_record_id']==ids[-1] and p['pending_window_end'] is None
    assert 'tests' not in state['apis'][0]
    assert state['topics'][0]['api_ids']==['me'];conn.close()
    unchanged=await c.post('/memory/commit',headers=h,json={'revision':first.json()['revision']})
    assert unchanged.json()['ok'] and not unchanged.json()['published']


@pytest.mark.asyncio
@pytest.mark.parametrize('mutation,code',[
    ('stale','stale_revision'),('unknown','unknown_evidence'),('cross','unknown_evidence'),
    ('empty','empty_delta'),('duplicate','duplicate_id'),('missing_topic','unknown_topic'),('missing_api','unknown_api'),
])
async def test_validation_does_not_advance_cursor(client,mutation,code):
    c,app=client;ids=setup_project(app.state.cfg);other=setup_project(app.state.cfg,'B',1);body=payload(ids[0])
    if mutation=='stale':body['revision']='wrong'
    if mutation=='unknown':body['facts'][0]['evidence_ids']=[99999]
    if mutation=='cross':body['facts'][0]['evidence_ids']=other
    if mutation=='empty':body={'revision':None}
    if mutation=='duplicate':body['facts'].append(dict(body['facts'][0]))
    if mutation=='missing_topic':body['facts'][0]['topic_id']='missing'
    if mutation=='missing_api':body['topics'][0]['api_ids']=['missing']
    r=await c.post('/memory/commit',headers=headers(app,'A'),json=body)
    assert not r.json()['ok'] and code in {e['code'] for e in r.json()['errors']}
    conn=db.connect(app.state.cfg.data_dir);p=conn.execute("SELECT processed_record_id,pending_window_end,current_observation_id FROM projects WHERE session_id='A'").fetchone()
    assert p['processed_record_id']==0 and p['pending_window_end']==ids[-1] and p['current_observation_id'] is None;conn.close()


@pytest.mark.asyncio
async def test_local_failure_cannot_be_target_negative(client):
    c,app=client;ids=setup_project(app.state.cfg)
    conn=db.connect(app.state.cfg.data_dir);conn.execute('UPDATE tool_records SET tool_response_json=? WHERE id=?',(json.dumps({'stderr':'SyntaxError: no request was sent','stdout':''}),ids[0]));conn.close()
    r=await c.post('/memory/commit',headers=headers(app,'A'),json=payload(ids[0]))
    assert 'execution_mismatch' in {e['code'] for e in r.json()['errors']}
    body=payload(ids[0]);body['tests'][0].update(execution='error',outcome='not_evaluated')
    assert (await c.post('/memory/commit',headers=headers(app,'A'),json=body)).json()['ok']


@pytest.mark.asyncio
async def test_snapshot_correction_and_api_test_references(client):
    c,app=client;ids=setup_project(app.state.cfg);h=headers(app,'A')
    first=(await c.post('/memory/commit',headers=h,json=payload(ids[0]))).json()
    correction={'revision':first['revision'],'facts':[{'id':'f2','topic_id':'auth','kind':'observation','statement':'Only this unauthenticated request returned 401.','supersedes':'f1','evidence_ids':[ids[0]]}]}
    result=(await c.post('/memory/commit',headers=h,json=correction)).json();assert result['ok']
    current=(await c.get('/web/project/A')).json()
    assert len(current['state']['facts'])==2 and len(current['view']['nodes'][0]['facts'])==1
    assert len(current['view']['apis'][0]['tests'])==1
    old=(await c.get('/web/project/A',params={'observation_id':current['versions'][-1]['id']})).json()
    assert len(old['state']['facts'])==1


@pytest.mark.asyncio
async def test_schema_error_is_logged_and_old_protocol_retires(client):
    c,app=client;setup_project(app.state.cfg);h=headers(app,'A')
    r=await c.post('/memory/commit',headers=h,json={'revision':None,'facts':[{}]})
    assert r.status_code==400
    assert (await c.post('/observer/submit',headers=h,json={'baseRevision':None})).status_code==410
    for route in ['/memory/read','/memory/commit']:
        assert (await c.post(route,json={})).status_code==401
        assert (await c.post(route,headers=h,json={'session_id':'B'})).status_code==403
