"use strict";
(() => {
  const same=(a,b)=>a.verdict===b.verdict && a.rationale.trim()===b.rationale.trim();
  const empty=()=>({verdict:"",rationale:""});

  // One controller owns one identity's current form. Load only after flush()
  // succeeds; reset() invalidates callbacks when an identity is discarded.
  // Verdict edits use {immediate:true}; note edits use the default debounce.
  // It never rewrites inputs after a response: update history and badges from
  // onSaved instead, without loading the catalog or form again.
  function create({post,onState=()=>{},onSaved=()=>{},debounceMs=650,
    requestId=()=>globalThis.crypto.randomUUID(),
    setTimer=globalThis.setTimeout.bind(globalThis),clearTimer=globalThis.clearTimeout.bind(globalThis)}={}) {
    if(typeof post!=="function")throw new TypeError("post is required");
    let card=null,draft=empty(),persisted=empty(),generation=0,timer=null;
    let active=null,failed=null,ready=false,flushing=false,error=null,saved=false;

    function state() {
      const dirty=!!failed || !same(draft,persisted);
      return Object.freeze({saving:!!active,dirty,error,
        status:active?"saving":error?(failed?"error":"invalid"):dirty?"pending":saved?"saved":"idle",
        draft:Object.freeze({...draft}),cardId:card?.id||null});
    }
    function emit(){onState(state());}
    function clearDebounce(){if(timer!==null){clearTimer(timer);timer=null;}}
    function load(nextCard,current=null) {
      clearDebounce();++generation;active=null;failed=null;ready=false;flushing=false;error=null;saved=false;
      card=nextCard?Object.freeze({id:nextCard.id,fingerprint:nextCard.fingerprint}):null;
      draft={verdict:current?.verdict||"",rationale:current?.rationale||""};persisted={...draft};emit();
    }
    function valid() {
      if(!["aligned","uncertain","misaligned"].includes(draft.verdict))return "请先选择一个结论，备注才能保存。";
      if(draft.rationale.length>4000)return "备注最多 4000 字，请缩短后重试。";
      return null;
    }
    function receipt(data,payload) {
      const row=data?.judgment;
      if(!row?.id || row.card_id!==payload.card_id || row.fingerprint!==payload.fingerprint ||
        row.verdict!==payload.verdict || row.rationale!==payload.rationale)
        throw new Error("未收到完整的保存确认，请重试。");
      return row;
    }
    async function drain(operation,retryFailed) {
      while(operation.generation===generation && card) {
        if(failed && !retryFailed)return false;
        if(!failed && (!ready || same(draft,persisted))){ready=false;return true;}
        if(!failed){
          error=valid();if(error){emit();return false;}
          operation.payload=Object.freeze({request_id:requestId(),card_id:card.id,fingerprint:card.fingerprint,
            verdict:draft.verdict,rationale:draft.rationale.trim()});
        }else operation.payload=failed;
        ready=false;error=null;emit();
        let row;
        try{row=receipt(await post("./api/judgments",operation.payload),operation.payload);}
        catch(reason){
          if(operation.generation!==generation)return false;
          failed=operation.payload;error=reason?.message||"保存失败，请重试。";emit();return false;
        }
        if(operation.generation!==generation)return false;
        failed=null;persisted={verdict:operation.payload.verdict,rationale:operation.payload.rationale};saved=true;
        // A newer edit stays in draft; an older response only acknowledges the
        // exact payload it sent. The queued edit is then saved in order.
        onSaved(row,operation.payload);emit();
        if(flushing && !same(draft,persisted))ready=true;
      }
      return false;
    }
    function launch(retryFailed=false) {
      if(active)return active.promise;
      if(!card)return Promise.resolve(true);
      const operation={generation,payload:null,promise:null};active=operation;
      // A microtask combines synchronous changes before the first request.
      operation.promise=Promise.resolve().then(()=>drain(operation,retryFailed)).finally(()=>{
        if(active===operation){active=null;flushing=false;emit();}
      });emit();return operation.promise;
    }
    function edit(patch,{immediate=false}={}) {
      if(!card)return;
      const next={...draft,...patch};
      if(next.verdict===draft.verdict && next.rationale===draft.rationale)return;
      draft={verdict:String(next.verdict||""),rationale:String(next.rationale||"")};
      clearDebounce();if(!failed)error=null;
      if(!failed){
        if(immediate || flushing){ready=true;launch();}
        else{ready=false;timer=setTimer(()=>{timer=null;ready=true;launch();},debounceMs);}
      }
      emit();
    }
    function flush() {
      clearDebounce();ready=true;flushing=true;return launch(true);
    }
    return Object.freeze({load,edit,flush,retry:flush,reset:()=>load(null),get state(){return state();}});
  }
  (typeof window!=="undefined"?window:globalThis).StatementSave=Object.freeze({create});
})();
