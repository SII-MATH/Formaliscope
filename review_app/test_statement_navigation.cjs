"use strict";
const assert=require('node:assert/strict');
require('./static/statement-navigation.js');

const clone=value=>JSON.parse(JSON.stringify(value));
const deferred=()=>{let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};};
let identitySequence=0;
const reading=(id,scrollY=0)=>({selected:id,scope:`scope-${id}`,search:`query-${id}`,
  filters:{review:'pending',roles:[`role-${id}`]},view:'graph',graphDepth:2,
  scrollY,listScrollTop:scrollY/2,moduleVisible:true,moduleOpen:true});

// A browser moves its cursor before delivering popstate. Events remain queued
// so a failed save can schedule a second event when it bounces back.
function browserHistory(){
  const entries=[{state:null,url:'/review'}],events=[],calls=[];
  let cursor=0;
  return {
    entries,events,calls,
    get cursor(){return cursor;},
    get state(){return entries[cursor].state;},
    get url(){return entries[cursor].url;},
    replaceState(state,_title,url){
      entries[cursor]={state:clone(state),url:url===undefined?entries[cursor].url:String(url)};
      calls.push(['replace',cursor]);
    },
    pushState(state,_title,url){
      entries.splice(cursor+1);entries.push({state:clone(state),url:String(url)});++cursor;
      calls.push(['push',cursor]);
    },
    go(delta){
      calls.push(['go',delta]);
      const next=cursor+delta;
      if(next<0||next>=entries.length||next===cursor)return;
      cursor=next;events.push(clone(entries[cursor].state));
    },
    back(){this.go(-1);},
    forward(){this.go(1);},
  };
}

function fixture({canLeave=async()=>true,restoreState=null,history:existingHistory=null}={}){
  const history=existingHistory||browserHistory(),restored=[],changes=[];
  let current=reading('A'),form={verdict:'',rationale:''};
  const applyReading=value=>{current=clone(value);form={verdict:'',rationale:''};};
  const navigation=StatementNavigation.create({history,capture:()=>current,
    restore:async snapshot=>{restored.push(clone(snapshot));if(restoreState)await restoreState(snapshot,applyReading);else applyReading(snapshot);},
    canLeave,onChange:value=>changes.push(value),token:()=>`identity-${++identitySequence}`});
  return {
    history,navigation,restored,changes,
    get reading(){return current;},set reading(value){current=value;},
    get form(){return form;},set form(value){form=value;},
    async dispatch(){const results=[];while(history.events.length)results.push(await navigation.pop(history.events.shift()));return results;},
    push(id,scrollY){navigation.remember();current=reading(id,scrollY);navigation.push(`/review#${id}`);},
  };
}

function testRememberAndPush(){
  const f=fixture();
  f.navigation.remember('/unused');assert.equal(f.history.calls.length,0,'An uninitialized controller does not rewrite unrelated history');
  f.reading=reading('A',240);f.navigation.replace('/review#A');
  assert.deepEqual(f.history.state.statementNavigation.snapshot,reading('A',240));
  assert.equal(f.changes.at(-1).canBack,false);
  f.reading.filters.roles.push('later');
  assert.deepEqual(f.history.state.statementNavigation.snapshot.filters.roles,['role-A'],'Stored filters are independent of later edits');
  f.reading.scrollY=510;f.navigation.remember('/review?scope=A#A');
  assert.equal(f.history.entries.length,1,'Remember updates the current entry without adding history');
  assert.equal(f.history.state.statementNavigation.snapshot.scrollY,510);
  f.push('B',900);
  assert.equal(f.history.entries.length,2);
  assert.equal(f.history.entries[0].state.statementNavigation.snapshot.scope,'scope-A');
  assert.equal(f.history.entries[0].state.statementNavigation.snapshot.scrollY,510);
  assert.deepEqual(f.history.state.statementNavigation.snapshot,reading('B',900),'A pushed entry owns its selected item, range, filters and reading position');
  assert.equal(f.changes.at(-1).canBack,true);
}

