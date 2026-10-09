'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const settle=async()=>{for(let i=0;i<6;i++)await new Promise(resolve=>setImmediate(resolve));};
const response=(body,status=200)=>({ok:status>=200&&status<300,status,json:async()=>body});
const alpha={id:'alpha@new',repository_id:'alpha',repository_name:'Alpha',source_commit:'a'.repeat(40)};
const beta={id:'beta@new',repository_id:'beta',repository_name:'Beta',source_commit:'b'.repeat(40)};
const users=[
  {reviewer:'u_admin',display_name:'管理员',is_admin:true,disabled:false,current_count:0,history_count:0},
  {reviewer:'u_first',display_name:'同名 <script>bad()</script>',is_admin:false,disabled:false,current_count:1,history_count:2},
  {reviewer:'u_second',display_name:'同名',is_admin:false,disabled:false,current_count:0,history_count:0},
];
function fixture({global=true,hook=()=>null}={}){
  const elements=new Map(),calls=[];let href='http://review.example/admin?dataset=alpha@old';
  function $(id){
    if(!elements.has(id))elements.set(id,{value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,
      listeners:{},parentElement:{hidden:false},setAttribute(name,value){this[name]=value;},scrollIntoView(){},click(){},
      addEventListener(name,handler){this.listeners[name]=handler;},showModal(){this.open=true;},close(){this.open=false;this.listeners.close?.();}});
    return elements.get(id);
  }
  for(const id of ['user-role','user-status','reviewer','verdict'])$(id).value='all';
  const context={document:{getElementById:$,querySelector:()=>$('brand'),createElement:()=>({click(){}})},
    location:{get href(){return href;}},history:{state:null,replaceState(_,__,url){href=String(url);}},
    URL,URLSearchParams,Blob,setTimeout,console,
    async fetch(url,options){
      calls.push({url,options});const pending=hook(url,options);if(pending)return pending;
      if(url.startsWith('./api/auth/me'))return response({is_admin:true,can_view_users:global,auth_mode:'name',user_id:'u_admin'});
      if(url==='./api/admin/users')return response({users,datasets:[alpha,beta],stats:{registered:3,enabled:3,admins:1,reviewers:1}});
      if(url.startsWith('./api/admin/user?')){
        const params=new URL(url,'http://review.example').searchParams,user=users.find(item=>item.reviewer===params.get('reviewer'));
        return response({user,dataset:params.get('dataset')===beta.id?beta:alpha,current_count:1,history_count:26,next_cursor:params.get('cursor')==='0'?25:null,
          reviews:[{id:'row',card_id:'statement::alpha::X',title:'<b>条目</b>',rationale:'<img src=x onerror=bad()>',verdict:'aligned',status:'current',card_available:true,created_at:'2026-10-09T00:00:00Z'}]});
      }
      if(url.startsWith('./api/admin/summary'))return response({dataset:alpha,source_commit:alpha.source_commit,history_count:0,stale_count:0,judgments:[]});
      if(url==='./api/admin/reset-password')return response({ok:true,message:'密码已重置'});
      throw Error('Unexpected request: '+url);
    }};
  vm.createContext(context);vm.runInContext(fs.readFileSync(__dirname+'/static/admin.js','utf8'),context);
  return {calls,$,get url(){return href;},open(id){$('users').onclick({target:{closest:()=>({dataset:{user:id}})}});}};
}
async function directoryAndDetails(){
  const f=fixture();await settle();
  assert.equal(f.$('users-panel').hidden,false);assert.equal(f.$('summary-panel').hidden,true);
  assert.equal(f.$('stat-users').textContent,3);assert.match(f.$('users').innerHTML,/u_first/);assert.match(f.$('users').innerHTML,/u_second/);
  assert.doesNotMatch(f.$('users').innerHTML,/<script>/);assert.match(f.$('users').innerHTML,/&lt;script&gt;/);
  assert.equal(new URL(f.url).searchParams.get('dataset'),alpha.id);
  f.$('user-search').value='u_second';f.$('user-search').oninput();assert.doesNotMatch(f.$('users').innerHTML,/u_first/);assert.match(f.$('users').innerHTML,/u_second/);
  f.open('u_first');await settle();assert.equal(f.$('user-detail').hidden,false);assert.match(f.$('detail-id').textContent,/u_first/);
  assert.match(f.$('user-reviews').innerHTML,/&lt;img/);assert.doesNotMatch(f.$('user-reviews').innerHTML,/<img/);assert.match(f.$('user-reviews').innerHTML,/当前有效/);
  f.$('detail-dataset').value=beta.id;f.$('detail-dataset').onchange();await settle();
  assert.match(f.calls.at(-1).url,/dataset=beta%40new/);assert.match(f.$('user-reviews').innerHTML,/dataset=beta%40new/);
  f.$('detail-next').onclick();await settle();assert.match(f.calls.at(-1).url,/cursor=25/);assert.equal(f.$('detail-next').disabled,true);
  f.$('close-detail').onclick();assert.equal(f.$('user-detail').hidden,true);
  assert.ok(f.calls.every(call=>!call.options.method),'The backend only issues read requests');
}
async function staleDetailAndExpiry(){
  let finishFirst,finishExpired;
  const f=fixture({hook(url){
    if(url.includes('reviewer=u_first'))return new Promise(resolve=>{finishFirst=resolve;});
    if(url.includes('reviewer=u_second'))return new Promise(resolve=>{finishExpired=resolve;});
  }});await settle();f.open('u_first');await settle();f.open('u_second');await settle();
  finishFirst(response({user:users[1],dataset:alpha,reviews:[],current_count:99,history_count:99,next_cursor:null}));await settle();
  assert.match(f.$('detail-id').textContent,/u_second/);assert.doesNotMatch(f.$('detail-count').textContent,/99/);
  finishExpired(response({error:'登录已过期'},401));await settle();
  assert.equal(f.$('users-panel').hidden,true);assert.equal(f.$('users').innerHTML,'');assert.equal(f.$('user-reviews').innerHTML,'');
  assert.match(f.$('admin-message').textContent,/登录已过期/);
}
async function scopedAdminAndSummary(){
  const scoped=fixture({global:false});await settle();
  assert.equal(scoped.$('users-tab').hidden,true);assert.equal(scoped.$('summary-panel').hidden,false);
  assert.ok(!scoped.calls.some(call=>call.url==='./api/admin/users'));
  let finishOld;
  const f=fixture({hook(url){if(url==='./api/admin/summary?dataset=alpha%40new')return new Promise(resolve=>{finishOld=resolve;});}});
  await settle();f.$('summary-tab').onclick();await settle();
  f.$('summary-dataset').value=beta.id;f.$('summary-dataset').onchange();await settle();
  finishOld(response({dataset:alpha,source_commit:alpha.source_commit,history_count:999,stale_count:0,judgments:[]}));await settle();
  assert.doesNotMatch(f.$('summary-note').textContent,/999/);
  assert.equal(new URL(f.url).searchParams.get('dataset'),beta.id);
}
async function passwordReset(){
  const f=fixture();await settle();f.open('u_first');await settle();
  assert.equal(f.$('reset-password').hidden,false);f.$('reset-password').onclick();
  assert.match(f.$('reset-target').textContent,/u_first/);assert.equal(f.$('reset-password-dialog').open,true);
  f.$('reset-admin-password').value='administrator-password';
  await f.$('reset-password-form').listeners.submit({preventDefault(){}});
  const reset=f.calls.at(-1);assert.equal(reset.url,'./api/admin/reset-password');assert.equal(reset.options.method,'POST');
  assert.deepEqual(JSON.parse(reset.options.body),{reviewer:'u_first',current_password:'administrator-password'});
  assert.equal(f.$('reset-admin-password').value,'');assert.equal(f.$('reset-password-dialog').open,false);
  assert.match(f.$('admin-message').textContent,/已重置/);
  f.open('u_admin');await settle();assert.equal(f.$('reset-password').hidden,true);
  const lost=fixture({hook(url){if(url==='./api/admin/reset-password')return response({error:'权限已失效'},403);}});
  await settle();lost.open('u_first');await settle();lost.$('reset-password').onclick();
  lost.$('reset-admin-password').value='administrator-password';
  await lost.$('reset-password-form').listeners.submit({preventDefault(){}});
  assert.equal(lost.$('users').innerHTML,'');assert.equal(lost.$('users-panel').hidden,true);
  assert.equal(lost.$('reset-admin-password').value,'');
}
(async()=>{await directoryAndDetails();await staleDetailAndExpiry();await scopedAdminAndSummary();await passwordReset();console.log('Admin directory, reset confirmation, account isolation, pagination, role restrictions and stale responses passed.');})().catch(error=>{console.error(error);process.exitCode=1;});
