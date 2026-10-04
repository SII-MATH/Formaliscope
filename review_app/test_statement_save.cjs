"use strict";
const assert=require("node:assert/strict");
require("./static/statement-save.js");

const card={id:"statement::Test.one",fingerprint:"basis-1"};
const turn=()=>new Promise(resolve=>setImmediate(resolve));
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
function harness() {
  const calls=[],saved=[],timers=new Map();let nextId=0,nextTimer=0;
  const service=StatementSave.create({requestId:()=>`request-${++nextId}`,
    setTimer:(callback,delay)=>{const id=++nextTimer;timers.set(id,{callback,delay});return id;},
    clearTimer:id=>timers.delete(id),onSaved:row=>saved.push(row),
    post:(url,payload)=>{const pending=deferred();calls.push({url,payload,pending});return pending.promise;}});
  function finish(index,extra={}) {
    const {url,payload,pending}=calls[index];
    const row={...payload,id:url.endsWith('drafts')?'draft-1':`judgment-${index}`,...extra};
    if(url.endsWith('drafts')){row.revision=payload.revision+1;pending.resolve({draft:row});}
    else pending.resolve({judgment:row});
  }
  function fire(){const scheduled=[...timers.values()];timers.clear();scheduled.forEach(item=>item.callback());}
  service.load(card);
  return {service,calls,saved,timers,finish,fire};
}
async function draftsDoNotBecomeHistory() {
  const h=harness();h.service.edit({verdict:"aligned"},{immediate:true});await turn();
  assert.equal(h.calls[0].url,"./api/drafts");h.finish(0);await turn();
  assert.equal(h.service.state.dirty,false);assert.equal(h.service.state.unfinished,true);assert.equal(h.saved.length,0);
  for(const text of ["第一行","第一行\n第二行","完整意见"]){
    h.service.edit({rationale:text});assert.equal(h.timers.size,1);h.fire();await turn();
    h.finish(h.calls.length-1);await turn();
  }
  assert.equal(h.calls.length,4);assert.equal(h.saved.length,0,"Typing pauses only update the draft");
  assert.equal(h.calls[3].payload.revision,3);
  const completing=h.service.complete();await turn();assert.equal(h.service.state.completing,true);
  assert.equal(h.calls[4].url,"./api/judgments");assert.equal(h.calls[4].payload.draft_revision,4);
  h.finish(4);assert.equal(await completing,true);assert.equal(h.saved.length,1);
  assert.equal(h.saved[0].rationale,"完整意见");assert.equal(h.service.state.status,"completed");
  assert.equal(await h.service.flush(),true);assert.equal(h.calls.length,5,"Unchanged navigation does not create another version");
  h.service.edit({rationale:"完整意见 "});h.fire();await turn();assert.equal(h.calls.length,5);
}
async function navigationDrainsLatestDraft() {
  const h=harness();h.service.edit({verdict:"aligned"},{immediate:true});await turn();
  for(const verdict of ["uncertain","misaligned","uncertain"]){h.service.edit({verdict},{immediate:true});}
  h.service.edit({rationale:"仍在阅读"});
  const leaving=h.service.flush();assert.equal(h.calls.length,1);h.finish(0);await turn();
  assert.equal(h.calls[1].url,"./api/drafts");assert.equal(h.calls[1].payload.rationale,"仍在阅读");
  h.finish(1);await turn();assert.equal(h.calls[2].url,"./api/judgments");
  assert.equal(h.calls[2].payload.verdict,"uncertain");assert.equal(h.saved.length,0);
  h.finish(2);assert.equal(await leaving,true);assert.equal(h.saved.length,1);
}
async function noteOnlyAndRestoration() {
  const h=harness();h.service.edit({rationale:"先记录疑问"});h.fire();await turn();
  h.finish(0);await turn();assert.equal(await h.service.flush(),true);assert.equal(h.calls.length,1);
  assert.equal(h.saved.length,0);assert.equal(h.service.state.unfinished,true);
  assert.equal(await h.service.complete(),false);assert.match(h.service.state.error,/选择/);
  h.service.load(card,null,{verdict:'',rationale:'先记录疑问',revision:7},7);
  assert.equal(h.service.state.draft.rationale,'先记录疑问');assert.equal(h.service.state.dirty,false);
  h.service.edit({verdict:'uncertain'},{immediate:true});await turn();assert.equal(h.calls[1].payload.revision,7);
  h.finish(1);await turn();const complete=h.service.complete();await turn();h.finish(2);assert.equal(await complete,true);
}
async function retriesPreserveBothKindsOfRequest() {
  const h=harness();h.service.edit({verdict:"misaligned",rationale:"原判断"},{immediate:true});await turn();
  h.calls[0].pending.reject(new Error("响应丢失"));await turn();
  h.service.edit({verdict:"aligned",rationale:"修订意见"},{immediate:true});await turn();assert.equal(h.calls.length,1);
  const retry=h.service.retry();await turn();assert.deepEqual(h.calls[1].payload,h.calls[0].payload);
  h.finish(1);await turn();assert.equal(h.calls[2].payload.rationale,"修订意见");
  h.finish(2);await turn();assert.equal(h.calls[3].url,"./api/judgments");
  h.calls[3].pending.reject(new Error("完成确认丢失"));assert.equal(await retry,false);
  assert.equal(h.service.state.draft.rationale,"修订意见");assert.equal(h.saved.length,0);
  const first=h.service.flush(),second=h.service.flush();assert.equal(first,second);await turn();
  assert.deepEqual(h.calls[4].payload,h.calls[3].payload);
  const {request_id,...row}=h.calls[4].payload;
  h.calls[4].pending.resolve({judgment:{...row,id:"already-committed"},replayed:true});
  assert.equal(await first,true);assert.equal(h.saved.length,1);assert.equal(h.service.state.dirty,false);
}
async function debounceAndFailedNavigation() {
  const h=harness();h.service.edit({verdict:"aligned"},{immediate:true});await turn();
  h.service.edit({rationale:"输入中"});h.finish(0);await turn();assert.equal(h.calls.length,1);
  assert.equal(h.service.state.status,"pending");
  const leaving=h.service.flush();await turn();assert.equal(h.timers.size,0);
  h.calls[1].pending.reject(new Error("网络断开"));assert.equal(await leaving,false);
  assert.equal(h.service.state.draft.rationale,"输入中");assert.equal(h.service.state.dirty,true);
  const retry=h.service.flush();await turn();h.finish(2);await turn();h.finish(3);assert.equal(await retry,true);
  assert.equal(h.calls[2].payload.request_id,h.calls[1].payload.request_id);
}
async function invalidReceiptAndIdentityReset() {
  const h=harness();h.service.edit({verdict:"aligned"},{immediate:true});await turn();
  h.finish(0,{card_id:"another-card"});await turn();assert.match(h.service.state.error,/确认/);
  assert.equal(h.saved.length,0);const retry=h.service.flush();await turn();h.finish(1);await turn();
  h.service.reset();h.service.load(card,{verdict:"misaligned",rationale:"另一人的意见"});
  h.finish(2);assert.equal(await retry,false);assert.equal(h.saved.length,0);
  assert.equal(h.service.state.draft.rationale,"另一人的意见");assert.equal(h.service.state.status,"completed");
}
(async()=>{
  await draftsDoNotBecomeHistory();await navigationDrainsLatestDraft();await noteOnlyAndRestoration();
  await retriesPreserveBothKindsOfRequest();await debounceAndFailedNavigation();await invalidReceiptAndIdentityReset();
  console.log("Statement drafts, completion boundaries, restoration, idempotent retries and navigation tests passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
