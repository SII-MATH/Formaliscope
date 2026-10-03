"use strict";
(() => {
  // Keep public evidence separate from personal history: only evidence is cached.
  function create({fetch: fetcher=globalThis.fetch.bind(globalThis), onUnauthorized=()=>{}, evidenceLimit=12}={}) {
    const cache=new Map(), inflight=new Map();
    let generation=0;

    async function request(url, {optionalUnauthorized=false, ...options}={}) {
      const response=await fetcher(url, {cache:"no-store", ...options});
      if(response.status===401 && optionalUnauthorized)return null;
      const data=await response.json();
      if(!response.ok){
        if(response.status===401)onUnauthorized();
        throw new Error(data.error || `请求失败：${response.status}`);
      }
      return data;
    }

    function post(url, body) {
      return request(url, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)});
    }

    function evidence(id) {
      if(cache.has(id))return Promise.resolve(cache.get(id));
      if(inflight.has(id))return inflight.get(id);
      const started=generation;
      const pending=request(`./api/evidence?id=${encodeURIComponent(id)}`).then(card=>{
        if(started===generation){
          cache.set(id,card);
          while(cache.size>evidenceLimit)cache.delete(cache.keys().next().value);
        }
        return card;
      }).finally(()=>{if(inflight.get(id)===pending)inflight.delete(id);});
      inflight.set(id,pending);
      return pending;
    }

    function clearEvidenceCache() {
      ++generation;
      cache.clear();inflight.clear();
    }

    return Object.freeze({request,post,evidence,clearEvidenceCache});
  }
  (typeof window!=="undefined"?window:globalThis).StatementAPI=Object.freeze({create});
})();
