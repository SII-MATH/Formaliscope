"use strict";
(() => {
  const $=id=>document.getElementById(id);
  const status=(text,error=false)=>{$('message').textContent=text;$('message').classList.toggle('error',error);};
  const enter=()=>location.replace(new URL('./',document.baseURI));
  async function request(path,body){
    const response=await fetch(path,body===undefined?{cache:'no-store'}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body),cache:'no-store'});
    const data=await response.json();
    if(response.status===401)location.replace(new URL('./login',document.baseURI));
    if(!response.ok)throw new Error(data.error||'请求失败，请重试');
    return data;
  }
  $('change-password-form').addEventListener('submit',async event=>{
    event.preventDefault();
    if($('new-password').value!==$('confirm-password').value){status('两次输入的新密码不一致',true);return;}
    $('change-password').disabled=true;
    try{
      await request('./api/auth/password',{current_password:$('current-password').value,new_password:$('new-password').value});
      for(const id of ['current-password','new-password','confirm-password'])$(id).value='';
      status('密码已修改，正在进入审阅…');enter();
    }catch(error){status(error.message,true);}
    finally{$('change-password').disabled=false;}
  });
  $('back').onclick=enter;
  $('password-logout').onclick=async()=>{
    try{await request('./api/auth/logout',{});location.replace(new URL('./login',document.baseURI));}
    catch(error){status(error.message,true);}
  };
  request('./api/auth/me').then(me=>{
    if(me.auth_mode!=='name'){enter();return;}
    $('password-account').value=me.user_id;
    if(me.must_change_password){$('back').hidden=true;$('password-hint').textContent='当前使用初始密码，请先设置自己的密码，再进入工作台。忘记密码请联系管理员重置。';}
  }).catch(error=>status(error.message,true));
})();
