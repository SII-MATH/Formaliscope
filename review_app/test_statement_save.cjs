"use strict";
const assert=require("node:assert/strict");
require("./static/statement-save.js");

const card={id:"statement::Test.one",fingerprint:"basis-1"};
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function harness({debounceMs=15}={}) {
  const calls=[],saved=[],states=[],timers=new Map();let nextId=0,nextTimer=0;
  const service=StatementSave.create({debounceMs,requestId:()=>`request-${++nextId}`,
    setTimer:(callback,delay)=>{const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimer:id=>timers.delete(id),onState:state=>states.push(state),onSaved:(row,payload)=>saved.push({row,payload}),
    post:(url,payload)=>{const pending=deferred();calls.push({url,payload,pending});return pending.promise;}});
  function finish(index,extra={}) {
    const request=calls[index];request.pending.resolve({judgment:{...request.payload,id:`judgment-${index}`,...extra}});
  }
  function fire(){const scheduled=[...timers.values()];timers.clear();scheduled.forEach(item=>item.callback());}
  service.load(card);
  return {service,calls,saved,states,timers,finish,fire};
}

async function immediateAndDebounce() {
  const h=harness();h.service.edit({verdict:"aligned"},{immediate:true});
  await turn();assert.equal(h.calls.length,1,"Selecting a verdict starts a save without a button");
  assert.equal(h.calls[0].url,"./api/judgments");assert.equal(h.service.state.saving,true);
  h.finish(0);await turn();assert.equal(h.service.state.dirty,false);assert.equal(h.service.state.status,"saved");
  for(const rationale of ["初稿","补充","最后的解释 "]){h.service.edit({rationale});}
  assert.equal(h.timers.size,1,"Typing resets a single debounce timer");
  assert.equal(h.calls.length,1);h.fire();await turn();assert.equal(h.calls.length,2);
  assert.equal(h.calls[1].payload.rationale,"最后的解释");assert.equal(Object.isFrozen(h.calls[1].payload),true);
  h.finish(1);await turn();
  assert.equal(h.service.state.draft.rationale,"最后的解释 ","A response must not rewrite the form input");
  assert.equal(h.service.state.dirty,false);assert.equal(h.saved.length,2);
  h.service.edit({rationale:"最后的解释"});h.fire();await turn();
  assert.equal(h.calls.length,2,"Whitespace-only changes do not append redundant history");
}

async function coalescedQueue() {
  const h=harness();h.service.edit({verdict:"aligned"},{immediate:true});await turn();
  for(const verdict of ["uncertain","aligned","misaligned","uncertain"]){h.service.edit({verdict},{immediate:true});}
  h.service.edit({rationale:"仍在阅读"});
  const navigating=h.service.flush();
  assert.equal(h.calls.length,1,"Edits never create parallel judgment requests");
  h.finish(0);await turn();
  assert.equal(h.service.state.draft.verdict,"uncertain","The earlier response cannot overwrite a newer edit");
  assert.equal(h.calls.length,2,"Only the final queued edit becomes another judgment");
  assert.equal(h.calls[1].payload.verdict,"uncertain");assert.equal(h.calls[1].payload.rationale,"仍在阅读");
  assert.notEqual(h.calls[0].payload.request_id,h.calls[1].payload.request_id);
  let left=false;navigating.then(value=>{left=value;});await turn();assert.equal(left,false,"Navigation waits for the newest edit");
  h.finish(1);assert.equal(await navigating,true);assert.equal(left,true);assert.equal(h.service.state.dirty,false);
}

async function failedRetryIsIdempotent() {
  const h=harness();h.service.edit({verdict:"misaligned",rationale:"原判断"},{immediate:true});await turn();
  h.calls[0].pending.reject(new Error("响应丢失"));await turn();
  assert.equal(h.service.state.status,"error");assert.equal(h.service.state.dirty,true);
  h.service.edit({verdict:"aligned",rationale:"修订意见"},{immediate:true});await turn();
  assert.equal(h.calls.length,1,"A failed request waits for an explicit retry instead of looping");
  const retry=h.service.retry();await turn();assert.equal(h.calls.length,2);
  assert.deepEqual(h.calls[1].payload,h.calls[0].payload,"An uncertain server result retries the exact same request ID and body");
  h.finish(1);await turn();assert.equal(h.calls.length,3);
  assert.equal(h.calls[2].payload.rationale,"修订意见");assert.equal(h.calls[2].payload.verdict,"aligned");
  h.finish(2);assert.equal(await retry,true);assert.equal(h.saved.length,2);assert.equal(h.service.state.dirty,false);
}

async function noteDebounceDuringFlightAndReplay() {
  const h=harness();h.service.edit({verdict:"aligned"},{immediate:true});await turn();
  h.service.edit({rationale:"正在输入的解释"});h.finish(0);await turn();
  assert.equal(h.calls.length,1,"Finishing an earlier save does not cut short a note's debounce");
  assert.equal(h.service.state.status,"pending");h.fire();await turn();
  h.calls[1].pending.reject(new Error("确认响应丢失"));await turn();
  const first=h.service.flush(),second=h.service.flush();assert.equal(first,second,"Concurrent navigation and retry share one drain");
  await turn();const payload=h.calls[2].payload;
  const {request_id,...replayedRow}=payload;
  h.calls[2].pending.resolve({judgment:{...replayedRow,id:"already-committed"},replayed:true});
  assert.equal(await first,true,"The server's replayed receipt may omit request_id");
  assert.equal(h.calls.length,3);assert.equal(h.service.state.dirty,false);
}

async function failedNavigationPreservesInput() {
  const h=harness();h.service.edit({verdict:"uncertain",rationale:"待讨论"});
  const leaving=h.service.flush();await turn();assert.equal(h.timers.size,0);
  h.calls[0].pending.reject(new Error("网络断开"));assert.equal(await leaving,false);
  assert.deepEqual(h.service.state.draft,{verdict:"uncertain",rationale:"待讨论"});assert.equal(h.service.state.dirty,true);
  const retry=h.service.flush();await turn();h.finish(1);assert.equal(await retry,true);
  assert.equal(h.calls[0].payload.request_id,h.calls[1].payload.request_id);
  h.service.load({id:"statement::Test.two",fingerprint:"basis-2"});
  assert.equal(h.service.state.cardId,"statement::Test.two");assert.equal(h.service.state.dirty,false);
}

async function invalidAndUnconfirmed() {
  const h=harness();h.service.edit({rationale:"只有备注"});
  assert.equal(await h.service.flush(),false);assert.equal(h.calls.length,0);assert.match(h.service.state.error,/选择/);
  h.service.edit({verdict:"aligned"},{immediate:true});await turn();h.finish(0,{card_id:"another-card"});await turn();
  assert.equal(h.saved.length,0,"An unrelated POST receipt cannot mark the current entry as saved");
  assert.equal(h.service.state.dirty,true);assert.match(h.service.state.error,/确认/);
  const retry=h.service.retry();await turn();assert.equal(h.calls[1].payload.request_id,h.calls[0].payload.request_id);
  h.finish(1);assert.equal(await retry,true);
}

async function revertDuringFlightAndIdentityReset() {
  const h=harness();h.service.load(card,{verdict:"aligned",rationale:"既有意见"});
  h.service.edit({verdict:"misaligned"},{immediate:true});await turn();
  h.service.edit({verdict:"aligned"},{immediate:true});const drain=h.service.flush();
  h.finish(0);await turn();assert.equal(h.calls.length,2,"Reverting while a different value is in flight still restores the chosen value on the server");
  h.finish(1);assert.equal(await drain,true);
  h.service.edit({verdict:"uncertain"},{immediate:true});await turn();
  h.service.reset();h.service.load(card,{verdict:"misaligned",rationale:"另一人的意见"});
  const accepted=h.saved.length;h.finish(2);await turn();
  assert.equal(h.saved.length,accepted,"A response from an abandoned identity cannot change the new identity's history");
  assert.equal(h.service.state.draft.rationale,"另一人的意见");assert.equal(h.service.state.status,"idle");
}

(async()=>{
  await immediateAndDebounce();await coalescedQueue();await failedRetryIsIdempotent();await noteDebounceDuringFlightAndReplay();
  await failedNavigationPreservesInput();await invalidAndUnconfirmed();await revertDuringFlightAndIdentityReset();
  console.log("Statement autosave, serialized edits, idempotent retry and navigation tests passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
