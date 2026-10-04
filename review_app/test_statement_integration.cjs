"use strict";
const assert=require("node:assert/strict");
const fs=require("node:fs");
const path=require("node:path");
const vm=require("node:vm");

// Execute the real page controller and its state modules. The small DOM and
// API stand-ins let native history events and save acknowledgements arrive in
// controlled orders without duplicating the controller's navigation logic.
const clone=value=>JSON.parse(JSON.stringify(value));
const settle=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));};
function fixture() {
  const elements=new Map(),listeners=new Map(),requests=[],records=new Map(),drafts=new Map(),reads=[];
  const classes=()=>({toggle(){},add(){},remove(){}});
  function element(id) {
    if(!elements.has(id))elements.set(id,{id,value:"",textContent:"",innerHTML:"",hidden:false,open:false,
      disabled:false,scrollTop:0,scrollLeft:0,offsetTop:0,style:{},dataset:{},classList:classes(),
      parentElement:{open:false},setAttribute(){},scrollIntoView(){},showModal(){this.open=true;},close(){this.open=false;},
      addEventListener(type,callback){this[type]=callback;},querySelectorAll(){return [];},
      querySelector(){return {textContent:"",disabled:false,scrollIntoView(){}};}});
    return elements.get(id);
  }
  element("scope").value="all";element("sort").value="name";element("module-panel").hidden=true;
  const radios=["aligned","uncertain","misaligned"].map(value=>({value,checked:false,disabled:false}));
  const main=element("main-panel");
  const cards=["A","B","C"].map(id=>({id,declaration:`Test.${id}`,title:id,kind:"def",role:"其他声明",
    priority:1,module_file:`Test/${id}.lean`,fingerprint:`fp-${id}`,fingerprint_scheme:"kip126-review-legacy.v1",
    fingerprints:{"kip126-review-legacy.v1":`fp-${id}`},dependencies:[],statement:id,
    lean:{file:`Test/${id}.lean`,line:1,source:`def ${id} := 1`}}));
  const stack=[{url:"https://review.example/#A",state:null}];let cursor=0,nextToken=0;
  const browserHistory={
    replaceState(state,_,url){stack[cursor]={state:clone(state),url:String(url)};},
    pushState(state,_,url){stack.splice(cursor+1);stack.push({state:clone(state),url:String(url)});++cursor;},
    go(delta){const next=cursor+delta;if(next<0||next>=stack.length||next===cursor)return;
      cursor=next;const state=clone(stack[cursor].state);queueMicrotask(()=>listeners.get("popstate")?.({state}));},
    back(){this.go(-1);},forward(){this.go(1);},
  };
  const location={get href(){return stack[cursor].url;},get hash(){return new URL(this.href).hash;}};
  const api={
    async request(url){
      reads.push(url);
      if(url==="./api/config")return {preview:false};
      if(url==="./api/catalog")return {cards:clone(cards),source_commit:"source",snapshot_digest:"snapshot"};
      if(url.startsWith("./api/review-state?")){
        const id=new URL(url,location.href).searchParams.get('id'),rows=records.get(id)||[],draft=drafts.get(id);
        return {current:clone(rows[0]||null),draft:clone(draft?.pending?draft:null),
          draft_revision:draft?.revision||0,history_count:rows.length};
      }
      if(url.startsWith("./api/history?")){
        const params=new URL(url,location.href).searchParams,id=params.get('id'),offset=Number(params.get('cursor')||0),rows=records.get(id)||[];
        return {history:clone(rows.slice(offset,offset+25)),next_cursor:rows.length>offset+25?String(offset+25):null};
      }
      if(url.startsWith("./api/module?"))return {source:"def A := 1\ndef B := 1\ndef C := 1"};
      throw new Error(`Unexpected request: ${url}`);
    },
    async evidence(id){return clone(cards.find(card=>card.id===id));},clearEvidenceCache(){},
    post(url,payload){
      assert.ok(['./api/drafts','./api/judgments'].includes(url));
      return new Promise((resolve,reject)=>requests.push({url,payload:clone(payload),resolve,reject}));
    },
  };
  const identity={current:{display_name:"Tester"},async initialize(){return true;},async load(){return this.current;}};
  const document={baseURI:location.href,activeElement:null,head:{appendChild(){}},getElementById:element,
    querySelectorAll:selector=>selector==='[name="verdict"]'?radios:[],
    querySelector:selector=>selector==='[name="verdict"]:checked'?radios.find(radio=>radio.checked):main,
    addEventListener(){},createElement:()=>element("generated")};
  const context={document,location,history:browserHistory,URL,setTimeout,clearTimeout,console,
    requestAnimationFrame:callback=>setImmediate(callback),crypto:{randomUUID:()=>`token-${++nextToken}`},
    innerWidth:1200,scrollX:0,scrollY:0,scrollTo(){},
    addEventListener:(type,callback)=>listeners.set(type,callback),
    StatementAPI:{create:()=>api},StatementIdentity:{create:()=>identity},
    StatementGraph:{create:()=>({show(){},capture:()=>({}),restore(){}})},StatementSymbols:{create:()=>({clear(){}})},
    Stage3Lean:{toHtml:text=>text},Stage3Latex:{toHtml:text=>text,typeset(){}},MathJax:{typesetPromise(){}}};
  context.window=context;vm.createContext(context);
  for(const filename of ["directory-tree.js","review-labels.js","statement-save.js","statement-navigation.js","statement.js"])
    vm.runInContext(fs.readFileSync(path.join(__dirname,"static",filename),"utf8"),context,{filename});
  return {
    requests,stack,element,reads,records,drafts,
    get url(){return stack[cursor].url;},get title(){return element("card-title").textContent;},
    click(id){element("card-list").onclick({target:{closest:()=>({dataset:{id}})}});},
    back(){browserHistory.back();},forward(){browserHistory.forward();},
    edit(){element("rationale").value="An opinion that must survive a failed save.";
      radios.forEach(radio=>{radio.checked=radio.value==="aligned";});element("verdicts").onchange();},
    finish(index=0){const request=requests[index],row={...request.payload,id:`row-${index}`,reviewer:"Tester",
      created_at:`2026-10-04T00:00:0${index}.000Z`,fingerprint_scheme:"kip126-review-legacy.v1"};
      if(request.url.endsWith('drafts')){
        row.revision=request.payload.revision+1;row.pending=true;drafts.set(row.card_id,row);request.resolve({draft:row});
      }else{
        if(drafts.has(row.card_id))drafts.get(row.card_id).pending=false;
        records.set(row.card_id,[row,...(records.get(row.card_id)||[])]);request.resolve({judgment:row});
      }},
  };
}

