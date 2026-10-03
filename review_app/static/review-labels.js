(function(root){
  'use strict';
  const groups=[
    {id:'role',name:'数学角色',source:'目录与索引规则 · 候选分类',items:[['model','模型定义 · M','blue'],['literature','文献输入 · A(M)','violet'],['computed','计算输入 · C(M)','cyan'],['target','最终目标 · T(M)','indigo'],['transport','比较与运输','fuchsia'],['derivation','中间推导','slate'],['challenge','Challenge 接口','amber'],['infrastructure','基础设施','gray'],['unclassified','待分类','gray']]},
    {id:'topic',name:'数学主题',source:'名称与路径规则 · 候选主题',items:[['spectral','谱序列','teal'],['adams','Adams','teal'],['sphere','球面','teal'],['comparison','经典／合成同伦','teal']]},
    {id:'priority',name:'审阅优先度',source:'现有目标与目录规则 · 演示分级',items:[['p0','P0 · 优先审核','orange'],['p1','P1 · 重点审核','amber'],['p2','P2 · 常规审核','slate']]},
    {id:'status',name:'我的审阅状态',source:'本人记录 · 当前版本无有效判断均归为未审阅',items:[['pending','未审阅','gray'],['aligned','通过','green'],['uncertain','没看懂','amber'],['misaligned','不通过','red']]},
    {id:'readback',name:'回译状态',source:'当前快照的实际回译记录',items:[['none','未回译','gray'],['draft','回译草稿','blue'],['verified','回译已核验','cyan']]}
  ];
  const definitions=new Map();
  for(const group of groups){group.items=group.items.map(([key,name,color])=>{const item=Object.freeze({id:group.id+'/'+key,group:group.id,name,color,source:group.source});definitions.set(item.id,item);return item;});Object.freeze(group.items);Object.freeze(group);}
  const reviewedStates=['aligned','uncertain','misaligned'];
  const legacyAliases=new Map([['status/stale','status/pending'],['topic/certificate','role/computed'],['readback/context','readback/draft']]);
  function normalizeSelection(ids){return new Set([...ids].map(id=>legacyAliases.get(id)||id).filter(id=>definitions.has(id)));}
  function reviewState(card){return card.stale?'pending':card.verdict==='partial'?'uncertain':reviewedStates.includes(card.verdict)?card.verdict:'pending';}
  function reviewBucket(selected){
    const statuses=[...selected].filter(id=>definitions.get(id)?.group==='status');
    return !statuses.length||statuses.length===4?'all':statuses.every(id=>id==='status/pending')?'pending':statuses.every(id=>id!=='status/pending')?'reviewed':null;
  }
  function setReviewScope(selected,bucket){
    for(const id of selected)if(definitions.get(id)?.group==='status')selected.delete(id);
    for(const state of bucket==='pending'?['pending']:bucket==='reviewed'?reviewedStates:[])selected.add('status/'+state);
  }
  function forCard(card){
    const file=card.module_file||card.lean?.file||'', name=card.declaration||'', text=(file+' '+name).toLowerCase();
    let role='unclassified';
    if(card.main_target||card.role==='主定理')role='target';
    else if(file.startsWith('KIPBase/'))role='infrastructure';
    else if(/\/Literature\//.test(file))role='literature';
    else if(/\/LinProgram\/|\/Interpretation\/|\/Certificate\//.test(file))role='computed';
    else if(/Challenge/.test(file))role='challenge';
    else if(/Comparison|Transport/.test(file))role='transport';
    else if(/\.(sphereAdamsData|sphereAdamsModel|standardH6Square|NonzeroSurvival)$/.test(name))role='model';
    else if(['theorem','lemma'].includes(card.kind))role='derivation';
    const classification=card.enrichment?.classification;
    if(classification)role=classification.role;
    const priority=card.enrichment?.priority;
    const tags=['role/'+role,...(priority?(priority.level?['priority/'+priority.level]:[]):['priority/'+(card.main_target||card.role==='主定理'?'p0':card.priority>=80?'p1':'p2')]),
      'status/'+reviewState(card),
      'readback/'+(card.statement_origin==='backtranslation'?'draft':'none')];
    if(classification)tags.push(...classification.topics.map(topic=>'topic/'+topic));
    else {
      if(/spectralsequence|spectral_sequence/.test(text))tags.push('topic/spectral');
      if(/adams/.test(text))tags.push('topic/adams');
      if(/sphere/.test(text))tags.push('topic/sphere');
      if(/comparison|classicalsynthetic|syntheticclassical/.test(text))tags.push('topic/comparison');
    }
    return tags;
  }
  function matches(tags,selected,skipGroup){
    const choices=new Map();
    for(const id of selected){const item=definitions.get(id);if(!item||item.group===skipGroup)continue;if(!choices.has(item.group))choices.set(item.group,[]);choices.get(item.group).push(id);}
    return [...choices.values()].every(ids=>ids.some(id=>tags.includes(id)));
  }
  root.ReviewLabels=Object.freeze({groups:Object.freeze(groups),definitions,forCard,matches,normalizeSelection,reviewState,reviewBucket,setReviewScope});
})(typeof window==='undefined'?globalThis:window);
