'use strict';
const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
function fixture(storage,baseURI='https://example.org/formaliscope/login'){
  const dialogs=[];
  const document={baseURI,body:{append(dialog){dialogs.push(dialog);}},createElement(){
    return {listeners:{},button:{},innerHTML:'',setAttribute(){},querySelector(){return this.button;},
      addEventListener(event,fn){this.listeners[event]=fn;},showModal(){this.open=true;},
      close(){this.open=false;this.listeners.close();},remove(){this.removed=true;}};
  }};
  const context={document,URL,localStorage:storage};vm.createContext(context);
  vm.runInContext(fs.readFileSync(__dirname+'/static/auth-notice.js','utf8'),context);
  return {show:context.AuthenticationNotice.show,dialogs};
}
const saved=new Map(),storage={getItem:key=>saved.get(key),setItem:(key,value)=>saved.set(key,value)};
const first=fixture(storage);first.show('password-only-v1');assert.equal(first.dialogs.length,1);
assert.match(first.dialogs[0].innerHTML,/12345678/);assert.match(first.dialogs[0].innerHTML,/已取消恢复码/);
first.show('password-only-v1');assert.equal(first.dialogs.length,1);
first.dialogs[0].button.onclick();assert.equal(first.dialogs[0].removed,true);
const again=fixture(storage);again.show('password-only-v1');assert.equal(again.dialogs.length,0);
const other=fixture(storage,'https://example.org/another/login');other.show('password-only-v1');assert.equal(other.dialogs.length,1);
const privateBrowser=fixture({getItem(){throw Error('disabled');},setItem(){throw Error('disabled');}});
privateBrowser.show('password-only-v1');privateBrowser.dialogs[0].button.onclick();
console.log('Authentication update notice, acknowledgement, path isolation and unavailable storage passed.');