async function testBackForwardAndBranch(){
  const f=fixture();f.reading=reading('A',100);f.navigation.replace('/review#A');
  f.push('B',200);f.push('C',300);f.push('D',400);
  f.reading.scrollY=455;f.reading.listScrollTop=222;
  f.navigation.back();assert.deepEqual(await f.dispatch(),[true]);
  assert.deepEqual(f.reading,reading('C',300));
  f.history.forward();await f.dispatch();
  assert.equal(f.reading.selected,'D');assert.equal(f.reading.scrollY,455,'Forward restores the position captured when leaving the entry');
  assert.equal(f.reading.listScrollTop,222);
  f.history.back();await f.dispatch();f.history.back();await f.dispatch();
  assert.deepEqual(f.reading,reading('B',200));
  f.history.forward();await f.dispatch();assert.equal(f.reading.selected,'C');
  f.push('E',500);
  assert.deepEqual(f.history.entries.map(entry=>entry.state.statementNavigation.snapshot.selected),['A','B','C','E'],'A new jump after forward discards the remaining forward branch');
  f.history.forward();assert.equal(f.history.events.length,0);
  f.navigation.back();await f.dispatch();f.history.forward();await f.dispatch();
  assert.deepEqual(f.reading,reading('E',500),'The replacement branch does not restore the removed entry');
  f.history.back();await f.dispatch();f.history.back();await f.dispatch();f.history.back();await f.dispatch();
  assert.deepEqual(f.reading,reading('A',100));assert.equal(f.changes.at(-1).canBack,false);
}

async function testFailedFlushBounces(){
  let allowed=false;
  const f=fixture({canLeave:async()=>allowed});f.navigation.replace('/review#A');f.push('B',180);
  f.reading.scrollY=975;f.reading.filters.review='misaligned';
  f.form={verdict:'misaligned',rationale:'This unsaved explanation must survive.'};
  const before=clone(f.reading),beforeForm=clone(f.form);
  f.navigation.back();
  assert.equal(await f.navigation.pop(f.history.events.shift()),false);
  assert.equal(f.history.cursor,1,'A failed flush moves the browser back to the original entry');
  assert.equal(f.history.url,'/review#B');assert.equal(f.navigation.restoring,true);
  assert.deepEqual(await f.dispatch(),[false]);
  assert.equal(f.navigation.restoring,false);assert.equal(f.restored.length,0,'A rejected leave never reloads the form or an earlier reading state');
  assert.deepEqual(f.reading,before);assert.deepEqual(f.form,beforeForm);
  assert.equal(f.changes.at(-1).canBack,true);
  allowed=true;f.navigation.back();await f.dispatch();assert.equal(f.reading.selected,'A');
}

async function testThrownFlushBounces(){
  const f=fixture({canLeave:async()=>{throw new Error('Network unavailable');}});
  f.navigation.replace('/review#A');f.push('B',120);
  f.history.back();assert.deepEqual(await f.dispatch(),[false,false]);
  assert.equal(f.history.cursor,1);assert.equal(f.navigation.restoring,false);
  assert.equal(f.reading.selected,'B');assert.equal(f.restored.length,0);
}

async function testResetRejectsOldIdentity(){
  const f=fixture();f.navigation.replace('/review#A');f.push('B',150);
  const old=clone(f.history.state);
  f.navigation.reset();assert.equal(f.changes.at(-1).canBack,false);
  assert.equal(await f.navigation.pop(old),false);
  f.reading=reading('new-identity',40);f.navigation.replace('/review#new-identity');
  assert.notEqual(f.history.state.statementNavigation.session,old.statementNavigation.session);
  assert.equal(await f.navigation.pop(old),false);
  f.history.back();await f.dispatch();
  assert.deepEqual(f.reading,reading('new-identity',40),'Browser entries from an earlier identity cannot restore its selected item or filters');
  assert.equal(f.restored.length,0);
}

async function testResetDuringFlush(){
  const flush=deferred(),f=fixture({canLeave:()=>flush.promise});
  f.navigation.replace('/review#A');f.push('B',200);f.history.back();
  const pending=f.navigation.pop(f.history.events.shift());assert.equal(f.navigation.restoring,true);
  f.navigation.reset();f.reading=reading('new-identity',60);f.navigation.replace('/review#new-identity');
  flush.resolve(true);assert.equal(await pending,false,'A completed old-identity flush cannot revive its pending navigation');
  assert.equal(f.navigation.restoring,false);assert.equal(f.restored.length,0);
  assert.deepEqual(f.reading,reading('new-identity',60));
  assert.equal(f.history.state.statementNavigation.index,0);
}

