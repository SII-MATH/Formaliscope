'use strict';
const assert=require('node:assert/strict');
require('./static/lean-renderer.js');
require('./static/statement-symbols.js');

function renderer(){
  const source='\n\ndef foo (α : Type) :=\n\n\n  A.β α -- A.comment\n  /- A.hidden /- A.nested -/ A.stillHidden -/\n  "A.string \\\" name"\n';
  const html=Stage3Lean.toHtml(source,{symbols:true,baseLine:20});
  assert.match(html,/data-symbol="foo" data-symbol-line="22" data-symbol-column="5"/);
  assert.match(html,/data-symbol="A.β" data-symbol-line="25" data-symbol-column="3"/);
  assert.match(html,/data-symbol="α"/);
  for(const value of ['A.comment','A.hidden','A.nested','A.stillHidden','A.string'])assert.ok(!html.includes(`data-symbol="${value}"`),value);
  assert.ok(!html.includes('data-symbol="def"'));
  assert.match(html,/<span class="k">def<\/span>/);
  assert.match(Stage3Lean.toHtml('def «a b» := A.β',{symbols:true,scope:'module'}),/data-symbol="«a b»"/);
  assert.ok(!Stage3Lean.toHtml('A.foo').includes('lean-symbol'),'Plain rendering stays available for field snippets');
  const astral=Stage3Lean.toHtml('𝕏 α',{symbols:true});
  assert.match(astral,/data-symbol="α" data-symbol-line="1" data-symbol-column="3"/);
  assert.ok(!Stage3Lean.toHtml("'a' '\\n'",{symbols:true}).includes('lean-symbol'));
  assert.ok(!Stage3Lean.toHtml('"unterminated A.nope',{symbols:true}).includes('lean-symbol'));
  assert.ok(!Stage3Lean.toHtml('/- unterminated A.nope',{symbols:true}).includes('lean-symbol'));
}
function element(){return {hidden:false,innerHTML:'',listeners:{},addEventListener(event,fn){this.listeners[event]=fn;},querySelector(){return null;},closest(){return null;},contains(){return true;}};}
async function controller(){
  const panel=element(),code=element();let current={id:'current'},target=null,requests=[],resolve,opened=0;
  const highlight={classList:{add(name){target=name;},remove(){}},scrollIntoView(){},focus(){}};
  code.querySelector=selector=>selector.includes('"9"')?highlight:null;
  const service=StatementSymbols.create({panel,codeElements:[code],getCard:()=>current,
    request:path=>{requests.push(path);return new Promise(done=>resolve=done);},
    onNavigate:async id=>{current={id};},onLocate:async()=>{opened++;}});
  const token={dataset:{symbol:'A.foo',symbolLine:'5',symbolColumn:'3',symbolScope:'declaration'}};
  let pending=service.resolveToken(token);resolve({status:'local',name:'x',message:'本地绑定',target:{card_id:'current',line:9,column:1}});await pending;
  assert.equal(target,'symbol-highlight');assert.equal(opened,0);
  assert.ok(requests[0].includes('name=A.foo'));
  pending=service.resolveToken(token);resolve({status:'ambiguous',name:'same',message:'请选择',candidates:[{card_id:'first',declaration:'A.same',file:'A.lean',line:2}]});await pending;
  assert.equal(current.id,'current');assert.ok(panel.innerHTML.includes('data-symbol-choice="0"'));
  pending=service.resolveToken(token);resolve({status:'unresolved',name:'Nat.add',message:'Mathlib',candidates:[]});await pending;
  assert.ok(panel.innerHTML.includes('Mathlib'));assert.equal(current.id,'current');
  pending=service.resolveToken(token);service.clear();resolve({status:'unresolved',name:'stale',message:'stale',candidates:[]});await pending;
  assert.equal(panel.hidden,true,'Results from the previous card never reopen the panel');
  pending=service.resolveToken(token);resolve({status:'definition',name:'A.foo',target:{card_id:'next',line:9,column:1},candidates:[]});await pending;
  assert.equal(current.id,'next');
  pending=service.resolveToken(token);resolve({status:'local',name:'n',message:'上下文',target:{card_id:'next',file:'A.lean',line:1,column:1,scope:'module'}});await pending;
  assert.equal(opened,1);assert.ok(panel.innerHTML.includes('A.lean:1'));
}
(async()=>{renderer();await controller();console.log('Lean definition tracing renderer and navigation tests passed.');})().catch(error=>{console.error(error);process.exitCode=1;});
