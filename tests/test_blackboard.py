"""Memory v2 domain rules, independent of transport/storage/graph."""
import json
import pytest
from pydantic import ValidationError
from service import blackboard as memory
from service.schemas import MemoryCommitInput
from service.feedback import render_digest, estimated_tokens
from service.evidence import execution_status


def delta(**items):
    return MemoryCommitInput(revision=None, **items)


def topic(id='auth', **extra):
    return dict(id=id, title='Authentication', **extra)


def fact(id='fact1', **extra):
    return dict(id=id, topic_id='auth', statement='This anonymous GET returned 401.', evidence_ids=[1], **extra)


def trial(id='test1', **extra):
    return dict({'id':id, 'topic_id':'auth', 'action':'GET /api/me', 'result':'HTTP 401', 'execution':'completed', 'outcome':'supports', 'evidence_ids':[1]}, **extra)


def test_independent_empty_model():
    state = memory.initial_state()
    assert state['schema_version'] == 2
    assert set(state) == {'schema_version','revision','topics','facts','tests','apis','questions'}


def test_append_and_idempotency():
    data=delta(topics=[topic()], facts=[fact()], tests=[trial()])
    first=memory.merge(memory.initial_state(),data)
    assert first.changed and not first.errors
    again=memory.merge(first.state,data)
    assert not again.changed and not again.errors


def test_immutable_observations_and_corrections():
    original=memory.merge(memory.initial_state(),delta(topics=[topic()],facts=[fact()])).state
    changed=fact();changed['statement']='Overwritten'
    assert memory.merge(original,delta(facts=[changed])).errors[0]['code']=='immutable_record'
    correction=fact('fact2',supersedes='fact1');correction['statement']='Only the specified unauthenticated request returned 401.'
    result=memory.merge(original,delta(facts=[correction]))
    assert not result.errors and len(result.state['facts'])==2
    assert [f['id'] for f in memory.active_facts(result.state)]==['fact2']


@pytest.mark.parametrize('execution',['error','denied','interrupted','unknown'])
def test_failed_execution_is_not_target_negative(execution):
    with pytest.raises(ValidationError):delta(tests=[trial(execution=execution,outcome='contradicts')])
    assert delta(tests=[trial(execution=execution,outcome='not_evaluated')])


def test_api_identity_and_single_test_storage():
    api={'id':'me','endpoint':'GET /api/me?x=1','purpose':'Profile','evidence_ids':[1]}
    first=memory.merge(memory.initial_state(),delta(topics=[topic(api_ids=['me'])],apis=[api],tests=[trial(api_ids=['me'])]),'https://fixture.test')
    assert not first.errors and first.state['apis'][0]['endpoint']=='GET https://fixture.test/api/me'
    assert 'tests' not in first.state['apis'][0]
    duplicate={**api,'id':'other'}
    assert any(e['code']=='duplicate_endpoint' for e in memory.merge(first.state,delta(apis=[duplicate]),'https://fixture.test').errors)
    assert memory.normalize_endpoint('GET /A/') != memory.normalize_endpoint('GET /a')


def test_endpoint_prose_and_non_http_resources_rejected():
    for endpoint in ['POST /x (GET view)','UNKNOWN dns://fixture.test']:
        with pytest.raises(ValueError):memory.normalize_endpoint(endpoint)


def test_references_and_topic_relation_basis():
    first=memory.merge(memory.initial_state(),delta(topics=[topic()],facts=[fact()])).state
    child=memory.merge(first,delta(topics=[topic('child',origin_fact_ids=['fact1'])]))
    assert not child.errors
    broken=memory.merge(first,delta(topics=[topic('child',origin_fact_ids=['missing'])]))
    assert any(e['code']=='unknown_fact' for e in broken.errors)
    circular=memory.merge(first,delta(topics=[topic(origin_fact_ids=['fact1'])]))
    assert any(e['code']=='self_origin' for e in circular.errors)


def test_question_resolution_is_evidenced():
    q=dict(id='q',topic_id='auth',question='Does login change it?',evidence_ids=[1],status='resolved')
    with pytest.raises(ValidationError):delta(questions=[q])
    q['resolution']='The cited follow-up request answered it.'
    assert delta(questions=[q])


def test_legacy_is_read_only_and_not_promoted_to_verified_fact():
    old={'schema_version':1,'revision':'old','assessments':[{'id':'a','subject':'Old direction','status':'tried-hit','conclusion':'Old claim','uncertainty':None,'evidenceRefs':['record:1'],'attempts':[],'dependsOn':[],'apiIds':[]}],'apis':[],'guidance':{'lock':'old strategy'}}
    before=json.dumps(old,sort_keys=True)
    converted=memory.prepare_state(old)
    assert json.dumps(old,sort_keys=True)==before
    assert converted['facts'][0]['kind']=='legacy_summary'
    assert 'guidance' not in converted


@pytest.mark.parametrize('response,expected',[
    ({'stdout':'HTTP 403 Forbidden'},'completed'),
    ({'error':'Tool call rejected by user','is_error':True},'denied'),
    ({'stdout':'','stderr':'SyntaxError: invalid syntax'},'error'),
    ({'stdout':'partial','stderr':'Killed: 9'},'interrupted'),
    ({'stdout':'warning','stderr':'an informational warning'},'completed'),
])
def test_execution_receipts(response,expected):
    assert execution_status(response)==expected


def test_digest_is_bounded_valid_and_secret_safe():
    state=memory.merge(memory.initial_state(),delta(topics=[topic()],facts=[fact()])).state
    for i in range(20):state['facts'].append({**fact(f'f{i}'),'kind':'observation','statement':'观察内容。'*150})
    state['facts'][0]['statement']='目标完成。凭据 sk-'+'x'*28+' NSSCTF{private_value}'
    text=render_digest(state,20,'Keep this task in scope')
    assert estimated_tokens(text)<=800
    data=json.loads(text.split('<flysec-memory>\n')[1].split('\n</flysec-memory>')[0])
    assert data['protocol']==2 and data['through_record']==20
    assert 'sk-' not in text and 'private_value' not in text
    assert text==render_digest(state,20,'Keep this task in scope')