async function testConcurrentNativeBack({allowed,latestFirst}){
  const first=deferred(),latest=deferred(),guards=[first,latest];
  const f=fixture({canLeave:()=>guards.shift().promise});
  f.navigation.replace('/review#A');f.push('B',100);f.push('C',200);
  const before=clone(f.reading);
  f.history.back();const olderPop=f.navigation.pop(f.history.events.shift());
  f.history.back();const newerPop=f.navigation.pop(f.history.events.shift());
  assert.equal(f.history.cursor,0);
  if(latestFirst){latest.resolve(allowed);await newerPop;first.resolve(allowed);}
  else{first.resolve(allowed);assert.equal(await olderPop,false);assert.equal(f.history.calls.filter(call=>call[0]==='go'&&call[1]>0).length,0,'An obsolete guard cannot bounce or restore the browser');latest.resolve(allowed);}
  assert.equal(await olderPop,false);assert.equal(await newerPop,allowed);
  if(allowed){
    assert.deepEqual(f.reading,reading('A'));assert.equal(f.restored.length,1,'Only the newest native back event restores a reading state');
    assert.equal(f.navigation.restoring,false);assert.equal(f.changes.at(-1).canBack,false);
  }else{
    assert.equal(f.history.cursor,2,'Two native back clicks bounce to the actual original entry');
    assert.deepEqual(f.history.calls.at(-1),['go',2]);await f.dispatch();
    assert.equal(f.navigation.restoring,false);assert.equal(f.restored.length,0);assert.deepEqual(f.reading,before);
  }
}

async function testReturnToOriginalEntryDuringFailedFlush(){
  const first=deferred(),latest=deferred(),guards=[first,latest];
  const f=fixture({canLeave:()=>guards.shift().promise});
  f.navigation.replace('/review#A');f.push('B',250);
  f.form={verdict:'uncertain',rationale:'Keep this unsaved comment.'};
  const before=clone(f.reading),beforeForm=clone(f.form);
  f.history.back();const olderPop=f.navigation.pop(f.history.events.shift());
  f.history.forward();const newerPop=f.navigation.pop(f.history.events.shift());
  assert.equal(f.history.cursor,1);
  first.resolve(false);assert.equal(await olderPop,false);
  latest.resolve(false);assert.equal(await newerPop,false);
  assert.equal(f.history.calls.some(call=>call[0]==='go'&&call[1]===0),false,'Already being at the original entry must never call history.go(0), which refreshes a real browser');
  assert.equal(f.navigation.restoring,false,'No bounce event is needed when the browser returned to the original entry');
  assert.equal(f.history.events.length,0);assert.equal(f.restored.length,0);
  assert.deepEqual(f.reading,before);assert.deepEqual(f.form,beforeForm);
}

async function testTwoBacksDuringFlushPreserveVisibleReading(){
  const first=deferred(),latest=deferred(),guards=[first,latest];
  const f=fixture({canLeave:()=>guards.length?guards.shift().promise:Promise.resolve(true)});
  f.navigation.replace('/review#A');f.push('B',200);f.push('C',300);
  // These scroll positions have not yet been written to browser history.
  f.reading.scrollY=475;f.reading.listScrollTop=88;
  const original=clone(f.reading);
  f.history.back();const olderPop=f.navigation.pop(f.history.events.shift());
  f.history.back();const newerPop=f.navigation.pop(f.history.events.shift());
  latest.resolve(true);await newerPop;first.resolve(true);await olderPop;
  assert.equal(f.reading.selected,'A');
  f.history.forward();await f.dispatch();f.history.forward();await f.dispatch();
  assert.deepEqual(f.reading,original,'The first pop captures the visible item before a delayed flush and multiple native back clicks');
}

