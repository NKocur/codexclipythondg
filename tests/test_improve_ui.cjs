const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const nodes=new Map(),timers=[],requests=[];
const el=id=>{
  if(!nodes.has(id))nodes.set(id,{value:'',textContent:'',innerHTML:'',disabled:false,checked:false,
    scrollTop:0,scrollHeight:100,classList:{toggle(){}}});
  return nodes.get(id);
};
let chat=null,active=true,applyError=false;
const context=vm.createContext({document:{getElementById:el},setTimeout(fn){timers.push(fn);},
  fetch:async(path,options={})=>{
    requests.push({path,options});
    let body;
    if(path==='/api/bootstrap')body={token:'test-token',improvement_chat:true};
    else {
      assert.equal(options.headers['X-Workbench-Token'],'test-token');
      if(path==='/api/history')body={runs:[{id:'run1',goal:'Build <feature>',status:active?'running':'completed'}]};
      else if(path==='/api/improve' && options.method==='POST'){
        const data=JSON.parse(options.body);
        chat={status:'running',run_id:data.run_id,run_goal:'Build feature',messages:[{role:'user',text:data.message}],activity:[],proposal:null};
        body={chat};
      } else if(path==='/api/improve/apply'){
        if(applyError)return {ok:false,json:async()=>({error:'Source changed. Prepare again.'})};
        chat.proposal.applied=true;chat.proposal.can_apply=false;body={chat};
      } else if(path==='/api/improve/revert') {
        chat.proposal.applied=false;chat.proposal.reverted=true;body={chat};
      } else if(path==='/api/improve/stop') {chat.status='stopped';body={chat};}
      else body={chat,source_root:'C:/Workbench'};
    }
    return {ok:true,json:async()=>JSON.parse(JSON.stringify(body))};
  },console});
el('improveMode').value='review';
vm.runInContext(fs.readFileSync('web/improve.js','utf8'),context);
const settle=()=>new Promise(r=>setImmediate(r));
(async()=>{
  await settle();
  assert.equal(el('improveRun').value,'run1');
  assert.equal(el('improveSource').value,'C:/Workbench');
  assert.equal(el('improveSend').disabled,false);
  el('improveMessage').value='Why did this loop? <script>evil()</script>';
  await el('improveForm').onsubmit({preventDefault(){}});
  assert.equal(el('improveSend').disabled,true);
  assert.match(el('improveMessages').innerHTML,/&lt;script&gt;/);
  assert.doesNotMatch(el('improveMessages').innerHTML,/<script>/);
  assert.equal(el('improveMessage').value,'');
  await el('improveStop').onclick();
  assert.equal(requests.some(r=>r.path==='/api/stop'),false);
  assert.equal(el('improveSend').disabled,false);
  el('improveMode').value='prepare';el('improveMode').onchange();
  el('improveMessage').value='Fix the loop';
  await el('improveForm').onsubmit({preventDefault(){}});
  assert.equal(JSON.parse(requests.filter(r=>r.path==='/api/improve'&&r.options.method==='POST').at(-1).options.body).mode,'prepare');
  chat.status='ready';
  chat.messages.push({role:'assistant',text:'Prepared a patch'});
  chat.proposal={id:'proposal1',summary:'Improved routing',diff:'+ <script>not executable</script>',tests:[{command:'node test.js',result:'passed',exit_code:0,evidence:'Passed'}],limitations:[],can_apply:true,applied:false};
  await timers.shift()();
  assert.match(el('improveTests').innerHTML,/Tests reported by the assistant/);
  assert.equal(el('improveDiff').textContent,chat.proposal.diff);
  el('improveApprove').checked=true;el('improveApprove').onchange();
  assert.equal(el('improveApply').disabled,true); // active main run
  active=false;await timers.shift()();
  assert.equal(el('improveApply').disabled,false);
  applyError=true;await el('improveApply').onclick();
  assert.equal(el('improveStatus').textContent,'Source changed. Prepare again.');
  applyError=false;await el('improveApply').onclick();
  assert.equal(el('improveApply').disabled,true);
  assert.match(el('improveApplyHint').textContent,/Applied to source/);
  await el('improveRevert').onclick();
  assert.match(el('improveApplyHint').textContent,/Reverted in source/);
  assert.equal(requests.some(r=>r.path==='/api/run'&&r.options.method==='POST'),false);
  console.log('Improvement tab: independent chat, escaped content, mode selection, apply approval, active-run gate and errors passed.');
})().catch(e=>{console.error(e);process.exitCode=1;});
