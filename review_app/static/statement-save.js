"use strict";
(() => {
  const same=(a,b)=>a.verdict===b.verdict && a.rationale.trim()===b.rationale.trim();
  const empty=()=>({verdict:"",rationale:""});
  const verdicts=["aligned","uncertain","misaligned"];

  // Autosaves replace a recoverable draft. A completion boundary drains that
  // draft first, then appends one judgment. Only its receipt updates progress.
  function create({post,onState=()=>{},onSaved=()=>{},debounceMs=650,
    requestId=()=>globalThis.crypto.randomUUID(),
    setTimer=globalThis.setTimeout.bind(globalThis),clearTimer=globalThis.clearTimeout.bind(globalThis)}={}) {
    if(typeof post!=="function")throw new TypeError("post is required");
    let card=null,draft=empty(),persisted=empty(),completed=empty(),revision=0,generation=0,timer=null;
    let active=null,failed=null,ready=false,flushing=false,wantsComplete=false,allowIncomplete=false,error=null,saved=false;

    function state() {
      const dirty=!!failed || !same(draft,persisted),unfinished=!same(draft,completed);
      return Object.freeze({saving:!!active,dirty,error,unfinished,completing:wantsComplete||active?.kind==='complete',
        canComplete:verdicts.includes(draft.verdict)&&(unfinished||!!failed),
        status:active?"saving":error?(failed?"error":"invalid"):dirty?"pending":
          !unfinished&&completed.verdict?"completed":saved?"saved":"idle",
        draft:Object.freeze({...draft}),cardId:card?.id||null});
    }
    function emit(){onState(state());}
    function clearDebounce(){if(timer!==null){clearTimer(timer);timer=null;}}
    function load(nextCard,current=null,savedDraft=null,draftRevision=savedDraft?.revision||0) {
      clearDebounce();++generation;active=null;failed=null;ready=false;flushing=false;wantsComplete=false;error=null;
      card=nextCard?Object.freeze({id:nextCard.id,fingerprint:nextCard.fingerprint}):null;
      completed={verdict:current?.verdict||"",rationale:current?.rationale||""};
      draft=savedDraft?{verdict:savedDraft.verdict||"",rationale:savedDraft.rationale||""}:{...completed};
      persisted={...draft};revision=draftRevision;saved=!!savedDraft;emit();
    }
    function valid(kind) {
      if(kind==='complete'&&!verdicts.includes(draft.verdict))return "请先选择一个结论，再完成审阅。";
      if(draft.rationale.length>4000)return "备注最多 4000 字，请缩短后重试。";
      return null;
    }
    function receipt(data,payload,kind) {
      const row=kind==='draft'?data?.draft:data?.judgment;
      if(!row?.id || row.card_id!==payload.card_id || row.fingerprint!==payload.fingerprint ||
        row.verdict!==payload.verdict || row.rationale!==payload.rationale ||
        (kind==='draft'&&row.revision!==payload.revision+1))
        throw new Error("未收到完整的保存确认，请重试。");
      return row;
    }
    async function drain(operation,retryFailed) {
      while(operation.generation===generation && card) {
        if(failed && !retryFailed)return false;
        if(failed){operation.kind=failed.kind;operation.payload=failed.payload;}
        else{
          const needsDraft=ready&&!same(draft,persisted);
          if(!needsDraft){
            ready=false;
            if(!wantsComplete || same(draft,completed))return true;
            if(allowIncomplete&&!verdicts.includes(draft.verdict))return true;
          }
          operation.kind=needsDraft?'draft':'complete';
          error=valid(operation.kind);if(error){emit();return false;}
          operation.payload=Object.freeze({request_id:requestId(),card_id:card.id,fingerprint:card.fingerprint,
            verdict:draft.verdict,rationale:draft.rationale.trim(),
            ...(operation.kind==='draft'?{revision}:{draft_revision:revision})});
        }
        ready=false;error=null;emit();
        let row;
        try{row=receipt(await post(operation.kind==='draft'?"./api/drafts":"./api/judgments",operation.payload),operation.payload,operation.kind);}
        catch(reason){
          if(operation.generation!==generation)return false;
          failed={kind:operation.kind,payload:operation.payload};error=reason?.message||"保存失败，请重试。";emit();return false;
        }
        if(operation.generation!==generation)return false;
        failed=null;saved=true;
        if(operation.kind==='draft'){
          persisted={verdict:operation.payload.verdict,rationale:operation.payload.rationale};revision=row.revision;
        }else{
          completed={verdict:operation.payload.verdict,rationale:operation.payload.rationale};
          wantsComplete=flushing&&!same(draft,completed);onSaved(row,operation.payload);
        }
        emit();
        if(flushing&&!same(draft,persisted))ready=true;
      }
      return false;
    }
    function launch(retryFailed=false) {
      if(active)return active.promise;
      if(!card)return Promise.resolve(true);
      const operation={generation,payload:null,kind:null,promise:null};active=operation;
      operation.promise=Promise.resolve().then(()=>drain(operation,retryFailed)).finally(()=>{
        if(active===operation){active=null;flushing=false;wantsComplete=false;emit();}
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
    function finish(incomplete) {
      clearDebounce();ready=true;flushing=true;wantsComplete=true;allowIncomplete=incomplete;return launch(true);
    }
    return Object.freeze({load,edit,flush:()=>finish(true),complete:()=>finish(false),retry:()=>finish(true),
      reset:()=>load(null),get state(){return state();}});
  }
  (typeof window!=="undefined"?window:globalThis).StatementSave=Object.freeze({create});
})();
