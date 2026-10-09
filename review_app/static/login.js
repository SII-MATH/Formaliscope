"use strict";
(() => {
  const $ = (id) => document.getElementById(id);
  $('environment-hint').hidden=!['localhost','127.0.0.1','[::1]'].includes(location.hostname);
  let email = "";
  const status = (message, error = false) => {
    $("message").textContent = message;
    $("message").classList.toggle("error", error);
  };
  async function post(path, payload) {
    const response = await fetch(path, {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload), cache: "no-store",
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "请求失败，请稍后再试");
    return data;
  }
  const enter = () => location.replace(new URL("./", document.baseURI));
  function tab(mode) {
    for(const [name,form] of [['password','password-form'],['new','register-form']]){
      $(form).hidden=name!==mode;$(name+'-tab').setAttribute('aria-pressed',String(name===mode));
    }
    status(""); $(mode==='new'?"display-name":"account").focus();
  }
  $("new-tab").onclick=()=>tab('new');$("password-tab").onclick=()=>tab('password');
  $("password-form").addEventListener('submit',async event=>{
    event.preventDefault();$('password-login').disabled=true;status('正在登录…');
    try{
      const result=await post('./api/auth/password-login',{account:$('account').value.trim(),password:$('login-password').value});
      $('login-password').value='';
      if(result.must_change_password)location.replace(new URL('./password',document.baseURI));else enter();
    }catch(error){status(error.message,true);}
    finally{$('password-login').disabled=false;}
  });
  $("register-form").addEventListener("submit",async event=>{
    event.preventDefault(); $("register").disabled=true;status("正在创建身份…");
    try {
      const result=await post("./api/auth/register",{display_name:$("display-name").value.trim(),password:$('register-password').value});
      $('register-password').value='';enter();
    } catch(error) { status(error.message,true); }
    finally { $("register").disabled=false; }
  });
  fetch("./api/config",{cache:"no-store"}).then(async response=>{
    if(!response.ok)throw new Error("登录配置读取失败，请刷新重试");
    const config=await response.json();
    if(config.auth_mode==="name") {AuthenticationNotice.show(config.authentication_notice);$("name-entry").hidden=false;$("account").focus();}
    else if(config.preview) {enter();}
    else {$("email-form").hidden=false;$("login-hint").textContent="输入邮箱获取一次性验证码。";}
  }).catch(error=>status(error.message,true));
  async function sendCode() {
    email = $("email").value.trim().toLowerCase();
    $("send").disabled = true;
    $("resend").disabled = true;
    status("正在发送验证码…");
    try {
      const result = await post("./api/auth/request-code", {email});
      $("code-form").hidden = false;
      $("code").focus();
      status(result.message);
    } catch (error) {
      status(error.message, true);
    } finally {
      $("send").disabled = false;
      $("resend").disabled = false;
    }
  }
  $("email-form").addEventListener("submit", (event) => {
    event.preventDefault();
    sendCode();
  });
  $("resend").addEventListener("click", sendCode);
  $("code-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    $("verify").disabled = true;
    status("正在验证…");
    try {
      await post("./api/auth/verify-code", {email, code: $("code").value.trim()});
      location.replace(new URL("./", document.baseURI));
    } catch (error) {
      status(error.message, true);
      $("verify").disabled = false;
    }
  });
})();