async function prepare() {
  const f=fixture();await settle();assert.equal(f.title,"A");
  f.click("B");await settle();assert.equal(f.title,"B");
  f.edit();await settle();assert.equal(f.requests.length,1);return f;
}
async function finishBoundary(f){f.finish();await settle();assert.equal(f.requests[1].url,'./api/judgments');f.finish(1);await settle();}
async function ordinaryJumpThenNativeBack() {
  const f=await prepare();f.click("C");f.back();await settle();
  assert.equal(f.title,"B","The current form stays visible while its save is pending");
  await finishBoundary(f);
  assert.equal(f.title,"A");assert.equal(new URL(f.url).hash,"#A");
  assert.deepEqual(f.stack.map(entry=>entry.state.statementNavigation.snapshot.selected),["A","B"],
    "A stale ordinary jump cannot overwrite the browser entry reached by Back");
  f.forward();await settle();assert.equal(f.title,"B");assert.equal(new URL(f.url).hash,"#B");
}
async function nativeBackThenOrdinaryJump() {
  const f=await prepare();f.back();await settle();f.click("C");await settle();
  await finishBoundary(f);assert.equal(f.title,"A");assert.equal(new URL(f.url).hash,"#A");
  assert.deepEqual(f.stack.map(entry=>entry.state.statementNavigation.snapshot.selected),["A","B"],
    "An ordinary jump during restoration cannot create a new browser entry");
}
async function failedMixedNavigation() {
  const f=await prepare();f.click("C");f.back();await settle();f.requests[0].reject(new Error("Network unavailable"));
  await settle();assert.equal(f.title,"B");assert.equal(new URL(f.url).hash,"#B");
  assert.equal(f.element("rationale").disabled,false);assert.match(f.element("save-message").textContent,/Network unavailable/);
  assert.equal(f.element("rationale").value,"An opinion that must survive a failed save.");
  assert.equal(f.stack.length,2,"A failed save neither pushes C nor destroys A");
}
async function latestOrdinaryJump() {
  const f=await prepare();f.click("A");f.click("C");await settle();await finishBoundary(f);
  assert.equal(f.title,"C");assert.equal(new URL(f.url).hash,"#C");
  assert.deepEqual(f.stack.map(entry=>entry.state.statementNavigation.snapshot.selected),["A","B","C"],
    "Only the latest ordinary navigation waiting for the same save can push history");
}
async function nextUnderPendingFilter({acknowledgeBeforeClick}) {
  const f=fixture();await settle();f.click("B");await settle();
  await f.element("filters").onclick({target:{closest:()=>({dataset:{filter:"pending"}})}});
  f.edit();await settle();
  if(acknowledgeBeforeClick){f.finish();await settle();}
  f.element("next").onclick();await settle();
  if(!acknowledgeBeforeClick){
    assert.equal(f.title,"B","Next waits for the current draft acknowledgement");f.finish();await settle();
  }
  assert.equal(f.title,'B','A draft acknowledgement is not a completed judgment');
  f.finish(1);await settle();
  assert.equal(f.title,"C","Once B leaves the pending filter, Next continues after B rather than restarting at A");
  assert.equal(new URL(f.url).hash,"#C");assert.equal(f.requests.length,2);
}

