"use strict";
(() => {
  const $=id=>document.getElementById(id), escape=v=>String(v??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
  const names={aligned:'通过',uncertain:'没看懂',misaligned:'不通过',partial:'部分对齐（历史）'};
  let data=null,page=0;
  function filtered(){return (data?.judgments||[]).filter(r=>($('reviewer').value==='all'||r.reviewer===$('reviewer').value)&&($('verdict').value==='all'||r.verdict===$('verdict').value)&&`${r.card_id} ${r.rationale}`.toLowerCase().includes($('search').value.trim().toLowerCase()));}
  function render(){
    const rows=filtered(),pages=Math.max(1,Math.ceil(rows.length/50));page=Math.min(page,pages-1);
    $('results').innerHTML=rows.slice(page*50,(page+1)*50).map(r=>`<tr><td><a href="./#${encodeURIComponent(r.card_id)}"><code>${escape(r.card_id.replace('statement::',''))}</code></a>${r.rationale?`<p>${escape(r.rationale)}</p>`:''}</td><td>${escape(r.display_name||r.reviewer)}<p>${escape((r.reviewer.startsWith('u_')||r.reviewer.endsWith('@preview.local'))?'身份 '+r.reviewer.slice(0,8):r.reviewer)}</p></td><td><span class="status-badge ${r.verdict==='misaligned'?'rejected':r.verdict==='uncertain'?'uncertain':'reviewed'}">${escape(names[r.verdict]||r.verdict)}</span></td><td>${escape(new Date(r.created_at).toLocaleString('zh-CN'))}</td></tr>`).join('');
    $('admin-empty').hidden=rows.length>0;$('page-label').textContent=`${page+1} / ${pages}`;$('prev-page').disabled=page===0;$('next-page').disabled=page===pages-1;
    $('summary-note').textContent=`当前筛选 ${rows.length} 份判断 · 历史共 ${data.history_count} 条 · ${data.stale_count} 条旧版本记录 · develop ${data.source_commit.slice(0,9)}`;
  }
  async function load(){
    try{const response=await fetch('./api/admin/summary',{cache:'no-store'});const body=await response.json();if(!response.ok)throw new Error(body.error||'读取失败');data=body;
      $('stat-reviewers').textContent=new Set(data.judgments.map(r=>r.reviewer)).size;
      for(const verdict of ['aligned','uncertain','misaligned'])$('stat-'+verdict).textContent=data.judgments.filter(r=>r.verdict===verdict).length;
      const selected=$('reviewer').value,reviewers=new Map(data.judgments.map(r=>[r.reviewer,r.display_name||r.reviewer]));
      $('reviewer').innerHTML='<option value="all">所有审阅者</option>'+[...reviewers].map(([id,name])=>`<option value="${escape(id)}">${escape(name)} · ${escape((id.startsWith('u_')||id.endsWith('@preview.local'))?id.slice(0,8):id)}</option>`).join('');
      if(reviewers.has(selected))$('reviewer').value=selected;$('admin-message').textContent='';render();
    }catch(error){$('admin-message').textContent=error.message;}
  }
  $('refresh').onclick=load;$('search').oninput=$('reviewer').onchange=$('verdict').onchange=()=>{page=0;if(data)render();};
  $('prev-page').onclick=()=>{page--;render();};$('next-page').onclick=()=>{page++;render();};
  $('export').onclick=()=>{if(!data)return;const blob=new Blob([JSON.stringify({...data,judgments:filtered()},null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='kip126-review-summary.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};load();
})();
