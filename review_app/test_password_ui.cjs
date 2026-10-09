'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const settle=async()=>{for(let i=0;i<5;i++)await new Promise(resolve=>setImmediate(resolve));};
const response=(data,status=200)=>({ok:status>=200&&status<300,status,json:async()=>data});
function fixture(script,handler){
  const elements=new Map(),calls=[],redirects=[];
  function $(id){
    if(!elements.has(id))elements.set(id,{value:'',hidden:false,disabled:false,textContent:'',listeners:{},attributes:{},
      classList:{toggle(){}},focus(){},select(){},setAttribute(name,value){this.attributes[name]=value;},
      addEventListener(name,callback){this.listeners[name]=callback;}});
    return elements.get(id);
  }
  const context={document:{getElementById:$,baseURI:'https://example.org/review/login'},
    location:{replace(url){redirects.push(String(url));}},URL,Blob,setTimeout,navigator:{},
    fetch:async(path,options)=>{calls.push({path,options});return handler(path,options);}};
  vm.createContext(context);vm.runInContext(fs.readFileSync(__dirname+'/static/'+script,'utf8'),context);
  return {$,calls,redirects,submit:id=>$(id).listeners.submit({preventDefault(){}})};
}
async function login(){
  const f=fixture('login.js',(path)=>{
    if(path==='./api/config')return response({auth_mode:'name'});
    if(path==='./api/auth/password-login')return response({must_change_password:true});
    if(path==='./api/auth/register')return response({recovery_code:'test-recovery',user_id:'u_existing'});
    throw Error(path);
  });await settle();assert.equal(f.$('name-entry').hidden,false);
  f.$('password-tab').onclick();assert.equal(f.$('password-form').hidden,false);assert.equal(f.$('register-form').hidden,true);
  f.$('account').value='  u_existing  ';f.$('login-password').value='initial-password';
  await f.submit('password-form');assert.deepEqual(JSON.parse(f.calls.at(-1).options.body),{account:'u_existing',password:'initial-password'});
  assert.equal(f.$('login-password').value,'');assert.equal(f.redirects.at(-1),'https://example.org/review/password');
  f.$('new-tab').onclick();f.$('display-name').value='测试用户';f.$('register-password').value='my-private-password';
  await f.submit('register-form');assert.equal(f.$('new-account-id').value,'u_existing');assert.equal(f.$('register-password').value,'');
  assert.equal(f.$('recovery-panel').hidden,false);
  const failure=fixture('login.js',path=>response(path==='./api/config'?{auth_mode:'name'}:{error:'账号或密码不正确'},path==='./api/config'?200:401));
  await settle();await failure.submit('password-form');assert.equal(failure.redirects.length,0);
  assert.equal(failure.$('password-login').disabled,false);assert.match(failure.$('message').textContent,/不正确/);
}
async function change(){
  const f=fixture('password.js',path=>response(path==='./api/auth/me'?{auth_mode:'name',user_id:'u_original',must_change_password:true}:{ok:true}));
  await settle();assert.equal(f.$('password-account').value,'u_original');assert.equal(f.$('back').hidden,true);
  f.$('current-password').value='old-password';f.$('new-password').value='new-password';f.$('confirm-password').value='mismatch-password';
  await f.submit('change-password-form');assert.equal(f.calls.length,1);assert.match(f.$('message').textContent,/不一致/);
  f.$('confirm-password').value='new-password';await f.submit('change-password-form');
  assert.deepEqual(JSON.parse(f.calls.at(-1).options.body),{current_password:'old-password',new_password:'new-password'});
  for(const id of ['current-password','new-password','confirm-password'])assert.equal(f.$(id).value,'');
  assert.equal(f.redirects.at(-1),'https://example.org/review/');
}
(async()=>{await login();await change();console.log('Password login, initial-change redirect, account IDs, mismatch handling and password field clearing passed.');})().catch(error=>{console.error(error);process.exitCode=1;});
