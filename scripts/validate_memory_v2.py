"""Controlled local validation: real HTTP fixtures + real Pi, never public targets.

Run after building pi_ext. Only synthetic fixture receipts go to the configured
Memory Curator model. The real key is referenced by path, never copied into artifacts.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'data' / ('v2-validation-' + uuid.uuid4().hex[:10])
OUT.mkdir(mode=0o700, parents=True)


class Fixture(BaseHTTPRequestHandler):
    def log_message(self,*args):pass
    def do_GET(self):
        if self.path=='/assets/app.js':
            status=200;body=b"function loadProfile(id){return fetch('/api/profile?id='+id)};function loadOrders(status){return fetch('/api/orders?status='+status)}"
        elif self.path.startswith('/api/profile'):
            status=401;body=b'{"error":"login_required"}'
        elif self.path.startswith('/api/orders'):
            status=200;body=b'{"orders":[],"marker":"LOCAL_ORDER_OK"}'
        else:status=404;body=b'not found'
        self.send_response(status);self.send_header('Content-Type','application/javascript' if self.path=='/assets/app.js' else 'application/json');self.end_headers();self.wfile.write(body)


fixture=ThreadingHTTPServer(('127.0.0.1',0),Fixture)
threading.Thread(target=fixture.serve_forever,daemon=True).start()
base=f'http://127.0.0.1:{fixture.server_port}'
with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
api=f'http://127.0.0.1:{port}'
env={**os.environ,'FLYSEC_PORT':str(port),'FLYSEC_DATA_DIR':str(OUT),'FLYSEC_PI_API_KEY_FILE':str(ROOT/'data/.deepseek-key')}
env.pop('FLYSEC_PI_COMMAND',None)
log=(OUT/'service.log').open('w')
proc=subprocess.Popen([sys.executable,'-u','-m','service'],cwd=ROOT,env=env,stdout=log,stderr=subprocess.STDOUT)
sid='local-memory-validation-'+uuid.uuid4().hex[:12]
checks={}


def call(path,body=None,token=None):
    headers={'Content-Type':'application/json'}
    if token:headers['X-FlySec-Token']=token
    req=urllib.request.Request(api+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    with urllib.request.urlopen(req,timeout=8) as res:return json.load(res)


def wait_publish(previous=0):
    end=time.monotonic()+150
    while time.monotonic()<end:
        data=call('/web/project/'+sid)
        if data.get('observation') and data['observation']['id']>previous:
            return data
        if data.get('latest_run',{}).get('status')=='failed':
            raise RuntimeError('Memory Curator failed: '+str(data['latest_run'].get('error')))
        time.sleep(1)
    call('/control/curator.pause',{'session_id':sid},token)
    raise RuntimeError('Model did not produce a valid receipt within the bounded validation window')


def actual_http(path):
    try:
        with urllib.request.urlopen(base+path,timeout=5) as response:
            code=response.status;body=response.read().decode();headers=dict(response.headers)
    except urllib.error.HTTPError as response:
        code=response.code;body=response.read().decode();headers=dict(response.headers)
    return {'stdout':f'HTTP {code}\n{body}','stderr':'','exit_code':0,'headers':headers}


def ingest(key,path,response,description):
    return call('/hook/record.ingest',{'session_id':sid,'call_key':key,'tool_name':'exec_command',
                'tool_input':{'url':base+path,'method':'GET','description':description},'tool_response':response,
                'metadata':{'hook_event_name':'PostToolUse','source':'controlled-local-validation'}},token)['record_id']


try:
    for _ in range(40):
        try:call('/health');break
        except Exception:time.sleep(.25)
    token=(OUT/'.secret').read_text().strip()
    call('/hook/project.ensure',{'session_id':sid,'hint':{'target':base,'objective':'Only summarize these local fixture receipts. Use a separate frontend-discovery topic for the JS read, and API-response topics that originate from the cited frontend discovery observation; these are evidence associations, not temporal causality. Remember profile and orders API metadata, preserve request scope, and distinguish local Python failures from target responses. Do not execute requests or commands.'}},token)
    ids=[]
    ids.append(ingest('js','/assets/app.js',actual_http('/assets/app.js'),'Read local frontend source; API lines are candidates, not fully tested endpoints.'))
    ids.append(ingest('profile','/api/profile?id=1',actual_http('/api/profile?id=1'),'Observe anonymous GET profile response; cannot conclude all authentication behavior.'))
    error=subprocess.run([sys.executable,'-c','raise SyntaxError("LOCAL_FIXTURE_NO_HTTP_SENT")'],capture_output=True,text=True)
    ids.append(ingest('error','/api/orders',{'stdout':error.stdout,'stderr':error.stderr,'exit_code':error.returncode},'Local script failed before any orders HTTP request was sent.'))
    call('/control/agent-turn/begin',{'session_id':sid},token);call('/control/agent-turn/stop',{'session_id':sid},token)
    first=wait_publish()
    state=first['state'];checks['protocol_v2']=state['schema_version']==2
    tool_logs=call('/web/observation/'+sid+'/'+str(first['observation']['id'])+'/logs')['logs']
    checks['curator_tool_logs']={'curator_read','curator_commit'} <= {x['op'] for x in tool_logs}
    checks['curator_runtime_identity']=bool(first['project'].get('curator_session_id'))
    checks['two_apis_discovered']=len(state['apis'])==2 and any('/api/profile' in a['endpoint'] for a in state['apis']) and any('/api/orders' in a['endpoint'] for a in state['apis'])
    checks['api_parameters_collected']=all({p['name'] for p in a['parameters']} >= ({'id'} if '/api/profile' in a['endpoint'] else {'status'}) for a in state['apis'])
    first_digest=json.loads(first['memory_digest'].split('<flysec-memory>\n',1)[1].split('\n</flysec-memory>',1)[0])
    checks['failed_api_remains_pending']=any('/api/orders' in a['endpoint'] for a in first_digest['api_todo']) and first_digest['api_pending_count']>=1
    error_tests=[t for t in state['tests'] if ids[2] in t['evidence_ids']]
    checks['local_error_not_target_negative']=bool(error_tests) and all(t['execution']=='error' and t['outcome']=='not_evaluated' for t in error_tests)
    checks['facts_have_actual_evidence']=bool(state['facts']) and all(set(f['evidence_ids']) <= set(ids) for f in state['facts'])
    checks['scope_is_preserved']=any(f.get('scope') for f in state['facts']) or any(t.get('scope') for t in state['tests'])
    (OUT/'first-publication.json').write_text(json.dumps(first,ensure_ascii=False,indent=2))
    second_id=ingest('orders','/api/orders?status=pending',actual_http('/api/orders?status=pending'),'Actual GET orders executed successfully now; keep old tests and reuse the previously discovered API ID.')
    old_api_ids={a['id'] for a in state['apis']};old_test_ids={t['id'] for t in state['tests']}
    call('/control/agent-turn/begin',{'session_id':sid},token);call('/control/agent-turn/stop',{'session_id':sid},token)
    second=wait_publish(first['observation']['id']);state2=second['state']
    checks['api_identity_retained']={a['id'] for a in state2['apis']}==old_api_ids and len(state2['apis'])==2
    checks['origin_has_actual_evidence']=any(t['origin_fact_ids'] and all(fid in {f['id'] for f in state2['facts']} for fid in t['origin_fact_ids']) for t in state2['topics'])
    checks['old_tests_retained']=old_test_ids <= {t['id'] for t in state2['tests']}
    checks['new_test_appended']=any(second_id in t['evidence_ids'] and t['execution']=='completed' for t in state2['tests'])
    checks['cursor_and_end_state']=second['project']['processed_record_id']==second_id and second['project']['pending_window_end'] is None and second['project']['agent_turn_active']==0
    pending=call('/hook/map.pending?session_id='+sid,token=token)
    checks['digest_delivered']=pending.get('revision')==state2['revision'] and '<flysec-memory>' in pending.get('map_text','')
    call('/hook/map.ack',{'session_id':sid,'revision':pending['revision']},token)
    checks['same_version_not_repeated']=call('/hook/map.pending?session_id='+sid,token=token)['revision'] is None
    (OUT/'second-publication.json').write_text(json.dumps(second,ensure_ascii=False,indent=2))
    summary={'checks':checks,'all_passed':all(checks.values()),'artifact_directory':str(OUT),'session_id':sid,'scope':'real local HTTP fixture and real configured Pi model; no public target requests'}
    (OUT/'result.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2));print(json.dumps(summary,ensure_ascii=False),flush=True)
    if not summary['all_passed']:raise RuntimeError('A required capability check failed')
finally:
    proc.terminate()
    try:proc.wait(timeout=10)
    except subprocess.TimeoutExpired:proc.kill();proc.wait()
    fixture.shutdown();fixture.server_close();log.close()
