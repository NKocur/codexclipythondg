const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = [];
function element(tag) {
  const node = {tag,style:{},attributes:{},children:[],selectors:{},listeners:{},
    setAttribute(k,v){this.attributes[k]=v;},
    appendChild(n){this.children.push(n);},
    querySelector(s){return this.selectors[s] ||= element(s);},
    addEventListener(k,fn){this.listeners[k]=fn;},
    showModal(){this.open=true;},close(){this.open=false;this.listeners.close();},
    remove(){this.removed=true;}
  };
  elements.push(node);return node;
}
let pending, policy, removed = 0;
const revoked = [], requests = [];
const context = vm.createContext({window:{},Blob,
  document:{createElement:element,body:element('body')},
  URL:{createObjectURL(blob){assert.equal(blob.type,'application/pdf');return 'blob:pdf';},revokeObjectURL(url){revoked.push(url);}},
  DOMParser:class {parseFromString(text){return {
    querySelectorAll(){return [{remove(){removed++;}}];},createElement:element,
    head:{prepend(p){policy=p;}},documentElement:{outerHTML:text}
  };}},
  fetch(url,options){requests.push({url,options});return new Promise(resolve=>{pending=resolve;});}
});
vm.runInContext(fs.readFileSync('web/preview.js','utf8'),context);
const viewer = context.window.WorkbenchPreview;
const open=name=>viewer.open({name,url:'/preview/'+name,token:'local-token',onDownload:async()=>{}});
const latestFrame=()=>elements.filter(e=>e.tag==='iframe').at(-1);
(async()=>{
  let task=open('REPORT.md');
  pending({ok:true,json:async()=>({html:'<html><h1>Heading</h1></html>'})});await task;
  assert.equal(requests[0].options.headers['X-Workbench-Token'],'local-token');
  assert.equal(latestFrame().attributes.sandbox,'');
  assert.match(latestFrame().srcdoc,/<h1>Heading/);
  assert.match(policy.content,/default-src 'none'/);
  assert.equal(removed,1);
  task=open('REPORT.pdf');pending({ok:true,blob:async()=>new Blob(['%PDF-1.4'])});await task;
  assert.equal(latestFrame().src,'blob:pdf');
  const pdfFrame=latestFrame();
  task=open('REPORT.html');pending({ok:true,json:async()=>({html:'<html>Styled report</html>'})});await task;
  assert.equal(pdfFrame.removed,true);assert.deepEqual(revoked,['blob:pdf']);
  task=open('slow.html');const stale=pending;
  const next=open('new.html');pending({ok:true,json:async()=>({html:'new report'})});await next;
  stale({ok:true,json:async()=>({html:'stale report'})});await task;
  assert.match(latestFrame().srcdoc,/new report/);
  task=open('missing.md');pending({ok:false,json:async()=>({error:'File missing'})});await task;
  const dialog=elements.find(e=>e.tag==='dialog');
  assert.match(dialog.querySelector('[data-preview-message]').textContent,/File missing/);
  dialog.close();assert.equal(dialog.open,false);
  console.log('Document viewer: isolated HTML/Markdown, PDF URLs, cleanup, stale requests and errors passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
