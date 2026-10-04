"use strict";
const assert=require('node:assert/strict');
require('./static/statement-api.js');
require('./static/statement-graph.js');
require('./static/statement-identity.js');

function response(data,status=200){return {ok:status>=200&&status<300,status,json:async()=>data};}
async function testAPI(){
  const calls=[],resolvers=[];
  const api=StatementAPI.create({evidenceLimit:2,fetch:url=>{
    calls.push(url);return new Promise(resolve=>resolvers.push(resolve));
  }});
  const first=api.evidence('same'),duplicate=api.evidence('same');
  assert.equal(first,duplicate,'Concurrent evidence requests share one pending response');
  resolvers.shift()(response({id:'same'}));await first;
  await api.evidence('same');assert.equal(calls.length,1);
  for(const id of ['second','third']){const request=api.evidence(id);resolvers.shift()(response({id}));await request;}
  const evicted=api.evidence('same');assert.equal(calls.length,4);resolvers.shift()(response({id:'same'}));await evicted;

  const previous=api.evidence('during-logout');api.clearEvidenceCache();
  const next=api.evidence('during-logout');assert.notEqual(previous,next);
  resolvers.shift()(response({id:'during-logout',session:'old'}));await previous;
  assert.equal(api.evidence('during-logout'),next,'An earlier session response cannot remove the new pending request');
  resolvers.shift()(response({id:'during-logout',session:'new'}));await next;
  assert.equal((await api.evidence('during-logout')).session,'new');

  let unauthorized=0,historyCalls=0;
  const account=StatementAPI.create({onUnauthorized:()=>unauthorized++,fetch:async url=>{
    if(url.includes('history')){historyCalls++;return response({history:[]});}
    return response({error:'请登录'},401);
  }});
  assert.equal(await account.request('./api/auth/me',{optionalUnauthorized:true}),null);
  assert.equal(unauthorized,0,'An optional session probe does not redirect');
  await assert.rejects(account.request('./api/auth/me'),/请登录/);assert.equal(unauthorized,1);
  await account.request('./api/history?id=same');await account.request('./api/history?id=same');
  assert.equal(historyCalls,2,'Personal review history is always fetched for the current identity');
}

function testGraph(){
  const catalog=[
    {id:'center',dependencies:['input','unindexed']},
    {id:'input',dependencies:['leaf','center']},
    {id:'leaf',dependencies:[]},
    {id:'user',dependencies:['center']},
    {id:'outer',dependencies:['user']},
  ];
  const one=StatementGraph.neighborhood(catalog,'center',1);
  assert.deepEqual([...one.nodes.keys()],['center','input','user']);
  assert.ok(one.nodes.get('input').x<one.nodes.get('center').x);
  assert.ok(one.nodes.get('user').x>one.nodes.get('center').x);
  const two=StatementGraph.neighborhood(catalog,'center',2);
  assert.deepEqual([...two.nodes.keys()],['center','input','user','leaf','outer']);
  assert.equal(two.nodes.size,5,'A cycle does not duplicate the selected declaration');
  const large=[{id:'center',dependencies:[]},...Array.from({length:25},(_,i)=>({id:`user-${i}`,dependencies:['center']}))];
  const bounded=StatementGraph.neighborhood(large,'center',1);
  assert.equal(bounded.nodes.size,19);assert.equal(bounded.omitted,7);
  assert.equal(bounded.nodes.get('center').y,bounded.height/2,'The selected declaration stays vertically centered');
}

async function testIdentity(){
  const elements=new Map(),saved=new Map(),requests=[],sessions=new Map();
  function $(id){
    if(!elements.has(id))elements.set(id,{value:'',open:false,listeners:{},button:{disabled:false},
      addEventListener(type,handler){this.listeners[type]=handler;},querySelector(){return this.button;},
      showModal(){this.open=true;},close(){this.open=false;}});
    return elements.get(id);
  }
  let account=null,mayNavigate=true,logoutCount=0,redirectCount=0,next=0;
  const api={request:async()=>account,post:async(url,body)=>{
    requests.push(url);
    if(url.endsWith('/session')){
      account={email:`preview-${++next}`,display_name:body.display_name};
      sessions.set(`key-${next}`,account);return {resume_key:`key-${next}`};
    }
    if(url.endsWith('/resume')){account=sessions.get(body.resume_key);return {};}
    if(url.endsWith('/profile')){account.display_name=body.display_name;return {};}
    if(url.endsWith('/logout')){account=null;return {};}
    throw new Error(`Unexpected endpoint: ${url}`);
  }};
  const storage={getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)};
  const service=StatementIdentity.create({elements:$,api,escape:value=>value,storage,mayNavigate:()=>mayNavigate,
    onLogout:()=>logoutCount++,redirect:()=>redirectCount++,restart:()=>service.load()});
  const submit=async name=>{$('display-name').value=name;await $('name-form').listeners.submit({preventDefault(){},currentTarget:$('name-form')});};
  assert.equal(await service.initialize({preview:true}),false);assert.equal($('name-dialog').open,true);
  await submit('同名审阅者');assert.equal(service.current.email,'preview-1');
  await $('profile-button').onclick();await submit('修改后的姓名');
  assert.equal(requests.at(-1),'./api/profile');assert.equal(service.current.email,'preview-1','Editing a name keeps the same account');
  mayNavigate=false;await $('logout').onclick();assert.equal(logoutCount,0,'Unsaved-opinion navigation guard also protects account switching');
  mayNavigate=true;await $('logout').onclick();await submit('修改后的姓名');
  assert.equal(service.current.email,'preview-2','A second preview identity stays separate even when the display name matches');
  await $('logout').onclick();$('resume-identity').value='0';await $('resume-button').onclick();
  assert.equal(service.current.email,'preview-1','Only the matching resume credential restores the earlier identity');
  assert.equal(redirectCount,0);
  account={email:'production@example.test',display_name:''};
  assert.equal(await service.initialize({preview:false}),false);await submit('生产审阅者');
  assert.equal(requests.at(-1),'./api/profile','Production identity uses the authenticated profile endpoint');
  await $('logout').onclick();assert.equal(redirectCount,1);
}

(async()=>{await testAPI();testGraph();await testIdentity();console.log('Statement API, identity isolation and dependency graph tests passed.');})().catch(error=>{console.error(error);process.exitCode=1;});
