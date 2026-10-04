"use strict";
(() => {
  // Each browser entry owns its filters and reading position, not review data.
  function create({history: browserHistory, capture, restore, canLeave, onChange=()=>{},
    token=()=>crypto.randomUUID()}={}) {
    const previous=browserHistory.state?.statementNavigation;
    const resumable=typeof previous?.session==='string'&&previous.session.length>0&&
      Number.isSafeInteger(previous.index)&&previous.index>=0;
    let session=resumable?previous.session:token(), index=resumable?previous.index:0;
    let initialized=false, restoring=false, bounce=null, operation=0;
    const entries=new Map();
    const clone=value=>JSON.parse(JSON.stringify(value));
    // A reload keeps the browser's existing entries. Personal review records
    // are loaded separately; only this entry's reading snapshot is retained.
    if(resumable&&previous.snapshot)entries.set(index,clone(previous.snapshot));
    function state(snapshot){return {statementNavigation:{session,index,snapshot:clone(snapshot)}};}
    function notify(){onChange({canBack:initialized&&index>0,restoring});}
    function remember(url){
      if(!initialized)return;
      const snapshot=clone(capture());entries.set(index,snapshot);
      browserHistory.replaceState(state(snapshot),'',url);notify();
    }
    function replace(url){
      initialized=true;remember(url);
    }
    function push(url){
      if(!initialized){replace(url);return;}
      for(const key of entries.keys())if(key>index)entries.delete(key);
      ++index;const snapshot=clone(capture());entries.set(index,snapshot);
      browserHistory.pushState(state(snapshot),'',url);notify();
    }
    async function pop(raw){
      const target=raw?.statementNavigation;
      if(!initialized||target?.session!==session||!Number.isInteger(target.index))return false;
      if(bounce===target.index){bounce=null;restoring=false;notify();return false;}
      const previous=index;
      // Capture the visible entry before waiting for a save. A later pop may
      // arrive while the target is loading; that placeholder is not a reading
      // position and must never replace the target's existing snapshot.
      if(!restoring)entries.set(previous,clone(capture()));
      const startedSession=session,turn=++operation;
      restoring=true;notify();
      // The browser already moved. A failed save sends it back without losing edits.
      let allowed=false;try{allowed=await canLeave();}catch{}
      if(startedSession!==session||turn!==operation)return false;
      if(!allowed){
        if(previous===target.index){restoring=false;bounce=null;notify();return false;}
        bounce=previous;browserHistory.go(previous-target.index);return false;
      }
      index=target.index;
      try{await restore(clone(entries.get(index)||target.snapshot));}
      finally{if(startedSession===session&&turn===operation){restoring=false;notify();}}
      return true;
    }
    function back(){if(initialized&&index>0&&!restoring)browserHistory.back();}
    function reset(){++operation;session=token();index=0;initialized=false;restoring=false;bounce=null;entries.clear();notify();}
    return Object.freeze({remember,replace,push,pop,back,reset,get restoring(){return restoring;}});
  }
  (typeof window!=="undefined"?window:globalThis).StatementNavigation=Object.freeze({create});
})();
