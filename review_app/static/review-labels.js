(function(root){
  'use strict';
  const definitions=new Map();
  const groupSpecs=[
    {id:'role',name:'数学角色',source:'Agent 候选分类',items:[['definition','对象与定义','blue'],['input','前提与输入','violet'],['comparison','比较与转换','fuchsia'],['computation','计算与核验','cyan'],['derivation','中间推导','slate'],['target','目标结论','indigo'],['infrastructure','工具与封装','gray']]},
    {id:'topic',name:'数学主题',source:'项目配置 · Agent 候选主题',items:[]},
    {id:'priority',name:'审阅优先度',source:'审阅顺序 · 未分级不视为低优先度',items:[['p0','P0 · 优先审核','orange'],['p1','P1 · 重点审核','amber'],['p2','P2 · 常规审核','slate']]},
    {id:'status',name:'我的审阅状态',source:'本人记录 · 当前版本无有效判断均归为未审阅',items:[['pending','未审阅','gray'],['aligned','通过','green'],['uncertain','没看懂','amber'],['misaligned','不通过','red']]},
    {id:'readback',name:'回译状态',source:'当前快照的实际回译记录',items:[['none','未回译','gray'],['draft','回译草稿','blue'],['verified','回译已核验','cyan']]}
  ];
  function makeGroup(spec){
    const items=spec.items.map(([key,name,color])=>{
      const item=Object.freeze({id:spec.id+'/'+key,group:spec.id,name,color,source:spec.source});
      definitions.set(item.id,item);return item;
    });
    return Object.freeze({...spec,items:Object.freeze(items)});
  }
  let groups=Object.freeze(groupSpecs.map(makeGroup));
  const roles=new Set(groupSpecs[0].items.map(item=>item[0]));
  const roleAliases=new Map([['model','definition'],['literature','input'],['computed','computation'],['transport','comparison'],['challenge','definition']]);
  const reviewedStates=['aligned','uncertain','misaligned'];
  const legacyAliases=new Map([
    ...[...roleAliases].map(([from,to])=>['role/'+from,'role/'+to]),
    ['status/stale','status/pending'],['topic/certificate','role/computation'],
    ['topic/spectral','topic/spectral_sequence'],['topic/comparison','role/comparison'],
    ['readback/context','readback/draft']
  ]);
  function configureTopics(topics){
    if(!Array.isArray(topics))throw new TypeError('数学主题配置必须是数组');
    const ids=new Set(),items=topics.map(topic=>{
      if(!topic||typeof topic.id!=='string'||!/^[a-z][a-z0-9_]*$/.test(topic.id)||typeof topic.name!=='string'||!topic.name.trim()||ids.has(topic.id))throw new TypeError('数学主题配置包含无效或重复的选项');
      ids.add(topic.id);return [topic.id,topic.name,'teal'];
    });
    for(const [id,item] of definitions)if(item.group==='topic')definitions.delete(id);
    groups=Object.freeze(groups.map(group=>group.id==='topic'?makeGroup({...group,items}):group));
  }
  function normalizeSelection(ids){return new Set([...ids].map(id=>definitions.has(id)?id:legacyAliases.get(id)||id).filter(id=>definitions.has(id)));}
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
    const file=card.module_file||card.lean?.file||'',name=card.declaration||'',text=(file+' '+name).toLowerCase();
    const analysis=card.enrichment,classification=analysis?.classification,isV2=analysis?.schema==='statement-enrichment.v2';
    let role=null;
    if(classification){
      role=isV2?classification.role:roleAliases.get(classification.role)||classification.role;
    }else if(!isV2){
      if(card.main_target||card.role==='主定理')role='target';
      else if(/\/Literature\//.test(file))role='input';
      else if(/\/LinProgram\/|\/Interpretation\/|\/Certificate\//.test(file))role='computation';
      else if(/Comparison|Transport/.test(file))role='comparison';
      else if(['theorem','lemma'].includes(card.kind))role='derivation';
      else if(card.kind==='axiom')role='input';
      else if(['def','abbrev','structure','class','instance','opaque'].includes(card.kind))role='definition';
    }
    const tags=[];
    if(roles.has(role))tags.push('role/'+role);
    let priority=null;
    if(analysis&&(Object.hasOwn(analysis,'priority')||isV2))priority=typeof analysis.priority==='string'?analysis.priority:analysis.priority?.level;
    else priority=card.main_target||card.role==='主定理'?'p0':card.priority>=80?'p1':'p2';
    if(['p0','p1','p2'].includes(priority))tags.push('priority/'+priority);
    tags.push('status/'+reviewState(card),'readback/'+(['none','draft','verified'].includes(analysis?.readback?.status)?analysis.readback.status:card.statement_origin==='backtranslation'?'draft':'none'));
    if(classification){
      for(const topic of classification.topics||[]){
        const id='topic/'+(!isV2&&topic==='spectral'?'spectral_sequence':topic);
        if(definitions.get(id)?.group==='topic'&&(isV2||topic!=='comparison'))tags.push(id);
      }
    }else if(!isV2){
      if(/spectralsequence|spectral_sequence/.test(text))tags.push('topic/spectral_sequence');
      if(/adams/.test(text))tags.push('topic/adams');
      if(/sphere/.test(text))tags.push('topic/sphere');
    }
    return [...new Set(tags)].filter(id=>definitions.has(id));
  }
  function matches(tags,selected,skipGroup){
    const choices=new Map();
    for(const id of selected){const item=definitions.get(id);if(!item||item.group===skipGroup)continue;if(!choices.has(item.group))choices.set(item.group,[]);choices.get(item.group).push(id);}
    return [...choices.values()].every(ids=>ids.some(id=>tags.includes(id)));
  }
  root.ReviewLabels=Object.freeze({get groups(){return groups;},definitions,configureTopics,forCard,matches,normalizeSelection,reviewState,reviewBucket,setReviewScope});
})(typeof window==='undefined'?globalThis:window);
