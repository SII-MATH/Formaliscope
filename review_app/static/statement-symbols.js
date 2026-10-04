(function(root){
  "use strict";
  const escape=value=>String(value??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;').replaceAll("'",'&#39;');

  function create({panel,codeElements,getCard,request,onNavigate,onLocate}){
    let sequence=0,choices=[],highlighted=null;
    const sources=Array.from(codeElements||[]);
    function clear(){sequence++;choices=[];if(panel){panel.hidden=true;panel.innerHTML='';}if(highlighted){highlighted.classList.remove('symbol-highlight');highlighted=null;}}
    function message(title,text,candidates=[]){
      if(!panel)return;
      panel.hidden=false;choices=candidates;
      panel.innerHTML=`<div class="symbol-heading"><strong>${escape(title)}</strong><button type="button" data-symbol-close aria-label="关闭定义追溯">×</button></div><p role="status">${escape(text)}</p>${candidates.length?`<div class="symbol-candidates">${candidates.map((item,index)=>`<button type="button" data-symbol-choice="${index}"><b>${escape(item.declaration)}</b><small>${escape(item.file)}:${item.line}</small></button>`).join('')}</div>`:''}`;
    }
    function locate(target){
      if(highlighted)highlighted.classList.remove('symbol-highlight');
      const order=target.scope==='module'?[...sources].reverse():sources;
      for(const source of order){
        if(source.closest('[hidden]'))continue;
        const token=source.querySelector(`[data-symbol-line="${Number(target.line)}"][data-symbol-column="${Number(target.column)}"]`);
        if(token){highlighted=token;token.classList.add('symbol-highlight');token.scrollIntoView({block:'nearest',inline:'nearest'});token.focus({preventScroll:true});return true;}
      }
      return false;
    }
    async function navigate(target){
      const old=getCard();
      if(old?.id!==target.card_id)await onNavigate(target.card_id,{source:'symbol',target});
      if(getCard()?.id===target.card_id)locate(target);
    }
    async function resolveToken(token){
      const card=getCard();if(!card||!token?.dataset.symbol)return;
      const turn=++sequence;
      message(token.dataset.symbol,'正在定位定义…');
      const query=new URLSearchParams({card_id:card.id,name:token.dataset.symbol,
        line:token.dataset.symbolLine,column:token.dataset.symbolColumn,
        scope:token.dataset.symbolScope||'declaration'});
      try{
        const result=await request(`./api/symbol?${query}`);
        if(turn!==sequence||getCard()?.id!==card.id)return;
        if(result.status==='definition')await navigate(result.target);
        else if(result.status==='local'&&!locate(result.target)){
          if(onLocate)await onLocate(result.target);
          if(turn===sequence&&getCard()?.id===card.id)locate(result.target);
          result.message+=` ${result.target.file}:${result.target.line}`;
        }
        if(getCard()?.id===card.id&&turn===sequence)message(result.name,result.message,result.candidates);
      }catch(error){if(turn===sequence&&getCard()?.id===card.id)message(token.dataset.symbol,error.message||'定义追溯暂时不可用，请重试。');}
    }
    for(const source of sources)source.addEventListener('click',event=>{
      const token=event.target.closest('[data-symbol]');if(token&&source.contains(token))resolveToken(token);
    });
    if(panel)panel.addEventListener('click',event=>{
      if(event.target.closest('[data-symbol-close]')){clear();return;}
      const button=event.target.closest('[data-symbol-choice]');if(!button)return;
      const target=choices[Number(button.dataset.symbolChoice)];if(!target)return;
      navigate(target).catch(error=>message(target.declaration,error.message));
    });
    return Object.freeze({clear,resolveToken,locate});
  }
  root.StatementSymbols=Object.freeze({create});
})(typeof window==='undefined'?globalThis:window);
