"use strict";
(() => {
  // A bounded neighborhood keeps navigation usable for declarations with many users.
  function neighborhood(catalog, selected, depth, limit=18) {
    const byId=new Map(catalog.map(card=>[card.id,card])),seen=new Set([selected]);
    const columns=new Map([[0,[selected]]]);let parents=[selected],children=[selected],omitted=0;
    for(let level=1;level<=depth;level++){
      const left=[...new Set(parents.flatMap(id=>byId.get(id)?.dependencies||[]))].filter(id=>byId.has(id)&&!seen.has(id));
      const right=catalog.filter(card=>card.dependencies?.some(id=>children.includes(id))).map(card=>card.id).filter(id=>!seen.has(id)&&!left.includes(id));
      omitted+=Math.max(0,left.length-limit)+Math.max(0,right.length-limit);
      parents=left.slice(0,limit);children=right.slice(0,limit);
      columns.set(-level,parents);columns.set(level,children);parents.concat(children).forEach(id=>seen.add(id));
    }
    const height=Math.max(310,...[...columns.values()].map(ids=>ids.length*60+50)),width=depth*600+400,nodes=new Map();
    for(const [level,ids] of columns)ids.forEach((id,index)=>nodes.set(id,{x:width/2+level*300,y:(height-(ids.length-1)*60)/2+index*60}));
    return {byId,nodes,width,height,omitted};
  }

  function create({elements:$, escape, reviewState, verdictNames, onSelect, isHighPriority=card=>card.priority>=80}) {
    let catalog=[],selected=null,graphZoom=1,graphBox={x:0,y:0,w:1100,h:310},drag=null;
    function applyBox(){$('graph').setAttribute('viewBox',`${graphBox.x} ${graphBox.y} ${graphBox.w} ${graphBox.h}`);}
    function render() {
      if(!selected)return;
      const graph=neighborhood(catalog,selected,Number($('graph-depth').value)),{byId,nodes,width:w,height:h,omitted}=graph;
      const center=byId.get(selected);if(!center)return;
      let edges='';
      for(const [id,p] of nodes)for(const dep of byId.get(id).dependencies||[]){
        const q=nodes.get(dep);if(!q)continue;
        edges+=`<path class="graph-edge" d="M${q.x+108},${q.y} C${(q.x+p.x)/2},${q.y} ${(q.x+p.x)/2},${p.y} ${p.x-108},${p.y}" marker-end="url(#arrow)"/>`;
      }
      $('graph').innerHTML=`<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#b8caaa"/></marker></defs>${edges}`+[...nodes].map(([id,p])=>{
        const card=byId.get(id),name=card.declaration.split('.').slice(-2).join('.');
        return `<g class="graph-node ${id===selected?'selected':''} ${isHighPriority(card)?'high':''}" data-id="${escape(id)}" role="button" tabindex="0" aria-label="${escape(card.declaration)}" transform="translate(${p.x-108},${p.y-23})"><title>${escape(card.declaration)}</title><rect width="216" height="46" rx="7"/><text x="12" y="19">${escape(name.length>27?name.slice(0,25)+'…':name)}</text><text class="node-role" x="12" y="35">${escape(card.role)}${reviewState(card)!=='pending'?' · '+escape(verdictNames[reviewState(card)]):''}</text></g>`;
      }).join('');
      graphBox={x:w/2-550/graphZoom,y:h/2-155/graphZoom,w:1100/graphZoom,h:310/graphZoom};applyBox();
      $('graph-caption').textContent=`已定位 ${center.declaration} · ${nodes.size} 个节点${omitted?` · 另有 ${omitted} 个邻接节点，可从条目的引用列表继续浏览`:''} · 拖动平移，＋／− 缩放`;
    }
    function show(nextCatalog,nextSelected,{resetZoom=false}={}) {
      catalog=nextCatalog;selected=nextSelected;if(resetZoom)graphZoom=1;render();
    }
    function zoom(factor) {
      const cx=graphBox.x+graphBox.w/2,cy=graphBox.y+graphBox.h/2;
      graphZoom=Math.min(3,Math.max(.2,graphZoom*factor));graphBox.w=1100/graphZoom;graphBox.h=310/graphZoom;
      graphBox.x=cx-graphBox.w/2;graphBox.y=cy-graphBox.h/2;applyBox();
    }
    $('graph').addEventListener('pointerdown',event=>{if(event.target.closest('[data-id]'))return;drag={x:event.clientX,y:event.clientY,bx:graphBox.x,by:graphBox.y};event.currentTarget.setPointerCapture(event.pointerId);});
    $('graph').addEventListener('pointermove',event=>{if(!drag)return;const rect=event.currentTarget.getBoundingClientRect(),scale=Math.max(graphBox.w/rect.width,graphBox.h/rect.height);graphBox.x=drag.bx-(event.clientX-drag.x)*scale;graphBox.y=drag.by-(event.clientY-drag.y)*scale;applyBox();});
    $('graph').addEventListener('pointerup',()=>{drag=null;});$('graph').addEventListener('pointercancel',()=>{drag=null;});
    $('graph').addEventListener('click',event=>{const node=event.target.closest('[data-id]');if(node)onSelect(node.dataset.id);});
    $('graph').addEventListener('keydown',event=>{const node=event.target.closest('[data-id]');if(node&&['Enter',' '].includes(event.key)){event.preventDefault();onSelect(node.dataset.id);}});
    $('zoom-in').onclick=()=>zoom(1.3);$('zoom-out').onclick=()=>zoom(1/1.3);
    $('center-graph').onclick=()=>{graphZoom=1;render();};$('graph-depth').onchange=render;
    return Object.freeze({show});
  }
  (typeof window!=="undefined"?window:globalThis).StatementGraph=Object.freeze({create,neighborhood});
})();
