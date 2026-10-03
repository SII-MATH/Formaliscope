"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? "").replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;").replaceAll('"',"&quot;");
  const verdictNames = {aligned:"通过", uncertain:"没看懂", misaligned:"不通过", partial:"部分对齐（历史）"};
  let catalog=[], byId=new Map(), selected=null, card=null, sequence=0, query="", page=0, view="list";
  let pendingRequest=null, saving=false, dirty=false, moduleSequence=0;
  const PAGE_SIZE=60;
  let mathLoading=false, mathTimer=null;
  const directories=window.StatementDirectories;
  let directory=new URL(location.href).searchParams.get('directory')||'', directoryNodes=new Map(), catalogInfo=null;
  const expandedDirectories=new Set(['']);
  const labels=window.ReviewLabels;
  const selectedLabels=labels.normalizeSelection((new URL(location.href).searchParams.get('labels')||'').split(','));
  let labelTab='content';
  const api=window.StatementAPI.create({onUnauthorized:()=>identities.unauthorized()});
  const json=api.request,post=api.post,evidence=api.evidence;
  const graph=window.StatementGraph.create({elements:$,escape,reviewState:labels.reviewState,verdictNames,
    isHighPriority:c=>priorityScore(c)>=2,
    onSelect:id=>openCard(id).then(()=>locateList())});
  const identities=window.StatementIdentity.create({elements:$,api,escape,restart:start,mayNavigate,onLogout:resetSession});

  function displayName(c){return c.declaration?.split('.').pop()||c.title||c.id;}
  function titleZh(c){return c.title_zh||(/[\u3400-\u9fff]/.test(c.title||'')?c.title:'');}
  function priorityScore(c){return c._labels?.includes('priority/p0')?3:c._labels?.includes('priority/p1')?2:c._labels?.includes('priority/p2')?1:0;}
  function renderProvenance(c){
    const analysis=c.enrichment,summary=$('label-provenance-summary');
    summary.textContent=analysis?`角色／主题：Agent 候选标注 · 优先度：${analysis.priority?.level?'Agent 候选标注':'尚未标注'} · 状态：本人记录`:'角色／主题／优先度：索引规则候选 · 状态：本人记录';
    const details=[];
    if(analysis?.classification?.rationale_zh)details.push(`分类依据：${analysis.classification.rationale_zh}`);
    if(analysis?.priority?.reason_zh)details.push(`优先度依据：${analysis.priority.reason_zh}`);
    if(analysis?.provenance){
      const source=analysis.provenance;
      if(source.model)details.push(`生成模型：${source.model}`);
      if(source.created_at)details.push(`生成时间：${new Date(source.created_at).toLocaleString('zh-CN')}`);
      if(source.context_completeness)details.push(`上下文：${{unknown:'完整性待检查',partial:'仍有待补内容',complete:'已收集完整'}[source.context_completeness]}`);
    }
    for(const unresolved of analysis?.readback?.unresolved||[])details.push(`待解释对象 ${unresolved.object}：${unresolved.reason}`);
    for(const item of analysis?.evidence||[])details.push(`依据位置：${item.file}:${item.line_start}${item.line_end!==item.line_start?`–${item.line_end}`:''}`);
    $('enrichment-provenance').hidden=!details.length;$('enrichment-provenance').textContent=details.join('\n');
  }
  function labelChip(id,{button=false,count=null,remove=false}={}){
    const item=labels.definitions.get(id);if(!item)return '';
    const content=`<span>${escape(item.name)}</span>${count===null?'':`<small>${count.toLocaleString()}</small>`}${remove?'<b aria-hidden="true">×</b>':''}`;
    return button?`<button class="review-label label-${item.color} ${selectedLabels.has(id)?'selected':''}" data-label="${id}" aria-label="${remove?'移除':'筛选'} ${escape(item.name)}" aria-pressed="${selectedLabels.has(id)}" title="${escape(item.source)}">${content}</button>`:`<span class="review-label label-${item.color}" title="${escape(item.source)}">${content}</span>`;
  }
  function renderCardLabels(){
    const tags=byId.get(selected)?._labels||[];
    $('card-labels').innerHTML=tags.filter(id=>id.startsWith('role/')||id.startsWith('priority/')).map(id=>labelChip(id)).join('');
    $('more-card-labels').innerHTML=tags.map(id=>labelChip(id)).join('');
  }
  function renderLabelUI(){
    const focusId=document.activeElement?.dataset.label,focusContainer=document.activeElement?.closest('#active-labels,#label-groups')?.id;
    const base=baseRows(), counts=new Map();
    for(const group of labels.groups){
      for(const row of base){if(!labels.matches(row._labels,selectedLabels,group.id))continue;for(const id of row._labels){if(labels.definitions.get(id)?.group===group.id)counts.set(id,(counts.get(id)||0)+1);}}
    }
    $('active-labels').hidden=!selectedLabels.size;
    $('active-labels').innerHTML=[...selectedLabels].map(id=>labelChip(id,{button:true,remove:true})).join('');
    $('label-filter-count').textContent=selectedLabels.size;
    $('reset-labels').hidden=!selectedLabels.size;
    $('label-outside-notice').hidden=!selected||!selectedLabels.size||labels.matches(byId.get(selected)?._labels||[],selectedLabels);
    if($('labels-dialog').open){
      const visibleGroups=labels.groups.filter(group=>['role','topic'].includes(group.id)===(labelTab==='content'));
      $('label-groups').innerHTML=visibleGroups.map(group=>`<fieldset><legend>${escape(group.name)}</legend><p>${escape(group.source)}</p><div>${group.items.map(item=>labelChip(item.id,{button:true,count:counts.get(item.id)||0})).join('')}</div></fieldset>`).join('');
      $('label-tabs').querySelectorAll('button').forEach(button=>{const active=button.dataset.labelTab===labelTab;button.classList.toggle('active',active);button.setAttribute('aria-pressed',active);});
      $('label-matched-count').textContent=`匹配 ${rows().length.toLocaleString()} 条`;
    }
    renderCardLabels();
    if(focusId&&focusContainer)$(focusContainer).querySelector(`[data-label="${focusId}"]`)?.focus({preventScroll:true});
  }
  async function changeLabel(id){
    if(!labels.definitions.has(id)||!mayNavigate())return;
    if(selectedLabels.has(id))selectedLabels.delete(id);else selectedLabels.add(id);
    await refreshSelection();
  }
  async function refreshSelection(){
    page=0;updateLocation();renderList();
    const list=rows();if(list.length&&!list.some(row=>row.id===selected))await openCard(list[0].id,true);
  }
  function clearLabels(){selectedLabels.clear();page=0;updateLocation();renderList();}

  function updateLocation(id=selected){
    const url=new URL(location.href);
    if(directory)url.searchParams.set('directory',directory);else url.searchParams.delete('directory');
    if(selectedLabels.size)url.searchParams.set('labels',[...selectedLabels].sort().join(','));else url.searchParams.delete('labels');
    url.hash=id?encodeURIComponent(id):'';history.replaceState(null,'',url);
  }
  function directoryRows(){return catalog.filter(c=>directories.matchesPath(c.module_file,directory));}
  function renderDirectoryScope(){
    const fileScope=directoryNodes.get(directory)?.file;
    $('directory-path').textContent=directory||'全部源码';
    $('directory-path').title=directory||'全库源码索引';
    $('scope-notice').hidden=!selected||directories.matchesPath(byId.get(selected)?.module_file,directory);
    $('progress-heading').textContent=directory?(fileScope?'当前文件审阅进度':'当前目录审阅进度'):'索引标注情况';
    $('progress-note').textContent=directory?(fileScope?'仅统计当前文件，不受检索和状态筛选影响。':'包含子目录；进度不受检索和状态筛选影响。'):'全库索引不代表全部必审，请选择目录范围。';
  }
  function renderDirectoryTree(){
    const search=$('directory-search').value.trim().toLowerCase();
    let html='';
    function row(path,depth){
      const node=directoryNodes.get(path);if(!node)return;
      const open=expandedDirectories.has(path);
      html+=`<div class="directory-row directory-depth-${Math.min(depth,12)} ${directory===path?'selected':''}">${node.file?'<span class="directory-toggle file-icon">◇</span>':`<button class="directory-toggle" data-expand="${escape(path)}" aria-label="${open?'收起':'展开'} ${escape(path)}" aria-expanded="${open}">${open?'▾':'▸'}</button>`}<button class="directory-select" data-directory="${escape(path)}" aria-label="选择 ${escape(path)}"><span>${escape(search?path:node.name)}</span><small>${node.count.toLocaleString()} 条</small></button></div>`;
      if(!search&&open)node.children.forEach(child=>row(child,depth+1));
    }
    if(search){[...directoryNodes.keys()].filter(path=>path&&path.toLowerCase().includes(search)).slice(0,150).forEach(path=>row(path,0));}
    else directoryNodes.get('')?.children.forEach(path=>row(path,0));
    $('directory-tree').innerHTML=html||'<p class="directory-no-results">没有匹配的目录或文件。</p>';
  }
  async function chooseDirectory(path){
    if(!directoryNodes.has(path)||!mayNavigate())return;
    directory=path;query='';page=0;$('search').value='';$('scope').value='all';
    $('directory-dialog').close();updateLocation();renderList();renderDirectoryScope();
    if(selected&&!directories.matchesPath(byId.get(selected)?.module_file,directory)){
      const first=rows()[0];if(first)await openCard(first.id,true);
    }
    locateList();
  }

  function baseRows(){
    const scope=$("scope").value;
    return directoryRows().filter(c=>
      (scope!=="main" || c.role==="主定理" || c.role==="直接依赖") &&
      (scope!=="interface" || ['structure','class'].includes(c.kind) || c.module_file?.includes('Challenge')) &&
      `${c.title} ${c.title_zh||''} ${c.reading_summary_zh||''} ${c.declaration} ${c.module_file} ${c.role} ${(c._labels||[]).map(id=>labels.definitions.get(id)?.name||'').join(' ')}`.toLowerCase().includes(query)
    );
  }
  function rows(){
    return baseRows().filter(c=>labels.matches(c._labels,selectedLabels)).sort((a,b)=>$("sort").value==="name" ? a.declaration.localeCompare(b.declaration) :
      priorityScore(b)-priorityScore(a) || b.priority-a.priority || a.declaration.localeCompare(b.declaration));
  }
  function renderList(){
    updateFilter();
    const list=rows(), pages=Math.max(1,Math.ceil(list.length/PAGE_SIZE));
    page=Math.min(page,pages-1);
    $("card-list").innerHTML=list.slice(page*PAGE_SIZE,(page+1)*PAGE_SIZE).map(c=>
      `<button class="list-row ${c.id===selected?"active":""} ${labels.reviewState(c)==="pending"?"":labels.reviewState(c)==="misaligned"?"rejected":labels.reviewState(c)==="uncertain"?"uncertain":"reviewed"}" data-id="${escape(c.id)}" aria-pressed="${c.id===selected}"><span class="dot"></span><span class="row-text"><b>${escape(displayName(c))}</b>${titleZh(c)?`<span class="row-subtitle">${escape(titleZh(c))}</span>`:''}<small>${escape(c.declaration)}</small><span class="row-labels">${c._labels.filter(id=>id.startsWith('role/')||id.startsWith('priority/')).map(id=>labelChip(id)).join('')}</span>${labels.reviewState(c)!=='pending'?`<span class="row-meta">${escape(verdictNames[labels.reviewState(c)])}</span>`:''}</span></button>`
    ).join("")||'<div class="list-empty">没有符合条件的条目。试试切换范围或清空搜索。</div>';
    $("result-count").textContent=`${list.length.toLocaleString()} 个条目`;
    $("page-label").textContent=`${page+1} / ${pages}`;
    $("prev-page").disabled=page===0;$("next-page").disabled=page===pages-1;
    const range=directoryRows(),done=range.filter(c=>labels.reviewState(c)!=='pending').length;
    $("progress-label").textContent=`${done} / ${range.length.toLocaleString()}`;
    $("progress-fill").style.width=`${range.length?100*done/range.length:0}%`;
    renderDirectoryScope();renderLabelUI();
  }
  function locateList(reset=false){
    let index=rows().findIndex(c=>c.id===selected);
    if(index<0 && reset){selectedLabels.clear();query="";$("search").value="";$("scope").value="all";if(!directories.matchesPath(byId.get(selected)?.module_file,directory)){directory=(byId.get(selected)?.module_file||'').split('/').slice(0,-1).join('/');}updateLocation();index=rows().findIndex(c=>c.id===selected);}
    if(index>=0){page=Math.floor(index/PAGE_SIZE);renderList();$("card-list").querySelector('.active')?.scrollIntoView({block:"nearest"});}
  }
  async function loadCatalog(){
    const data=await json("./api/catalog");catalog=data.cards;byId=new Map(catalog.map(c=>[c.id,c]));
    for(const row of catalog)row._labels=labels.forCard(row);
    catalogInfo=data;directoryNodes=directories.buildTree(catalog);
    if(!directoryNodes.has(directory)){directory='';updateLocation();}
    $("revision").textContent=`源码 · ${data.source_commit.slice(0,9)} · ${catalog.length.toLocaleString()} 条索引`;
    $('directory-version').textContent=`源码版本 ${data.source_commit.slice(0,9)}`;
    $('directory-tree-version').textContent=`固定提交 ${data.source_commit.slice(0,9)}`;
    renderList();return data;
  }
  function matches(c,row){
    const values=c.fingerprints||{[c.fingerprint_scheme]:c.fingerprint};
    return (row.review_basis_fingerprint && values[row.review_basis_scheme]===row.review_basis_fingerprint) || values[row.fingerprint_scheme||"kip126-review-legacy.v1"]===row.fingerprint;
  }
  function math(){
    clearTimeout(mathTimer);
    if(window.MathJax?.typesetPromise){window.Stage3Latex?.typeset([$("statement")]);return;}
    if(mathLoading)return;
    mathTimer=setTimeout(()=>{mathLoading=true;const script=document.createElement("script");script.src=new URL("./mathjax-tex-svg.js",document.baseURI);script.onload=()=>Promise.resolve(window.MathJax?.startup?.promise).then(()=>window.Stage3Latex?.typeset([$("statement")]));script.onerror=()=>{mathLoading=false;};document.head.appendChild(script);},350);
  }
  function codeHtml(text){return window.Stage3Lean?.toHtml(text)||escape(text);}
  function renderEvidence(c){
    card=c;$("empty").hidden=true;$("review-card").hidden=false;
    $("breadcrumb").textContent=`${c.role} / ${c.kind.toUpperCase()}`;
    $("card-title").textContent=displayName(c);
    $('card-subtitle').textContent=titleZh(c);$('card-subtitle').hidden=!titleZh(c);
    renderProvenance(c);
    $("meta").innerHTML=`<span>${escape(c.declaration)}</span>`;
    renderCardLabels();
    $("statement-origin").textContent=c.statement_origin==="blueprint"?"Blueprint 原文":c.statement_origin==="backtranslation"?"回译草稿 · 待核验":"生成的阅读摘要";
    $("statement").innerHTML=window.Stage3Latex?.toHtml(c.statement)||escape(c.statement);math();
    const summary=c.reading_summary_zh&&c.reading_summary_zh!==c.statement?c.reading_summary_zh:'';
    $('reading-summary').hidden=!summary;$('reading-summary').textContent=summary?`阅读摘要：${summary}`:'';
    $("nl-location").textContent=c.statement_origin==="reading-summary"?"阅读摘要帮助定位；请以 Lean 陈述为依据，尚未核验为语义回译。":c.statement_origin==="backtranslation"?"Agent 回译草稿；请对照 Lean 源码核验。":`${c.blueprint_file}:${c.blueprint_line}`;
    $("lean-code").innerHTML=codeHtml(c.lean?.source||"-- 尚未定位源码");
    $("lean-location").textContent=c.lean?`${c.lean.file}:${c.lean.line} · 声明完整显示` : "源码尚未定位";
    $("structure-panel").hidden=!c.fields?.length;
    $("field-count").textContent=`${c.fields?.length||0} 个字段`;
    $("structure-fields").innerHTML=(c.fields||[]).map(f=>`<div class="field"><b>${escape(f.name)}</b><code>${codeHtml(f.type)}</code></div>`).join("");
    $("module-panel").hidden=true;$("module-panel").open=false;$("toggle-module").textContent="查看完整文件";
    $("module-code").textContent="";++moduleSequence;
    $("dependency-count").textContent=`(${c.dependencies.length})`;
    $("dependencies").innerHTML=c.dependencies.map(id=>`<button data-id="${escape(id)}">${escape(byId.get(id)?.declaration||id)}</button>`).join("")||"没有定位到本仓库中的引用对象。";
  }
  function renderReview(c,history){
    const current=history.find(row=>matches(c,row));
    const badge=$("status-badge");
    badge.className=`status-badge ${current?current.verdict==="misaligned"?"rejected":current.verdict==="uncertain"||current.verdict==="partial"?"uncertain":"reviewed":""}`;
    badge.textContent=current?verdictNames[current.verdict]:"未审阅";
    badge.title=!current&&history.length?"内容已更新，当前版本尚未审阅；旧判断保留在审阅历史。":"";
    $("rationale").value=current?.rationale||"";
    document.querySelectorAll('[name="verdict"]').forEach(input=>{input.checked=input.value===current?.verdict;input.disabled=false;});
    $("rationale").disabled=false;$("save").disabled=false;
    $("history-count").textContent=`(${history.length})`;
    $("history").innerHTML=history.map(row=>`<div class="history-item ${matches(c,row)?"":"stale"}"><b>${escape(verdictNames[row.verdict])}</b><small>${escape(identities.current?.display_name||row.reviewer)} · ${escape(new Date(row.created_at).toLocaleString("zh-CN"))}${matches(c,row)?"":" · 旧版本"}</small>${row.rationale?`<p>${escape(row.rationale)}</p>`:""}</div>`).join("")||"暂无审阅记录。";
    dirty=false;pendingRequest=null;$("save-message").textContent="";
  }
  function mayNavigate(){
    if(saving){$("save-message").textContent="正在保存，请稍候。";return false;}
    if(dirty && !window.confirm("有尚未保存的意见，是否放弃这些修改并切换？"))return false;
    return true;
  }
  async function openCard(id,force=false){
    if(!id || !byId.has(id) || (!force && id!==selected && !mayNavigate()))return;
    const turn=++sequence;selected=id;card=null;dirty=false;pendingRequest=null;
    updateLocation(id);
    renderList();if(view==="graph")renderGraph();
    $("save").disabled=true;$("rationale").disabled=true;
    document.querySelectorAll('[name="verdict"]').forEach(input=>{input.disabled=true;});
    $("review-card").hidden=true;$("empty").hidden=false;$("empty").querySelector("h2").textContent="正在打开条目…";
    try{
      const statePromise=json(`./api/history?id=${encodeURIComponent(id)}`).then(data=>({data}),error=>({error}));
      const c=await evidence(id);if(turn!==sequence)return;renderEvidence(c);
      $("save-message").textContent="正在读取个人记录…";
      const state=await statePromise;if(turn!==sequence)return;
      if(state.error)throw state.error;
      renderReview(c,state.data.history);
    }catch(error){if(turn!==sequence)return;$("save-message").textContent=error.message;$("empty").querySelector("h2").textContent="条目加载失败";$("empty").querySelector("p").textContent=error.message;}
  }
  async function save(){
    if(!card || saving)return;
    const verdict=document.querySelector('[name="verdict"]:checked')?.value;
    if(!verdict){$("save-message").textContent="请先选择一个结论。";return;}
    const savedCard=card;
    pendingRequest=pendingRequest||crypto.randomUUID();saving=true;$("save").disabled=true;$("next").disabled=true;
    $("save-message").textContent="正在保存…";
    try{
      await post("./api/judgments",{request_id:pendingRequest,card_id:savedCard.id,fingerprint:savedCard.fingerprint,verdict,rationale:$("rationale").value.trim()});
      pendingRequest=null;dirty=false;await loadCatalog();
      const state=await json(`./api/history?id=${encodeURIComponent(savedCard.id)}`);
      if(card?.id===savedCard.id)renderReview(savedCard,state.history);
      $("save-message").textContent="已保存，可继续审阅或修改判断。";$("save-global").textContent="✓ 个人记录已保存";
      if(view==="graph")renderGraph();
    }catch(error){$("save-message").textContent=`${error.message}，可重试。`;}
    finally{saving=false;$("save").disabled=false;$("next").disabled=false;}
  }
  function next(offset=1){const list=rows(),i=list.findIndex(c=>c.id===selected),target=list[i+offset];if(target)openCard(target.id).then(()=>locateList());else $("save-global").textContent="当前范围已到最后一条。";}
  function updateFilter(){const bucket=labels.reviewBucket(selectedLabels);$("filters").querySelectorAll("button").forEach(b=>{const active=b.dataset.filter===bucket;b.classList.toggle("active",active);b.setAttribute('aria-pressed',active);});}
  function setView(value){view=value;$("views").querySelectorAll("button").forEach(b=>{b.classList.toggle("active",b.dataset.view===view);b.setAttribute("aria-pressed",String(b.dataset.view===view));});$("graph-panel").hidden=view!=="graph";if(view==="graph")renderGraph({resetZoom:true});else locateList();}
  function renderGraph(options){graph.show(catalog,selected,options);}

  async function start(){
    const identity=await identities.load();
    if(!identity.display_name){identities.show("edit");return;}
    await loadCatalog();let fromHash='';try{fromHash=decodeURIComponent(location.hash.slice(1));}catch{}
    const id=byId.has(fromHash)?fromHash:rows()[0]?.id;if(id){await openCard(id,true);locateList();}else $("empty").querySelector('h2').textContent="此快照暂无条目";
  }
  function resetSession(){
    selected=null;card=null;catalog=[];byId.clear();api.clearEvidenceCache();++sequence;
    view='list';setView('list');$("review-card").hidden=true;$("card-list").innerHTML="";
    $("admin-link").hidden=true;$("graph-panel").hidden=true;$("reviewer-name").textContent="…";$("save-global").textContent="";
  }
  $("card-list").onclick=event=>{const row=event.target.closest('[data-id]');if(row)openCard(row.dataset.id);};
  $("dependencies").onclick=event=>{const row=event.target.closest('[data-id]');if(row)openCard(row.dataset.id).then(()=>locateList());};
  $('choose-directory').onclick=()=>{
    let parts=directory.split('/');while(parts.length){expandedDirectories.add(parts.join('/'));parts.pop();}
    $('directory-search').value='';renderDirectoryTree();$('directory-dialog').showModal();
  };
  $('close-directory').onclick=()=>$('directory-dialog').close();
  $('directory-search').oninput=renderDirectoryTree;
  $('all-directories').onclick=()=>chooseDirectory('');
  $('directory-tree').onclick=event=>{
    const expand=event.target.closest('[data-expand]');if(expand){const path=expand.dataset.expand;if(expandedDirectories.has(path))expandedDirectories.delete(path);else expandedDirectories.add(path);renderDirectoryTree();return;}
    const row=event.target.closest('[data-directory]');if(row)chooseDirectory(row.dataset.directory);
  };
  $('show-card-directory').onclick=()=>chooseDirectory((card?.module_file||'').split('/').slice(0,-1).join('/'));
  $('choose-labels').onclick=()=>{$('labels-dialog').showModal();renderLabelUI();};
  $('toggle-finder').onclick=()=>{const open=document.querySelector('.sidebar').classList.toggle('finder-open');$('toggle-finder').setAttribute('aria-expanded',open);$('toggle-finder').textContent=open?'收起查找':'展开查找';};
  $('label-tabs').onclick=event=>{const button=event.target.closest('[data-label-tab]');if(button){labelTab=button.dataset.labelTab;renderLabelUI();}};
  $('close-labels').onclick=$('apply-labels').onclick=()=>{$('labels-dialog').close();locateList();};
  $('reset-labels').onclick=$('dialog-reset-labels').onclick=$('clear-outside-labels').onclick=clearLabels;
  for(const id of ['active-labels','label-groups'])$(id).onclick=event=>{const button=event.target.closest('[data-label]');if(button)changeLabel(button.dataset.label);};
  $('export-scope').onclick=()=>{
    if(!catalogInfo)return;
    const manifest={schema:'statement-review-scope.v1',source_commit:catalogInfo.source_commit,snapshot_digest:catalogInfo.snapshot_digest,directory,
      selection:directoryNodes.get(directory)?.file?'exact-file':'directory-with-descendants',declarations:directoryRows().map(c=>({id:c.id,declaration:c.declaration,file:c.module_file}))};
    const url=URL.createObjectURL(new Blob([JSON.stringify(manifest,null,2)],{type:'application/json'}));
    const link=document.createElement('a');link.href=url;link.download='kip126-review-scope.json';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    $('save-global').textContent=`已导出当前目录的 ${manifest.declarations.length} 条声明及版本`;
  };
  $("filters").onclick=async event=>{const button=event.target.closest('[data-filter]');if(button&&mayNavigate()){labels.setReviewScope(selectedLabels,button.dataset.filter);await refreshSelection();}};
  $("search").oninput=event=>{query=event.target.value.trim().toLowerCase();page=0;renderList();};
  $("scope").onchange=$("sort").onchange=()=>{page=0;renderList();};
  $("prev-page").onclick=()=>{page--;renderList();$("card-list").scrollTop=0;};$("next-page").onclick=()=>{page++;renderList();$("card-list").scrollTop=0;};
  $("locate-list").onclick=()=>locateList(true);
  $("views").onclick=event=>{const button=event.target.closest('[data-view]');if(button)setView(button.dataset.view);};
  $("save").onclick=save;$("next").onclick=()=>next();
  $("rationale").oninput=$("verdicts").onchange=()=>{dirty=true;pendingRequest=null;$("save-global").textContent="意见尚未保存";};
  $("copy-code").onclick=async()=>{try{await navigator.clipboard.writeText(card?.lean?.source||'');$("copy-code").textContent='已复制';setTimeout(()=>{$("copy-code").textContent='复制代码';},1500);}catch{$("save-global").textContent='可在代码区选中并复制。';}};
  $("toggle-module").onclick=async()=>{
    if(!card)return;if(!$("module-panel").hidden){$("module-panel").hidden=true;$("toggle-module").textContent='查看完整文件';return;}
    const turn=++moduleSequence,current=card.id;$("toggle-module").textContent='正在读取…';
    try{const module=await json(`./api/module?file=${encodeURIComponent(card.module_file)}`);if(turn!==moduleSequence||current!==card?.id)return;$("module-code").innerHTML=codeHtml(module.source);$("module-panel").hidden=false;$("module-panel").open=true;$("toggle-module").textContent='收起完整文件';}
    catch(error){if(turn===moduleSequence){$("toggle-module").textContent='查看完整文件';$("save-global").textContent=error.message;}}
  };
  document.addEventListener('keydown',event=>{
    if($("name-dialog").open||$('directory-dialog').open||$('labels-dialog').open)return;
    if((event.ctrlKey||event.metaKey)&&event.key==='Enter'){event.preventDefault();save();return;}
    if(event.target.matches('input,textarea,select'))return;
    if(event.key.toLowerCase()==='j')next();if(event.key.toLowerCase()==='k')next(-1);
    if(/^[1-3]$/.test(event.key)){const input=document.querySelectorAll('[name="verdict"]')[Number(event.key)-1];if(input&&!input.disabled){input.checked=true;dirty=true;pendingRequest=null;}}
  });
  window.addEventListener('beforeunload',event=>{if(dirty||saving){event.preventDefault();event.returnValue='';}});
  (async()=>{try{if(await identities.initialize(await json('./api/config')))await start();}catch(error){$("empty").querySelector('h2').textContent='加载失败';$("empty").querySelector('p').textContent=error.message;}})();
})();
