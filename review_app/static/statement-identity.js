"use strict";
(() => {
  function create({elements:$, api, escape, restart, mayNavigate, onLogout,
    storage, redirect=()=>location.replace(new URL('./login',document.baseURI))}) {
    let config=null, identity=null, nameMode="new",generation=0;

    function remembered() {
      try{
        const saved=JSON.parse((storage||globalThis.localStorage).getItem('kip126-preview-identities')||'[]');
        return Array.isArray(saved)?saved.filter(item=>item&&typeof item.email==='string'&&typeof item.name==='string'&&typeof item.key==='string'):[];
      }catch{return [];}
    }
    function remember(entry) {
      try{
        const list=remembered().filter(item=>item.email!==entry.email);list.push(entry);
        (storage||globalThis.localStorage).setItem('kip126-preview-identities',JSON.stringify(list));
      }catch{}
    }
    function show(mode) {
      nameMode=mode;$('name-error').textContent='';$('display-name').value=mode==='edit'?identity?.display_name||'':'';
      const saved=config?.preview&&mode==='new'?remembered():[];
      $('saved-identities').hidden=!saved.length;
      $('resume-identity').innerHTML=saved.map((item,index)=>`<option value="${index}">${escape(item.name)} · 身份 ${index+1}</option>`).join('');
      if(!$('name-dialog').open)$('name-dialog').showModal();
    }
    function unauthorized() {++generation;identity=null;if(config?.preview)show('new');else redirect();}
    async function load({optional=false,initialIdentity=undefined}={}) {
      const turn=++generation;
      const value=initialIdentity===undefined?await api.request('./api/auth/me',{optionalUnauthorized:optional}):initialIdentity;
      if(turn!==generation)return null;
      identity=value;
      if(!identity){if(optional)show('new');else unauthorized();return null;}
      if(identity.must_change_password){location.replace(new URL('./password',document.baseURI));return null;}
      $('reviewer-name').textContent=identity.display_name||identity.user_id||identity.email;
      $('password-button').hidden=config?.auth_mode!=='name';
      $('admin-link').hidden=!identity.is_admin;$('preview-badge').hidden=!config.preview;
      return identity;
    }
    async function initialize(nextConfig,{initialIdentity}={}) {
      config=nextConfig;
      if(!await load({optional:config.preview,initialIdentity})||!identity)return false;
      if(!identity.display_name){show('edit');return false;}
      return true;
    }

    $('name-dialog').addEventListener('cancel',event=>{if(!identity?.display_name)event.preventDefault();});
    $('name-form').addEventListener('submit',async event=>{
      event.preventDefault();const name=$('display-name').value.trim(),button=event.currentTarget.querySelector('button');
      if(!name)return;button.disabled=true;
      try{
        const result=await api.post(nameMode==='new'&&config.preview?'./api/preview/session':'./api/profile',{display_name:name});
        $('name-dialog').close();await restart();
        if(config.preview){
          const old=remembered().find(item=>item.email===identity.email);
          if(result.resume_key||old)remember({email:identity.email,name,key:result.resume_key||old.key});
        }
      }catch(error){$('name-error').textContent=error.message;}
      finally{button.disabled=false;}
    });
    $('resume-button').onclick=async()=>{
      const entry=remembered()[Number($('resume-identity').value)];if(!entry)return;
      $('resume-button').disabled=true;
      try{await api.post('./api/preview/resume',{resume_key:entry.key});$('name-dialog').close();await restart();}
      catch(error){$('name-error').textContent=error.message;}
      finally{$('resume-button').disabled=false;}
    };
    $('profile-button').onclick=async()=>{if(!await mayNavigate())return;$('account-menu').open=false;show('edit');};
    $('password-button').onclick=async()=>{if(!await mayNavigate())return;location.assign(new URL('./password',document.baseURI));};
    $('logout').onclick=async()=>{
      if(!await mayNavigate())return;
      try{await api.post('./api/auth/logout',{});onLogout();unauthorized();}
      catch(error){$('save-global').textContent=error.message;}
    };

    return Object.freeze({initialize,load,show,unauthorized,get current(){return identity;}});
  }
  (typeof window!=="undefined"?window:globalThis).StatementIdentity=Object.freeze({create});
})();
