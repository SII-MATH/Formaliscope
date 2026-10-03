"use strict";
(() => {
  function create({elements:$, api, escape, restart, mayNavigate, onLogout,
    storage, redirect=()=>location.replace(new URL('./login',document.baseURI))}) {
    let config=null, identity=null, nameMode="new";

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
    function unauthorized() {if(config?.preview)show('new');else redirect();}
    async function load({optional=false}={}) {
      identity=await api.request('./api/auth/me',{optionalUnauthorized:optional});
      if(!identity){show('new');return null;}
      $('reviewer-name').textContent=identity.display_name||identity.user_id||identity.email;
      $('recovery-button').hidden=config?.auth_mode!=='name';
      $('admin-link').hidden=!identity.is_admin;$('preview-badge').hidden=!config.preview;
      return identity;
    }
    async function initialize(nextConfig) {
      config=nextConfig;
      if(!await load({optional:config.preview}))return false;
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
    $('profile-button').onclick=()=>{$('account-menu').open=false;show('edit');};
    $('recovery-button').onclick=()=>{
      $('account-menu').open=false;$('recovery-result').hidden=true;$('generate-recovery').hidden=false;
      $('account-recovery-code').value='';$('recovery-error').textContent='';
      $('recovery-help').textContent='原恢复码将失效，其他设备上的登录也会退出。当前浏览器继续保留身份。';
      $('recovery-dialog').showModal();
    };
    $('recovery-form').addEventListener('submit',async event=>{
      event.preventDefault();$('generate-recovery').disabled=true;
      try{
        const result=await api.post('./api/auth/recovery',{});
        $('account-recovery-code').value=result.recovery_code;$('recovery-result').hidden=false;
        $('generate-recovery').hidden=true;$('recovery-help').textContent='新恢复码已生效，原恢复码已失效。请先保存再关闭。';
      }catch(error){$('recovery-error').textContent=error.message;}
      finally{$('generate-recovery').disabled=false;}
    });
    $('copy-account-recovery').onclick=async()=>{
      try{await navigator.clipboard.writeText($('account-recovery-code').value);$('recovery-error').textContent='已复制，请私下保存。';}
      catch{$('account-recovery-code').select();$('recovery-error').textContent='请手动复制选中的恢复码。';}
    };
    $('download-account-recovery').onclick=()=>{
      const url=URL.createObjectURL(new Blob(['Formaliscope 私人恢复码（勿分享）\n'+$('account-recovery-code').value+'\n'],{type:'text/plain;charset=utf-8'}));
      const link=document.createElement('a');link.href=url;link.download='formaliscope-recovery.txt';link.click();
      setTimeout(()=>URL.revokeObjectURL(url),1000);
    };
    function clearRecovery(){$('account-recovery-code').value='';$('recovery-result').hidden=true;}
    $('close-recovery').onclick=()=>{$('recovery-dialog').close();clearRecovery();};
    $('recovery-dialog').addEventListener('close',clearRecovery);
    $('logout').onclick=async()=>{
      if(!mayNavigate())return;
      try{await api.post('./api/auth/logout',{});identity=null;onLogout();unauthorized();}
      catch(error){$('save-global').textContent=error.message;}
    };

    return Object.freeze({initialize,load,show,unauthorized,get current(){return identity;}});
  }
  (typeof window!=="undefined"?window:globalThis).StatementIdentity=Object.freeze({create});
})();
