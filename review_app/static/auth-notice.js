"use strict";
(() => {
  let shown=false;
  function show(version){
    if(!version||shown)return;
    const key='formaliscope-auth-notice:'+new URL('./',document.baseURI).pathname+version;
    try{if(localStorage.getItem(key)==='seen')return;}catch{}
    shown=true;
    const dialog=document.createElement('dialog');dialog.className='auth-notice';
    dialog.setAttribute('aria-labelledby','auth-notice-title');
    dialog.innerHTML='<p class="auth-notice-label">FORMALISCOPE · 更新通知</p><h2 id="auth-notice-title">登录方式已更新</h2><p>我们已取消恢复码机制，所有用户现在使用<strong>账号 + 密码</strong>登录。账号可以填写注册姓名或账号 ID。</p><p>本次更新后，所有已有账号的初始密码统一为 <strong>12345678</strong>。请在首次登录后设置自己的密码；之后可在右上角账号菜单中手动修改。</p><p>忘记密码请联系管理员重置。原来的审阅记录继续保留，重名重复账号已归入同一主账号。</p><button type="button">我知道了</button>';
    dialog.addEventListener('close',()=>{try{localStorage.setItem(key,'seen');}catch{}dialog.remove();});
    dialog.querySelector('button').onclick=()=>dialog.close();
    document.body.append(dialog);dialog.showModal();
  }
  (typeof window!=='undefined'?window:globalThis).AuthenticationNotice=Object.freeze({show});
})();
