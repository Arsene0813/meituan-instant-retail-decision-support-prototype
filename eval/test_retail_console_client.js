// Optional Node test: controller behavior with a small in-memory element harness.
// No browser, runtime files, backend data, or UI framework dependency is needed.
"use strict";
const fs=require("node:fs"), path=require("node:path"), vm=require("node:vm"), assert=require("node:assert/strict");
const root=path.resolve(__dirname,"..");
class Element{
  constructor(tag){this.tag=tag;this.children=[];this.listeners={};this.dataset={};this.value="";this.checked=false;this.disabled=false;this.hidden=false;this.files=[];this._text="";this.classList={toggle(){}};}
  set textContent(value){this._text=String(value);this.children=[];}
  get textContent(){return this._text+this.children.map(x=>x.textContent||"").join("");}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;this._text="";}
  get options(){return this.children;}
  get selectedOptions(){return this.children.filter(x=>x.selected);}
  setAttribute(key,value){this[key]=value;}
  addEventListener(type,f){(this.listeners[type]??=[]).push(f);}
  async fire(type){for(const f of this.listeners[type]||[])await f({target:this});}
  querySelectorAll(tag){return descendants(this).filter(x=>x.tag===tag);}
  scrollIntoView(){}
}
function descendants(element){return element.children.flatMap(x=>[x,...descendants(x)]);}
function harness(){
  const elements={};const html=fs.readFileSync(path.join(root,"api/retail_console/index.html"),"utf8");
  for(const match of html.matchAll(/<([a-z]+)[^>]*\bid="([^"]+)"/g))elements[match[2]]=new Element(match[1]);
  const document={getElementById:id=>{assert.ok(elements[id],id);return elements[id];},
    querySelector:()=>({content:"synthetic-token"}),querySelectorAll:()=>[],
    createElement:tag=>new Element(tag),createTextNode:text=>{const n=new Element("text");n.textContent=text;return n;}};
  const context=vm.createContext({document,Node:Element,Option:class extends Element{constructor(text,value){super("option");this.textContent=text;this.value=value;}},
    fetch:async()=>{throw Error("unexpected network request");},Uint8Array,btoa:text=>Buffer.from(text,"binary").toString("base64")});
  const code=fs.readFileSync(path.join(root,"api/retail_console/console.js"),"utf8");
  assert.ok(code.endsWith("refreshCatalog();refreshBatches();refreshPublications();\n"));
  vm.runInContext(code.replace(/refreshCatalog\(\);refreshBatches\(\);refreshPublications\(\);\s*$/,""),context);
  return {context,elements,run:code=>vm.runInContext(code,context)};
}
function response(data){return {ok:true,text:async()=>JSON.stringify({number_encoding:"decimal_text",data})};}
function batch(id){return {batch_id:id,upload_id:id,status:"validated",received_at:"2026-05-01",extracted_at:"2026-05-01",context:{store_id:"B",period_start:"2026-03-01",period_end:"2026-03-01",dataset_id:"store_period_panel_metrics"}};}
async function main(){
  {
    const h=harness();h.context.fetch=async()=>response({items:[batch("batch_first")],next_offset:"50"});
    await h.run("refreshBatches()");const checkbox=descendants(h.elements["batch-list"]).find(n=>n.tag==="input");checkbox.checked=true;await checkbox.fire("change");
    assert.equal(h.run("state.selected.size"),1);
    h.context.fetch=async()=>response({items:[batch("batch_second")],next_offset:null});h.run("state.batchOffset=50");await h.run("refreshBatches()");
    const second=descendants(h.elements["batch-list"]).find(n=>n.tag==="input");second.checked=true;await second.fire("change");
    assert.equal(h.run("state.selected.size"),2);assert.match(h.elements["selected-batches"].textContent,/batch_first/);assert.match(h.elements["selected-batches"].textContent,/batch_second/);
    h.context.fetch=async()=>response({items:[batch("batch_first")],next_offset:"50"});h.run("state.batchOffset=0");await h.run("refreshBatches()");
    assert.equal(descendants(h.elements["batch-list"]).find(n=>n.tag==="input").checked,true);
    await h.elements["clear-selection"].fire("click");assert.equal(h.run("state.selected.size"),0);
  }
  {
    const h=harness();let finish;h.context.fetch=()=>new Promise(resolve=>finish=resolve);
    h.elements.publication.value="publication_explicit";h.elements.dataset.value="store_period_panel_metrics";h.elements.stores.value="B";h.elements["window-mode"].value="month";h.elements.month.value="2026-03";
    const pending=h.run("runQuery(false)");h.elements.month.value="2026-04";await h.elements.month.fire("input");
    finish(response({publication_id:"old",stores:[]}));await pending;
    assert.equal(h.elements["query-result"].children.length,0);
    assert.equal(h.elements["query-status"].textContent,"");
  }
  {
    const h=harness();h.context.fetch=async()=>response({batch_id:"batch_held",status:"quarantined",errors:["scope conflict"],preview:{errors:[],validated_records:[{source_line_end:"2",record:{store_id:"B",period_start:"2026-03-01",period_end:"2026-03-01",transaction_orders:"9007199254740993"}}]}});
    await h.run('loadDetail("batch_held")');const text=h.elements["batch-detail"].textContent;
    assert.match(text,/解析候选记录（批次待核对）/);assert.doesNotMatch(text,/核对通过的记录/);assert.match(text,/9007199254740993/);
  }
  {
    const h=harness();h.run("state.receiving=true;intakeEnabled()");
    for(const id of ["selection","source-file","revision","refresh-registry"])assert.equal(h.elements[id].disabled,true);
    const bytes=Buffer.from("\ufeff店铺：B\r\n成交金额0\r\n");h.context.sample=new Uint8Array(bytes).buffer;
    assert.equal(h.run("encodedBytes(sample)"),bytes.toString("base64"));
  }
  {
    const h=harness();const source="<img src=x onerror=alert(1)>";h.context.source=source;
    assert.equal(h.run('el("p",source).textContent'),source);
    assert.equal(h.run("exact(null)"),"未提供");assert.equal(h.run('exact("0")'),"0");
  }
  console.log("Console client: 5 interaction/state checks passed.");
}
main().catch(error=>{console.error(error);process.exitCode=1;});