async function lazyHistoryAndDraftRecovery(){
  const f=fixture();await settle();assert.equal(f.reads.some(url=>url.startsWith('./api/history?')),false);
  f.edit();await settle();f.finish();await settle();
  assert.equal(f.records.size,0,'Autosave must not update completed progress');
  assert.equal(f.element('status-badge').textContent,'未审阅');
  const completing=f.element('save').onclick();await settle();assert.equal(f.element('rationale').disabled,true);
  f.finish(1);await completing;await settle();assert.equal(f.element('status-badge').textContent,'通过');
  assert.equal(f.element('rationale').disabled,false);
  const original=f.records.get('A')[0];
  f.records.set('A',Array.from({length:55},(_,i)=>({...original,id:`old-${i}`})));
  f.element('review-history').open=true;f.element('review-history').ontoggle();await settle();
  assert.equal(f.element('history-more').hidden,false);
  assert.equal((f.element('history').innerHTML.match(/history-item/g)||[]).length,25);
  await f.element('history-more').onclick();await settle();
  assert.equal((f.element('history').innerHTML.match(/history-item/g)||[]).length,50);
  await f.element('history-more').onclick();assert.equal(f.element('history-more').hidden,true);
  const count=f.reads.filter(url=>url.startsWith('./api/history?')).length;
  f.element('review-history').ontoggle();await settle();assert.equal(f.reads.filter(url=>url.startsWith('./api/history?')).length,count);
}

(async()=>{await ordinaryJumpThenNativeBack();await nativeBackThenOrdinaryJump();
  await failedMixedNavigation();await latestOrdinaryJump();
  for(const acknowledgeBeforeClick of [true,false])await nextUnderPendingFilter({acknowledgeBeforeClick});
  await lazyHistoryAndDraftRecovery();
  console.log("Statement page integration: mixed native/ordinary navigation, failed saves and latest-intent tests passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
