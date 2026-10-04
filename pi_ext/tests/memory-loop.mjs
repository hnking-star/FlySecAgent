// Pure local bridge guards: no model calls, credentials, HTTP or host tools.
import assert from 'node:assert/strict';
import { runOneRound } from '../dist/loop.js';
import { createCuratorTools } from '../dist/tools.js';

function fakeSession(errorMessage) {
  let listener;
  return {
    subscribe: fn => { listener = fn; return () => { listener = undefined; }; },
    prompt: async () => { if (errorMessage) listener?.({type:'message_end',message:{role:'assistant',stopReason:'error',errorMessage}}); },
    waitForIdle: async () => {},
  };
}
function fakeTools(receipt, fatal = false) {
  return {reset: () => {}, lastSubmit: () => receipt, fatal: () => fatal};
}

const accepted = await runOneRound(fakeSession('402 quota after accepted commit'),fakeTools({ok:true,revision:'r1',published:true}),'agent_stop');
assert.equal(accepted.ok,true); assert.equal(accepted.revision,'r1');
const rejected = await runOneRound(fakeSession('401 invalid model auth'),fakeTools(null),'agent_stop');
assert.equal(rejected.ok,false); assert.equal(rejected.errors[0].code,'model_request_rejected');
const cancelled = await runOneRound(fakeSession(),fakeTools(null),'agent_stop',{shouldStop: () => true});
assert.equal(cancelled.errors[0].code,'curator_cancelled');
for (const code of ['invalid_memory','unsupported_schema']) {
  const tools = createCuratorTools({read:async () => ({ok:false,code}),commit:async () => ({ok:false,code})});
  await tools.tools.find(t => t.name === 'curator_read').execute('read',{});
  assert.equal(tools.fatal(),true);
  const stopped = await runOneRound(fakeSession(),fakeTools(null,tools.fatal()),'agent_stop');
  assert.equal(stopped.errors[0].code,'runtime_stopped');
}
const toolset = createCuratorTools({read:async () => ({ok:true}),commit:async () => ({ok:true,revision:'new-receipt',published:true})});
assert.deepEqual(toolset.tools.map(t=>t.name).sort(),['curator_commit','curator_read']);
assert.ok(!toolset.tools.some(t=>['memory_read','memory_commit','observation_context','observation_submit'].includes(t.name)));
await toolset.tools.find(t=>t.name==='curator_commit').execute('commit',{revision:null});
assert.equal(toolset.lastSubmit().revision,'new-receipt');
console.log('7 Curator bridge guards passed: original 5 guards, exact tool whitelist, commit receipt');
