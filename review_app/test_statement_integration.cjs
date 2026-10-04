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
function fixture({configure=()=>{},topics=[],initialURL='https://review.example/#A',initialEvidence=true,readHook=()=>{},evidenceHook=()=>{}}={}) {
  const elements=new Map(),listeners=new Map(),requests=[],records=new Map(),drafts=new Map(),reads=[];
  let apiOptions;
  const classes=()=>({toggle(){},add(){},remove(){}});
  function element(id) {
    assert.ok(!['structure-panel','structure-fields','field-count','reading-summary','enrichment-provenance'].includes(id),'Removed structure and enrichment detail elements are absent from the page');
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
  configure(cards,records,drafts);
  const currentReview=id=>(records.get(id)||[]).find(row=>row.fingerprint===cards.find(card=>card.id===id)?.fingerprint)||null;
  const stack=[{url:initialURL,state:null}];let cursor=0,nextToken=0;
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
      const pending=readHook(url);if(pending)return pending;
      if(url==="./api/config")return {preview:false};
      if(url==="./api/auth/me")return clone(identity.current);
      if(url.startsWith("./api/catalog"))return {cards:clone(cards.map(card=>({...card,
        verdict:currentReview(card.id)?.verdict||null,stale:!!records.get(card.id)?.length&&!currentReview(card.id)}))),
        source_commit:"source",snapshot_digest:"snapshot",enrichment_topics:clone(topics),
        initial_evidence:initialEvidence?clone(cards.find(c=>c.id===new URL(url,location.href).searchParams.get('initial'))||cards[0]):null};
      if(url.startsWith("./api/review-state?")){
        const id=new URL(url,location.href).searchParams.get('id'),rows=records.get(id)||[],draft=drafts.get(id);
        return {current:clone(currentReview(id)),draft:clone(draft?.pending?draft:null),
          draft_revision:draft?.revision||0,history_count:rows.length};
      }
      if(url.startsWith("./api/history?")){
        const params=new URL(url,location.href).searchParams,id=params.get('id'),offset=Number(params.get('cursor')||0),rows=records.get(id)||[];
        return {history:clone(rows.slice(offset,offset+25)),next_cursor:rows.length>offset+25?String(offset+25):null};
      }
      if(url.startsWith("./api/module?"))return {source:"def A := 1\ndef B := 1\ndef C := 1"};
      throw new Error(`Unexpected request: ${url}`);
    },
    async evidence(id){
      if(evidenceCache.has(id))return clone(evidenceCache.get(id));
      reads.push(`evidence:${id}`);const pending=evidenceHook(id);if(pending)return pending;
      return clone(cards.find(card=>card.id===id));
    },primeEvidence(card){evidenceCache.set(card.id,card);},clearEvidenceCache(){evidenceCache.clear();},
    post(url,payload){
      assert.ok(['./api/drafts','./api/judgments'].includes(url));
      return new Promise((resolve,reject)=>requests.push({url,payload:clone(payload),resolve,reject}));
    },
  };
  const evidenceCache=new Map();
  const identity={current:{display_name:"Tester"},async initialize(){return true;},async load(){reads.push('identity:load');return this.current;},unauthorized(){this.current=null;}};
  const document={baseURI:location.href,activeElement:null,head:{appendChild(){}},getElementById:element,
    querySelectorAll:selector=>selector==='[name="verdict"]'?radios:[],
    querySelector:selector=>selector==='[name="verdict"]:checked'?radios.find(radio=>radio.checked):main,
    addEventListener(){},createElement:()=>element("generated")};
  const context={document,location,history:browserHistory,URL,setTimeout,clearTimeout,console,
    requestAnimationFrame:callback=>setImmediate(callback),crypto:{randomUUID:()=>`token-${++nextToken}`},
    innerWidth:1200,scrollX:0,scrollY:0,scrollTo(){},
    addEventListener:(type,callback)=>listeners.set(type,callback),
    StatementAPI:{create:options=>{apiOptions=options;return api;}},StatementIdentity:{create:({restart,onLogout})=>Object.assign(identity,{restart,onLogout})},
    StatementGraph:{create:()=>({show(){},capture:()=>({}),restore(){}})},StatementSymbols:{create:()=>({clear(){}})},
    Stage3Lean:{toHtml:text=>text},Stage3Latex:{toHtml:text=>text,typeset(){}},MathJax:{typesetPromise(){}}};
  context.window=context;vm.createContext(context);
  for(const filename of ["directory-tree.js","review-labels.js","statement-save.js","statement-navigation.js","statement.js"])
    vm.runInContext(fs.readFileSync(path.join(__dirname,"static",filename),"utf8"),context,{filename});
  return {
    requests,stack,element,reads,records,drafts,
    logout(){identity.onLogout();},
    expire(){apiOptions.onUnauthorized();},
    async switchReviewer(){records.clear();drafts.clear();identity.onLogout();identity.current={display_name:'Other'};await identity.restart();},
    get url(){return stack[cursor].url;},get title(){return element("card-title").textContent;},
    click(id){element("card-list").onclick({target:{closest:()=>({dataset:{id}})}});},
    back(){browserHistory.back();},forward(){browserHistory.forward();},
    edit(verdict="aligned"){element("rationale").value="An opinion that must survive a failed save.";
      radios.forEach(radio=>{radio.checked=radio.value===verdict;});element("verdicts").onchange();},
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

function dependencyButton(f,id){
  return f.element('dependencies').innerHTML.match(new RegExp(`<button[^>]*data-id="${id}"[^>]*>.*?</button>`))?.[0];
}
async function dependencyReviewStates(){
  const f=fixture({configure(cards,records,drafts){
    cards.push(...['D','E','F','G'].map(id=>({...clone(cards[1]),id,declaration:`Test.${id}`,fingerprint:`fp-${id}`})));
    cards[0].dependencies=['B','C','D','E','F','G','unindexed'];
    cards[0].kind='structure';cards[0].fields=[{name:'value',type:'Nat'}];
    cards[0].lean.source='structure A where\n  value : Nat';
    for(const [id,verdict] of [['B','aligned'],['C','uncertain'],['D','misaligned'],['E','aligned'],['F','partial']])
      records.set(id,[{id:`review-${id}`,card_id:id,verdict,fingerprint:id==='E'?'old-version':`fp-${id}`}]);
    drafts.set('G',{pending:true,revision:1,verdict:'aligned',rationale:'Not completed'});
  }});await settle();
  assert.match(f.element('lean-code').innerHTML,/structure A where\n  value : Nat/,'Structure fields remain in the complete declaration source');
  for(const [id,state,text] of [['B','aligned','通过'],['C','uncertain','没看懂'],['D','misaligned','不通过'],['F','uncertain','没看懂']]){
    assert.match(dependencyButton(f,id),new RegExp(`data-review-state="${state}"`));
    assert.match(dependencyButton(f,id),new RegExp(`我的审阅：${text}`),'Review states are accessible without relying on color');
    assert.match(dependencyButton(f,id),new RegExp(`dependency-status"> · ${text}`));
  }
  for(const id of ['E','G','unindexed']){
    assert.match(dependencyButton(f,id),/data-review-state="pending"/,'Stale reviews, drafts and unknown references do not receive completed colors');
    assert.doesNotMatch(dependencyButton(f,id),/dependency-status/);
  }
  await f.switchReviewer();await settle();
  for(const id of ['B','C','D','E','F','G'])assert.match(dependencyButton(f,id),/data-review-state="pending"/,'Switching reviewers reloads only the new reviewer\'s results');
}
async function dependencyStatusAfterNavigation(){
  const f=fixture({configure(cards){cards[0].dependencies=['B'];}});await settle();
  assert.match(dependencyButton(f,'B'),/data-review-state="pending"/);
  f.element('dependencies').onclick({target:{closest:()=>({dataset:{id:'B'}})}});await settle();
  assert.equal(f.title,'B','Colored references retain dependency navigation');
  f.edit('misaligned');await settle();f.finish();await settle();
  assert.equal(f.element('status-badge').textContent,'未审阅','Saving a draft does not mark a reference as reviewed');
  f.back();await settle();assert.equal(f.title,'B','Returning waits for completion of the review');
  f.finish(1);await settle();assert.equal(f.title,'A');
  assert.match(dependencyButton(f,'B'),/data-review-state="misaligned"/,'Returning to a deep dependency ancestor shows the newly completed review');
  assert.match(dependencyButton(f,'B'),/dependency-status"> · 不通过/);
}

async function configuredTopicsAndV2Labels(){
  const f=fixture({topics:[{id:'topology',name:'拓扑学'},{id:'geometry',name:'几何学'}],
    initialURL:'https://review.example/?labels=topic%2Ftopology#B',configure(cards){
      for(const c of cards)c.enrichment={schema:'statement-enrichment.v2',title_zh:null,
        readback:{status:'draft',text_zh:c.statement},classification:{role:null,topics:[]},priority:null};
      cards[1].enrichment.classification={role:'derivation',topics:['topology']};
      cards[1].reading_summary_zh='DELETED_SUMMARY_FIELD';
    }});await settle();
  assert.equal(f.title,'B');
  assert.equal(new URL(f.url).searchParams.get('labels'),'topic/topology','A linked project topic survives the initial catalog load');
  assert.match(f.element('card-list').innerHTML,/data-id="B"/);
  assert.doesNotMatch(f.element('card-list').innerHTML,/data-id="[AC]"/,'Configured topics filter the real catalog');
  assert.match(f.element('active-labels').innerHTML,/拓扑学/);
  f.element('choose-labels').onclick();
  assert.match(f.element('label-groups').innerHTML,/data-label="topic\/topology"/);
  assert.match(f.element('label-groups').innerHTML,/data-label="topic\/geometry"/);
  assert.doesNotMatch(f.element('label-groups').innerHTML,/topic\/(adams|comparison|spectral)/,'The dialog uses this project\'s collection rather than KIP126 defaults');
  assert.match(f.element('card-labels').innerHTML,/中间推导/);
  assert.doesNotMatch(f.element('card-labels').innerHTML,/priority\//,'A null v2 priority is not assigned a legacy tier');
  assert.match(f.element('label-provenance-summary').textContent,/尚未分级/);
  f.element('labels-dialog').close();f.element('reset-labels').onclick();
  f.click('A');await settle();
  assert.equal(f.element('card-labels').innerHTML,'','A v2 null role is not replaced by a heuristic definition label');
  assert.match(f.element('more-card-labels').innerHTML,/回译草稿/);
  assert.doesNotMatch(f.element('statement').innerHTML,/DELETED_SUMMARY_FIELD/);
}

async function initialEvidenceAndSelection(){
  const linked=fixture({initialURL:'https://review.example/?directory=Test%2FA.lean#B'});await settle();
  assert.equal(linked.title,'B','An explicit valid hash keeps priority over the directory scope');
  assert.equal(linked.reads.filter(url=>url==='./api/auth/me').length,1);
  assert.equal(linked.reads.includes('identity:load'),false,'Startup reuses the initialized identity');
  assert.equal(linked.reads.includes('evidence:B'),false,'Matching initial evidence needs no separate request');
  assert.ok(linked.reads.some(url=>url.startsWith('./api/review-state?')),'Personal state is still fetched independently');
  const scoped=fixture({initialURL:'https://review.example/?directory=Test%2FB.lean'});await settle();
  assert.equal(scoped.title,'B','The automatic evidence candidate cannot override directory selection');
  assert.equal(scoped.reads.includes('evidence:B'),true,'A mismatched embedded candidate falls back to the selected evidence');
  const filtered=fixture({initialURL:'https://review.example/?labels=priority%2Fp0',configure(cards){cards[1].main_target=true;}});await settle();
  assert.equal(filtered.title,'B','Linked labels choose the matching row rather than the automatic evidence candidate');
  assert.doesNotMatch(filtered.element('card-list').innerHTML,/data-id="[AC]"/);
  const fallback=fixture({initialURL:'https://review.example/#unknown',initialEvidence:false});await settle();
  assert.equal(fallback.title,'A');assert.equal(fallback.reads.includes('evidence:A'),true);
  const malformed=fixture({initialURL:'https://review.example/#%E0%A4%A'});await settle();assert.equal(malformed.title,'A');
}

async function lateResponsesAndLogout(){
  let resolveB,resolveStateB;
  const f=fixture({evidenceHook:id=>id==='B'?new Promise(resolve=>{resolveB=resolve;}):null,
    readHook:url=>url==='./api/review-state?id=B'?new Promise(resolve=>{resolveStateB=resolve;}):null});await settle();
  f.click('B');await settle();f.click('C');await settle();assert.equal(f.title,'C');
  resolveB({id:'B'});resolveStateB({current:{verdict:'misaligned'},history_count:1});await settle();
  assert.equal(f.title,'C');assert.equal(f.element('status-badge').textContent,'未审阅','An older response cannot replace the current personal state');
  let resolveCatalog;
  const delayed=fixture({readHook:url=>url.startsWith('./api/catalog')?new Promise(resolve=>{resolveCatalog=resolve;}):null});await settle();
  // Resetting on identity expiry invalidates an already pending catalog response.
  delayed.logout();resolveCatalog({cards:[],initial_evidence:{id:'A'},source_commit:'old'});await settle();
  assert.equal(delayed.element('review-card').hidden,true);assert.equal(delayed.element('card-list').innerHTML,'');
  let resolveConfig;
  const bootstrap=fixture({readHook:url=>url==='./api/config'?new Promise(resolve=>{resolveConfig=resolve;}):null});await settle();
  bootstrap.logout();resolveConfig({preview:false});await settle();
  assert.equal(bootstrap.reads.some(url=>url.startsWith('./api/catalog')),false,'An old bootstrap cannot restore a logged-out session');
}

async function indexedSearchAfterCompletion(){
  const f=fixture();await settle();f.edit();await settle();f.finish();await settle();
  const completion=f.element('save').onclick();await settle();f.finish(1);await completion;await settle();
  f.element('search').value='通过';f.element('search').oninput({target:f.element('search')});
  assert.match(f.element('card-list').innerHTML,/data-id="A"/,'Completing a review updates the cached search labels');
  assert.doesNotMatch(f.element('card-list').innerHTML,/data-id="[BC]"/);
}

async function expiryKeepsFailedOpinion(){
  const f=fixture();await settle();f.edit();await settle();f.expire();
  f.requests[0].reject(new Error('Session expired'));await settle();
  assert.equal(f.element('rationale').value,'An opinion that must survive a failed save.');
  assert.match(f.element('save-message').textContent,/Session expired/,'An unauthorized save retains its error and unsaved input');
}

(async()=>{await ordinaryJumpThenNativeBack();await nativeBackThenOrdinaryJump();
  await failedMixedNavigation();await latestOrdinaryJump();
  for(const acknowledgeBeforeClick of [true,false])await nextUnderPendingFilter({acknowledgeBeforeClick});
  await lazyHistoryAndDraftRecovery();
  await dependencyReviewStates();await dependencyStatusAfterNavigation();
  await configuredTopicsAndV2Labels();
  await initialEvidenceAndSelection();await lateResponsesAndLogout();await indexedSearchAfterCompletion();
  await expiryKeepsFailedOpinion();
  console.log("Statement page integration: navigation, draft recovery, personal dependencies and configured v2 labels passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
