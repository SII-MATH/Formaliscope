"use strict";
(() => {
  const $=id=>document.getElementById(id);
  const escape=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const names={aligned:'通过',uncertain:'没看懂',misaligned:'不通过',partial:'部分对齐（历史）'};
  const date=value=>value?new Date(value).toLocaleString('zh-CN'):'尚未提交';
  const identity=id=>id.startsWith('u_')||id.endsWith('@preview.local')?id.slice(0,10):id;
  const badge=verdict=>`<span class="status-badge ${verdict==='misaligned'?'rejected':verdict==='uncertain'?'uncertain':'reviewed'}">${escape(names[verdict]||verdict)}</span>`;
  const suffix=id=>id===null?'':`?dataset=${encodeURIComponent(id)}`;
  const reviewLink=(dataset,id)=>`./${suffix(dataset)}#${encodeURIComponent(id)}`;
  let selectedDataset=new URL(location.href).searchParams.get('dataset');
  let data=null,directory=null,page=0,userPage=0,selectedUser=null,detailCursor=0,detail=null,view='summary';
  let canViewUsers=false,initialized=false,epoch=0,loadTurn=0,summaryTurn=0,usersTurn=0,detailTurn=0;
  let passwordMode=false,ownId=null,resetTarget=null;
  function clearPrivateViews(){
    ++epoch;++detailTurn;++summaryTurn;++usersTurn;
    directory=null;data=null;detail=null;selectedUser=null;
    for(const id of ['users','user-reviews','results'])$(id).innerHTML='';
    for(const id of ['users-panel','summary-panel','user-detail'])$(id).hidden=true;
    $('export').hidden=true;
    $('reset-password-dialog').close();resetTarget=null;$('reset-admin-password').value='';
  }
  async function request(path,payload){
    const started=epoch,response=await fetch(path,payload===undefined?{cache:'no-store'}:{cache:'no-store',method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)}),body=await response.json();
    if(body.password_change_required){clearPrivateViews();location.replace(new URL('./password',document.baseURI));}
    if(response.status===401||(response.status===403&&path.startsWith('./api/admin/'))){
      clearPrivateViews();
      $('admin-message').textContent=body.error||'登录已过期，请重新登录';
    }
    if(!response.ok)throw new Error(body.error||'读取失败');
    if(started!==epoch)throw new Error('登录状态已改变，请刷新后台');
    return body;
  }
  function setDataset(id){
    selectedDataset=id;$('review-link').href='./'+suffix(id);document.querySelector('.brand').href='./'+suffix(id);
    const url=new URL(location.href);if(id===null)url.searchParams.delete('dataset');else url.searchParams.set('dataset',id);
    history.replaceState(history.state,'',url);
  }
  function showView(next){
    view=next;$('users-panel').hidden=next!=='users';$('summary-panel').hidden=next!=='summary';$('export').hidden=next!=='summary';
    for(const name of ['users','summary'])$(name+'-tab').setAttribute('aria-current',name===next?'page':'false');
  }
  function filtered(){return (data?.judgments||[]).filter(row=>($('reviewer').value==='all'||row.reviewer===$('reviewer').value)&&($('verdict').value==='all'||row.verdict===$('verdict').value)&&`${row.card_id} ${row.rationale} ${row.display_name}`.toLowerCase().includes($('search').value.trim().toLowerCase()));}
  function renderSummary(){
    const rows=filtered(),pages=Math.max(1,Math.ceil(rows.length/50));page=Math.min(page,pages-1);
    $('results').innerHTML=rows.slice(page*50,(page+1)*50).map(row=>`<tr><td><a href="${reviewLink(data.dataset.id,row.card_id)}"><code>${escape(row.card_id.replace('statement::',''))}</code></a>${row.rationale?`<p>${escape(row.rationale)}</p>`:''}</td><td>${escape(row.display_name||row.reviewer)}<small>账号 ${escape(identity(row.reviewer))}</small></td><td>${badge(row.verdict)}</td><td>${escape(date(row.created_at))}</td></tr>`).join('');
    $('admin-empty').hidden=rows.length>0;$('page-label').textContent=`${page+1} / ${pages}`;$('prev-page').disabled=page===0;$('next-page').disabled=page===pages-1;
    $('summary-note').textContent=`当前筛选 ${rows.length} 份判断 · 历史共 ${data.history_count} 条 · ${data.stale_count} 条依据已变化的记录 · 源码提交 ${data.source_commit.slice(0,9)}`;
  }
  async function loadSummary(){
    const turn=++summaryTurn;data=null;$('results').innerHTML='';$('admin-empty').hidden=true;$('export').disabled=true;
    for(const id of ['stat-reviewers','stat-aligned','stat-uncertain','stat-misaligned'])$(id).textContent='—';
    $('summary-note').textContent='正在读取审阅汇总…';
    try{
      const body=await request('./api/admin/summary'+suffix(selectedDataset));if(turn!==summaryTurn)return;data=body;
      $('stat-reviewers').textContent=new Set(data.judgments.map(row=>row.reviewer)).size;
      for(const verdict of ['aligned','uncertain','misaligned'])$('stat-'+verdict).textContent=data.judgments.filter(row=>row.verdict===verdict).length;
      const selected=$('reviewer').value,reviewers=new Map(data.judgments.map(row=>[row.reviewer,row.display_name||row.reviewer]));
      $('reviewer').innerHTML='<option value="all">所有审阅者</option>'+[...reviewers].map(([id,name])=>`<option value="${escape(id)}">${escape(name)} · ${escape(identity(id))}</option>`).join('');
      $('reviewer').value=reviewers.has(selected)?selected:'all';$('admin-message').textContent='';$('export').disabled=false;renderSummary();
    }catch(error){if(turn===summaryTurn){$('admin-message').textContent=error.message;$('summary-note').textContent='未能读取汇总，请刷新重试。';}}
  }
  function role(user){return user.is_admin?'全局管理员':user.dataset_admin_count?'仓库版本管理员':'普通用户';}
  function filteredUsers(){
    const query=$('user-search').value.trim().toLowerCase(),selectedRole=$('user-role').value,status=$('user-status').value;
    return (directory?.users||[]).filter(user=>`${user.display_name} ${user.reviewer}`.toLowerCase().includes(query)&&
      (selectedRole==='all'||selectedRole==='admin'&&user.is_admin||selectedRole==='dataset-admin'&&!user.is_admin&&user.dataset_admin_count>0||selectedRole==='reviewer'&&!user.is_admin&&!user.dataset_admin_count)&&
      (status==='all'||status==='disabled'&&user.disabled||status==='enabled'&&!user.disabled));
  }
  function renderUsers(){
    const users=filteredUsers(),pages=Math.max(1,Math.ceil(users.length/25));userPage=Math.min(userPage,pages-1);
    $('users').innerHTML=users.slice(userPage*25,(userPage+1)*25).map(user=>`<tr class="${user.reviewer===selectedUser?'selected':''}"><td>${escape(user.display_name)}<small>账号 ${escape(identity(user.reviewer))}</small></td><td>${role(user)}<small>${user.disabled?'已停用':'正常启用'}</small></td><td>${user.current_count} / ${user.history_count}</td><td>${escape(date(user.last_review_at))}</td><td><button data-user="${escape(user.reviewer)}" aria-label="查看 ${escape(user.display_name)} 的审阅">查看审阅</button></td></tr>`).join('');
    $('users-empty').hidden=users.length>0;$('users-note').textContent=`共 ${directory.users.length} 个账号 · 当前筛选 ${users.length} 个`;
    $('users-page').textContent=`${userPage+1} / ${pages}`;$('users-prev').disabled=userPage===0;$('users-next').disabled=userPage===pages-1;
  }
  async function loadUsers(){
    const turn=++usersTurn;
    try{
      const body=await request('./api/admin/users');if(turn!==usersTurn)return;directory=body;
      for(const [id,key] of [['stat-users','registered'],['stat-enabled','enabled'],['stat-admins','admins'],['stat-participants','reviewers']])$(id).textContent=body.stats[key];
      const previousDetail=$('detail-dataset').value;
      const options=body.datasets.map(item=>`<option value="${escape(item.id)}">${escape(item.repository_name)} · ${escape(item.source_commit.slice(0,9))}</option>`).join('');
      for(const id of ['summary-dataset','detail-dataset'])$(id).innerHTML=options;
      if(!body.datasets.some(item=>item.id===selectedDataset))setDataset(body.datasets[0]?.id??null);
      $('summary-dataset').value=selectedDataset;$('detail-dataset').value=body.datasets.some(item=>item.id===previousDetail)?previousDetail:selectedDataset;
      $('admin-message').textContent='';renderUsers();
      if(selectedUser){if(body.users.some(user=>user.reviewer===selectedUser))await loadDetail(selectedUser);else closeDetail();}
    }catch(error){if(turn===usersTurn)$('admin-message').textContent=error.message;}
  }
  function closeDetail(){++detailTurn;selectedUser=null;detail=null;$('user-detail').hidden=true;if(directory)renderUsers();}
  async function loadDetail(userId,{cursor=0,scroll=false}={}){
    const turn=++detailTurn,user=directory?.users.find(item=>item.reviewer===userId);if(!user)return;
    selectedUser=userId;detailCursor=cursor;detail=null;
    $('user-detail').hidden=false;$('detail-name').textContent=user.display_name;$('detail-id').textContent=`账号 ${user.reviewer} · ${role(user)} · ${user.disabled?'已停用':'正常启用'}`;
    $('reset-password').hidden=!passwordMode||!canViewUsers||user.disabled||user.reviewer===ownId;
    $('user-reviews').innerHTML='';$('detail-empty').hidden=true;$('detail-count').textContent='';$('detail-message').textContent='正在读取已提交的审阅…';
    $('detail-prev').disabled=true;$('detail-next').disabled=true;$('detail-page').textContent='—';renderUsers();
    if(scroll)$('user-detail').scrollIntoView({behavior:'smooth',block:'start'});
    const params=new URLSearchParams({reviewer:userId,dataset:$('detail-dataset').value,cursor:String(cursor)});
    try{
      const body=await request('./api/admin/user?'+params);if(turn!==detailTurn)return;detail=body;
      $('detail-message').textContent='';$('detail-count').textContent=`当前版本有效 ${body.current_count} 份 · 此仓库历史保存 ${body.history_count} 条`;
      const states={current:'当前有效',superseded:'已被后续判断替代',stale:'依据已变化或条目已移除',historical:'旧版本审阅 · 未计入当前有效'};
      $('user-reviews').innerHTML=body.reviews.map(row=>`<tr><td>${row.card_available?`<a href="${reviewLink(body.dataset.id,row.current_card_id||row.card_id)}">${escape(row.title)}</a>`:escape(row.title)}<small>${escape(row.card_id)}</small>${row.rationale?`<p>${escape(row.rationale)}</p>`:''}${row.source_version?`<small>记录版本 ${escape(row.source_version.slice(0,9))}</small>`:''}${row.inherited_from_dataset?`<small>沿用来源 ${escape(row.inherited_from_dataset)}</small>`:''}</td><td>${badge(row.verdict)}</td><td>${states[row.status]}</td><td>${escape(date(row.created_at))}</td></tr>`).join('');
      $('detail-empty').hidden=body.reviews.length>0;$('detail-prev').disabled=cursor===0;$('detail-next').disabled=body.next_cursor===null;
      $('detail-page').textContent=`${Math.floor(cursor/25)+1} / ${Math.max(1,Math.ceil(body.history_count/25))}`;
    }catch(error){if(turn===detailTurn)$('detail-message').textContent=error.message;}
  }
  async function initialize(){
    const turn=++loadTurn;
    try{
      const me=await request('./api/auth/me'+suffix(selectedDataset));if(turn!==loadTurn)return;
      if(me.must_change_password){clearPrivateViews();location.replace(new URL('./password',document.baseURI));return;}
      if(!me.is_admin){clearPrivateViews();throw new Error('此入口仅对管理员开放');}
      passwordMode=me.auth_mode==='name';ownId=me.user_id;$('admin-password-link').hidden=!passwordMode;
      canViewUsers=!!me.can_view_users;$('users-tab').hidden=!canViewUsers;$('summary-dataset').parentElement.hidden=!canViewUsers;
      if(canViewUsers){await loadUsers();if(turn!==loadTurn)return;if(!directory)return;showView(initialized?view:'users');}
      else {++usersTurn;directory=null;closeDetail();$('users').innerHTML='';showView('summary');}
      initialized=true;
      if(view==='summary')await loadSummary();
    }catch(error){if(turn===loadTurn)$('admin-message').textContent=error.message;}
  }
  $('users-tab').onclick=()=>{if(canViewUsers)showView('users');};
  $('summary-tab').onclick=()=>{showView('summary');loadSummary();};
  $('refresh').onclick=initialize;
  $('summary-dataset').onchange=()=>{setDataset($('summary-dataset').value);page=0;loadSummary();};
  $('search').oninput=$('reviewer').onchange=$('verdict').onchange=()=>{page=0;if(data)renderSummary();};
  $('prev-page').onclick=()=>{if(page>0){page--;renderSummary();}};$('next-page').onclick=()=>{page++;renderSummary();};
  $('user-search').oninput=$('user-role').onchange=$('user-status').onchange=()=>{userPage=0;if(directory)renderUsers();};
  $('users-prev').onclick=()=>{if(userPage>0){userPage--;renderUsers();}};$('users-next').onclick=()=>{userPage++;renderUsers();};
  $('users').onclick=event=>{const button=event.target.closest('[data-user]');if(button)loadDetail(button.dataset.user,{scroll:true});};
  $('close-detail').onclick=closeDetail;$('detail-dataset').onchange=()=>{if(selectedUser)loadDetail(selectedUser);};
  $('reset-password').onclick=()=>{
    const user=directory?.users.find(item=>item.reviewer===selectedUser);
    if(!passwordMode||!canViewUsers||!user||user.disabled||user.reviewer===ownId)return;
    resetTarget=user.reviewer;$('reset-target').textContent=`${user.display_name} · ${user.reviewer}`;
    $('reset-admin-password').value='';$('reset-message').textContent='';$('reset-password-dialog').showModal();
  };
  $('reset-password-dialog').addEventListener('close',()=>{resetTarget=null;$('reset-admin-password').value='';});
  $('cancel-reset').onclick=()=>{$('reset-password-dialog').close();};
  $('reset-password-form').addEventListener('submit',async event=>{
    event.preventDefault();if(!resetTarget)return;$('confirm-reset').disabled=true;
    try{
      const result=await request('./api/admin/reset-password',{reviewer:resetTarget,current_password:$('reset-admin-password').value});
      $('reset-password-dialog').close();$('admin-message').textContent=result.message;
    }catch(error){$('reset-message').textContent=error.message;}
    finally{$('confirm-reset').disabled=false;$('reset-admin-password').value='';}
  });
  $('detail-prev').onclick=()=>{if(selectedUser&&detailCursor>0)loadDetail(selectedUser,{cursor:Math.max(0,detailCursor-25)});};
  $('detail-next').onclick=()=>{if(selectedUser&&detail&&detail.next_cursor!==null)loadDetail(selectedUser,{cursor:detail.next_cursor});};
  $('export').onclick=()=>{if(!data)return;const blob=new Blob([JSON.stringify({...data,judgments:filtered()},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='formaliscope-review-summary.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};
  initialize();
})();
