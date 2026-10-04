"""Capability guards for the v2 cutover; all evidence is synthetic and local."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from service import blackboard, db, observation
from service.export_schemas import generate
from service.feedback import render_digest
from tests.test_blackboard import delta, fact, topic, trial
from tests.test_curator_read import setup_project, headers
from tests.test_curator_commit import payload


def digest_body(text):
    return json.loads(text.split('<flysec-memory>\n', 1)[1].split('\n</flysec-memory>', 1)[0])


def test_generated_tool_schema_is_current_and_portable():
    exported = json.loads((Path(__file__).parents[1] / 'pi_ext/resources/curator-tools.schema.json').read_text())
    assert exported == generate()
    assert '$ref' not in json.dumps(exported) and '$defs' not in json.dumps(exported)
    assert exported['curator_commit']['additionalProperties'] is False


def test_unknown_method_refines_without_new_api_identity():
    api = dict(id='profile', endpoint='UNKNOWN /api/profile', purpose='Profile', evidence_ids=[1])
    old = blackboard.merge(blackboard.initial_state(), delta(apis=[api]), 'https://fixture.test').state
    refined = {**api, 'endpoint': 'GET /api/profile?id=1', 'evidence_ids': [2]}
    result = blackboard.merge(old, delta(apis=[refined]), 'https://fixture.test')
    assert not result.errors and len(result.state['apis']) == 1
    assert result.state['apis'][0]['evidence_ids'] == [1, 2]
    duplicate = blackboard.merge(old, delta(apis=[{**refined, 'id':'new'}]), 'https://fixture.test')
    assert any(e['code'] == 'reuse_api' for e in duplicate.errors)
    assert blackboard.normalize_endpoint('GET /A/', 'https://fixture.test') != blackboard.normalize_endpoint('GET /a', 'https://fixture.test')


def test_tests_are_immutable_and_topic_and_correction_cycles_rejected():
    old = blackboard.merge(blackboard.initial_state(), delta(topics=[topic()], facts=[fact()], tests=[trial()])).state
    changed = blackboard.merge(old, delta(tests=[trial(result='A different return')]))
    assert any(e['code'] == 'immutable_record' for e in changed.errors)
    circular = delta(topics=[topic(origin_fact_ids=['f_child']), topic('child', origin_fact_ids=['fact1'])],
                     facts=[{**fact('f_child'), 'topic_id':'child'}])
    assert any(e['code'] == 'cycle' for e in blackboard.merge(old, circular).errors)
    corrections = delta(facts=[fact('f2', supersedes='f3'), fact('f3', supersedes='f2')])
    assert any(e['code'] == 'correction_cycle' for e in blackboard.merge(old, corrections).errors)


def test_local_failures_do_not_remove_api_from_digest_todo():
    api = dict(id='profile', endpoint='GET /api/profile', purpose='Profile', evidence_ids=[1])
    old = blackboard.merge(blackboard.initial_state(), delta(topics=[topic(api_ids=['profile'])], apis=[api],
        tests=[trial(api_ids=['profile'], execution='error', outcome='not_evaluated')])).state
    assert digest_body(render_digest(old, 1))['api_todo'] == [{'id':'profile', 'endpoint':'GET /api/profile'}]
    completed = blackboard.merge(old, delta(tests=[trial('t2', api_ids=['profile'])])).state
    assert digest_body(render_digest(completed, 2))['api_todo'] == []


@pytest.mark.asyncio
async def test_empty_window_and_explicit_no_change_never_fake_publication(client):
    c, app = client
    for sid, records in [('empty', 0), ('metadata', 1)]:
        ids = setup_project(app.state.cfg, sid, records)
        body = {'revision':None}
        if records: body['unchanged_reason'] = 'Only repeated control metadata, no new observation or test.'
        result = (await c.post('/memory/commit', headers=headers(app, sid), json=body)).json()
        assert result['ok'] and not result['published'] and result['revision'] is None
        conn = db.connect(app.state.cfg.data_dir)
        row = conn.execute('SELECT * FROM projects WHERE session_id=?', (sid,)).fetchone()
        assert row['current_observation_id'] is None and row['pending_window_end'] is None
        assert row['processed_record_id'] == (ids[-1] if ids else 0)
        conn.close()


@pytest.mark.asyncio
async def test_future_evidence_cannot_publish_or_advance_window(client):
    c, app = client; ids = setup_project(app.state.cfg)
    conn = db.connect(app.state.cfg.data_dir)
    future = conn.execute("INSERT INTO tool_records(session_id,tool_name,tool_input_json,tool_response_json,metadata_json,received_at) VALUES('A','Read','{}','{}','{}','late')").lastrowid
    conn.close()
    result = (await c.post('/memory/commit', headers=headers(app, 'A'), json=payload(future))).json()
    assert not result['ok'] and any(e['code']=='record_after_window' for e in result['errors'])
    conn = db.connect(app.state.cfg.data_dir)
    row = conn.execute("SELECT * FROM projects WHERE session_id='A'").fetchone()
    assert row['processed_record_id'] == 0 and row['pending_window_end'] == ids[-1]
    conn.close()


@pytest.mark.asyncio
async def test_legacy_view_and_digest_never_mutate_archived_rows(client):
    c, app = client; ids = setup_project(app.state.cfg, records=1)
    old = {'schema_version':1, 'revision':'archived-v1', 'assessments':[
        {'id':'a', 'subject':'Historical check', 'conclusion':'Archived claim', 'status':'tried-hit',
         'evidenceRefs':['record:'+str(ids[0])], 'attempts':[], 'dependsOn':[], 'apiIds':[]}],
         'apis':[], 'guidance':{'lock':'OLD_STRATEGY_SENTINEL'}}
    conn = db.connect(app.state.cfg.data_dir)
    obs_id = conn.execute("SELECT id FROM observations WHERE session_id='A'").fetchone()[0]
    observation.commit_publish(conn, 'A', obs_id, json.dumps(old), '<heimdall-map>OLD_STRATEGY_SENTINEL</heimdall-map>')
    before = tuple(conn.execute('SELECT state_json,map_text FROM observations WHERE id=?', (obs_id,)).fetchone()); conn.close()
    data = (await c.get('/web/project/A')).json()
    assert data['state'] == old and data['view']['counts']['observations'] == 0
    assert data['view']['nodes'][0]['facts'][0]['kind'] == 'legacy_summary'
    pending = (await c.get('/hook/map.pending', params={'session_id':'A'}, headers={'X-FlySec-Token':app.state.service_token})).json()
    assert '<flysec-memory>' in pending['map_text'] and 'OLD_STRATEGY_SENTINEL' not in pending['map_text']
    conn = db.connect(app.state.cfg.data_dir)
    assert tuple(conn.execute('SELECT state_json,map_text FROM observations WHERE id=?', (obs_id,)).fetchone()) == before
    conn.close()


def test_database_upgrade_is_additive_and_preserves_three_tables(tmp_path):
    schema = (Path(__file__).parents[1] / 'service/schema.sql').read_text()
    old_schema = '\n'.join(line for line in schema.splitlines() if not any(x in line for x in ['agent_activity_known ', 'final_summary_requested ']))
    conn = sqlite3.connect(tmp_path / 'flysec.db'); conn.executescript(old_schema)
    conn.execute("INSERT INTO projects(session_id,target,objective,created_at) VALUES('old','fixture','local','now')")
    conn.execute("INSERT INTO tool_records(session_id,tool_name,tool_input_json,tool_response_json,metadata_json,received_at) VALUES('old','Read','{}','{\"untouched\":true}','{}','now')")
    conn.commit(); conn.close()
    db.init_db(tmp_path); db.init_db(tmp_path)
    conn = db.connect(tmp_path)
    assert {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")} == {'projects','tool_records','observations'}
    assert conn.execute('SELECT tool_response_json FROM tool_records').fetchone()[0] == '{"untouched":true}'
    assert conn.execute('SELECT agent_activity_known,final_summary_requested FROM projects').fetchone()[:] == (0, 0)
    conn.close()


@pytest.mark.asyncio
async def test_pause_begin_resume_restores_host_timer_and_closed_final_runs_once(client):
    c, app = client; h = {'X-FlySec-Token':app.state.service_token}; sid = 'paused-host'
    await c.post('/hook/project.ensure', headers=h, json={'session_id':sid, 'hint':{'target':'fixture', 'objective':'local'}})
    await c.post('/control/curator.pause', headers=h, json={'session_id':sid})
    await c.post('/control/agent-turn/begin', headers=h, json={'session_id':sid})
    await c.post('/control/curator.resume', headers=h, json={'session_id':sid})
    assert app.state.scheduler.snapshot(sid).agent_turn_active
    assert app.state.scheduler.snapshot(sid).next_timer_fire_at is not None
    await c.post('/control/curator.pause', headers=h, json={'session_id':sid})
    await c.post('/control/observation.close', headers=h, json={'session_id':sid})
    dispatched = []
    async def dispatcher(session_id, trigger):
        dispatched.append(trigger)
        conn = db.connect(app.state.cfg.data_dir)
        observation.start_observation(conn, session_id, trigger); conn.close()
    app.state.scheduler._dispatcher = dispatcher
    await c.post('/control/curator.resume', headers=h, json={'session_id':sid})
    await c.post('/control/curator.resume', headers=h, json={'session_id':sid})
    await asyncio.sleep(0)
    result = (await c.post('/memory/commit', headers=headers(app,sid), json={'revision':None})).json()
    assert result['ok'] and not result['published']
    await app.state.scheduler.pi_run_done(sid)
    await c.post('/control/observation.close', headers=h, json={'session_id':sid})
    assert dispatched == ['observation_close']
    p = (await c.get('/web/project/'+sid)).json()['project']
    assert p['final_summary_requested']==0 and p['observation_enabled']==0


@pytest.mark.asyncio
@pytest.mark.parametrize('stored', ['not json', '[]', '{"schema_version":99}', '{"schema_version":2}'])
async def test_invalid_snapshot_is_reported_not_mistaken_for_empty_memory(client, stored):
    c, app = client; setup_project(app.state.cfg, records=0)
    conn = db.connect(app.state.cfg.data_dir)
    conn.execute("UPDATE observations SET status='published',state_json=?,map_text='bad' WHERE session_id='A'", (stored,))
    conn.execute("UPDATE projects SET current_observation_id=(SELECT id FROM observations WHERE session_id='A') WHERE session_id='A'")
    conn.close()
    assert (await c.get('/web/project/A')).status_code == 409
    assert (await c.post('/memory/read', headers=headers(app,'A'), json={})).status_code == 409


def test_opt_in_is_anchored_and_accepts_multiline_targets():
    from hook.coco import parse_start_prompt
    assert parse_start_prompt('开始进行测试\nhttps://fixture.test\n只总结本地证据') == ('https://fixture.test','只总结本地证据')
    assert parse_start_prompt('开始对 https://fixture.test 进行测试') == ('https://fixture.test','记录授权测试过程，梳理攻击面、API 与测试结果')
    assert parse_start_prompt('文档里的“开始进行测试 https://fixture.test”是什么意思？') is None
    assert parse_start_prompt('```\n开始进行测试 https://fixture.test\n```') is None


@pytest.mark.asyncio
async def test_many_records_are_paginated_without_loss_or_source_truncation(client):
    c, app = client; ids = setup_project(app.state.cfg, records=137)
    seen, after = [], 0
    while True:
        result = (await c.post('/memory/read', headers=headers(app,'A'), json={'mode':'records','limit':50,'after_id':after})).json()
        seen.extend(r['id'] for r in result['records'])
        assert all(not r['source_truncated'] for r in result['records'])
        if not result['has_more']: break
        after = result['next_after_id']
    assert seen == ids


def test_pending_api_is_prioritized_over_additional_facts_in_dense_digest():
    api = dict(id='orders', endpoint='GET /api/orders', purpose='Orders', evidence_ids=[1])
    state = blackboard.merge(blackboard.initial_state(),delta(topics=[topic()], facts=[fact()], apis=[api])).state
    for i in range(20):
        state['facts'].append({**state['facts'][0], 'id':f'long{i}', 'statement':'Repeated additional observation. '*15})
    body = digest_body(render_digest(state, 1, 'Local validation objective'))
    assert body['api_pending_count']==1
    assert body['api_todo']==[{'id':'orders','endpoint':'GET /api/orders'}]


@pytest.mark.asyncio
async def test_restart_marks_orphaned_runtime_failed_without_advancing_evidence(cfg):
    from service.app import create_app
    ids = setup_project(cfg, records=2)
    conn = db.connect(cfg.data_dir)
    conn.execute("UPDATE projects SET agent_turn_active=1,agent_activity_known=1 WHERE session_id='A'"); conn.close()
    app = create_app(cfg, start_background=False)
    async with app.router.lifespan_context(app):
        conn = db.connect(cfg.data_dir)
        row = conn.execute("SELECT * FROM projects WHERE session_id='A'").fetchone()
        status = conn.execute("SELECT status,error FROM observations WHERE session_id='A'").fetchone()
        assert row['pending_window_end']==ids[-1] and row['processed_record_id']==0
        assert row['current_observation_id'] is None and row['agent_turn_active']==0 and row['agent_activity_known']==0
        assert status['status']=='failed' and 'restarted' in status['error']
        conn.close()


def test_flat_v2_deltas_keep_large_api_and_test_windows_without_small_topic_caps():
    apis = [dict(id=f'api{i}',endpoint=f'GET /api/items/{i}',purpose=f'Local capacity fixture {i}',evidence_ids=[1]) for i in range(64)]
    tests = [trial(f'trial{i}',api_ids=[f'api{i % 64}']) for i in range(120)]
    facts = [fact(f'observation{i}') for i in range(120)]
    result = blackboard.merge(blackboard.initial_state(),delta(topics=[topic(api_ids=[a['id'] for a in apis])],apis=apis,tests=tests,facts=facts))
    assert not result.errors
    assert len(result.state['apis'])==64 and len(result.state['tests'])==120 and len(result.state['facts'])==120


@pytest.mark.asyncio
async def test_curator_control_aliases_share_one_storage_flag(client):
    c, app = client; setup_project(app.state.cfg, records=0)
    h = {'X-FlySec-Token':app.state.service_token}
    assert (await c.post('/control/curator.pause',headers=h,json={'session_id':'A'})).json()['ok']
    p = (await c.get('/web/project/A')).json()['project']
    assert p['curator_paused']==p['observer_paused']==1
    read = await c.post('/memory/read',headers=headers(app,'A'),json={})
    assert read.status_code==409 and read.json()['code']=='curator_paused'
    assert (await c.post('/control/observer.resume',headers=h,json={'session_id':'A'})).json()['ok']
    p = (await c.get('/web/project/A')).json()['project']
    assert p['curator_paused']==p['observer_paused']==0
    assert (await c.post('/control/observer.pause',headers=h,json={'session_id':'A'})).json()['ok']
    assert (await c.post('/control/curator.resume',headers=h,json={'session_id':'A'})).json()['ok']


@pytest.mark.asyncio
async def test_curator_read_commit_use_new_log_names_and_keep_memory_api(client):
    c, app = client; ids=setup_project(app.state.cfg)
    h=headers(app,'A')
    assert (await c.post('/memory/read',headers=h,json={})).status_code==200
    result=(await c.post('/memory/commit',headers=h,json=payload(ids[0]))).json()
    assert result['ok']
    data=(await c.get('/web/project/A')).json()
    logs=(await c.get('/web/observation/A/'+str(data['observation']['id'])+'/logs')).json()['logs']
    assert {'curator_read','curator_commit'} <= {x['op'] for x in logs}
    assert not {'memory_read','memory_commit'} & {x['op'] for x in logs}


def test_curator_spec_does_not_register_old_model_tools():
    from service.export_schemas import generate
    schemas=generate()
    assert set(schemas)=={'protocol','curator_read','curator_commit'}
    root=Path(__file__).parents[1]
    assert not (root/'service/routers/observer.py').exists()
    assert (root/'service/routers/curator.py').exists()
    assert not (root/'pi_ext/resources/memory-tools.schema.json').exists()
    prompt=(root/'pi_ext/resources/system.md').read_text()
    assert 'Memory Curator' in prompt and 'curator_read' in prompt and 'curator_commit' in prompt
    assert 'memory_read' not in prompt and 'memory_commit' not in prompt