async function testBackDuringLoadingPreservesForwardReading(){
  const loading=deferred(),started=deferred();let restoreSequence=0;
  const f=fixture({restoreState:async(snapshot,apply)=>{
    const turn=++restoreSequence;
    // The app hides the old evidence and closes the module while opening a
    // card. A slower load must not become that entry's saved reading state.
    apply({...snapshot,scrollY:0,listScrollTop:0,moduleVisible:false,moduleOpen:false});
    if(snapshot.selected==='B'){started.resolve();await loading.promise;}
    if(turn===restoreSequence)apply(snapshot);
  }});
  f.reading=reading('A',100);f.navigation.replace('/review#A');f.push('B',200);f.push('C',300);
  const expectedB=reading('B',200),expectedC=clone(f.reading);
  f.history.back();const first=f.navigation.pop(f.history.events.shift());await started.promise;
  assert.equal(f.reading.selected,'B');assert.equal(f.reading.moduleVisible,false);
  f.history.back();await f.navigation.pop(f.history.events.shift());
  loading.resolve();await first;
  assert.deepEqual(f.reading,reading('A',100),'The latest restore wins over an earlier card load');
  f.history.forward();await f.dispatch();
  assert.deepEqual(f.reading,expectedB,'Forward restores the original module expansion and scroll position, rather than the interrupted loading placeholder');
  f.history.forward();await f.dispatch();assert.deepEqual(f.reading,expectedC);
}

async function testReloadResumesBrowserEntries(){
  const original=fixture();original.reading=reading('A',100);original.navigation.replace('/review#A');
  original.push('B',200);original.push('C',300);
  const oldSession=original.history.state.statementNavigation.session;
  // Reload creates a new controller while the browser keeps its entries.
  const reloaded=fixture({history:original.history});reloaded.reading=reading('C',350);
  reloaded.navigation.replace('/review#C');
  assert.equal(reloaded.history.entries.length,3);
  assert.equal(reloaded.history.state.statementNavigation.session,oldSession);
  assert.equal(reloaded.history.state.statementNavigation.index,2,'Initial replacement after reload keeps the real browser position');
  assert.equal(reloaded.changes.at(-1).canBack,true);
  reloaded.navigation.back();await reloaded.dispatch();
  assert.deepEqual(reloaded.reading,reading('B',200),'Back after reload restores an older browser snapshot even though it is absent from the new controller cache');
  reloaded.navigation.back();await reloaded.dispatch();assert.deepEqual(reloaded.reading,reading('A',100));
  reloaded.history.forward();await reloaded.dispatch();reloaded.history.forward();await reloaded.dispatch();
  assert.deepEqual(reloaded.reading,reading('C',350));
  reloaded.navigation.reset();reloaded.reading=reading('new-identity',50);reloaded.navigation.replace('/review#new-identity');
  assert.notEqual(reloaded.history.state.statementNavigation.session,oldSession,'Identity reset still replaces the resumed session');
  reloaded.history.back();await reloaded.dispatch();
  assert.deepEqual(reloaded.reading,reading('new-identity',50),'A reload does not weaken the reset boundary for older identity entries');
}

function testMalformedBrowserStateStartsFresh(){
  for(const value of [null,{session:'',index:2},{session:7,index:2},{session:'old',index:-1},
    {session:'old',index:'2'},{session:'old',index:1.5},{session:'old',index:Number.MAX_SAFE_INTEGER+1}]){
    const history=browserHistory();history.replaceState({statementNavigation:value},'', '/review#A');
    const f=fixture({history});f.navigation.replace('/review#A');
    assert.equal(history.state.statementNavigation.index,0,'Malformed browser navigation state cannot supply an index');
    assert.equal(f.changes.at(-1).canBack,false);
    assert.notEqual(history.state.statementNavigation.session,value?.session);
  }
}

(async()=>{
  testRememberAndPush();await testBackForwardAndBranch();await testFailedFlushBounces();
  await testThrownFlushBounces();await testResetRejectsOldIdentity();await testResetDuringFlush();
  for(const allowed of [true,false])for(const latestFirst of [true,false])await testConcurrentNativeBack({allowed,latestFirst});
  await testReturnToOriginalEntryDuringFailedFlush();
  await testTwoBacksDuringFlushPreserveVisibleReading();await testBackDuringLoadingPreservesForwardReading();
  await testReloadResumesBrowserEntries();testMalformedBrowserStateStartsFresh();
  console.log('Statement navigation history, reading position, failed-save bounce and identity isolation tests passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
