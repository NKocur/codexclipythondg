// A logic smoke test of the live adapter; DOM rendering still needs browser QA.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, {
    value:'', innerHTML:'', textContent:'', checked:true, style:{}, open:false,listeners:{},
    classList:{add(){},remove(){},toggle(){},contains(){return false;}},
    prepend(){}, appendChild(){},after(){},insertAdjacentHTML(){},setAttribute(){},focus(){},showModal(){this.open=true;}, close(){this.open=false;this.listeners.close?.();},
    addEventListener(name,fn){this.listeners[name]=fn;}
  });
  return elements.get(id);
}
el('goal').value = 'Inspect this workspace';
let serverRun = null, rev = 0, folders = [], now = Date.now(), authBlocked = false;
class TestDate extends Date { static now(){return now;} }
const requests = [], timers = [], storage = new Map();
const previews = [];
let resultButtons = [];
const context = vm.createContext({
  console, Date:TestDate, setTimeout(fn){timers.push(fn);},
  localStorage:{getItem(k){return storage.get(k)||null;},setItem(k,v){storage.set(k,v);}},
  document:{createElement:el,querySelector:el,querySelectorAll:()=>resultButtons,body:el('body')},
  window:{WorkbenchPreview:{open(options){previews.push(options);}}},
  fetch:async (path, options) => {
    requests.push({path,options});
    if (path === '/api/bootstrap') return {ok:true,json:async()=>({token:'token',cwd:'C:/project',codex_available:true})};
    if (path === '/api/history') return {ok:true,json:async()=>({runs:[{id:'research-run',goal:'Prior literature review',cwd:'C:/research',status:'completed',started:1,count:1,team:['Researcher']}]})};
    if (path === '/api/history?id=research-run') return {ok:true,json:async()=>({run:{id:'research-run',goal:'Prior literature review',cwd:'C:/research',sandbox:'workspace-write',status:'completed',team:true,agent:{id:'researcher',name:'Researcher',provider:'Codex',status:'done'},agents:[{id:'researcher',name:'Researcher',provider:'Codex',status:'done'}],current:0,started:1,finished:61,max_turns:20,turns:[],trail:['-1>0'],events:[{time:'12:00',text:'Earlier research finished',agent:'Researcher'}],artifacts:[{name:'REPORT.html',path:'artifacts/REPORT.html',body:'Saved report'}],output:'Research summary'}})};
    if (path === '/api/auth/status') return {ok:true,json:async()=>({providers:[{provider:'Codex',state:'ready',message:'Signed in',command:'codex.cmd login'}]})};
    if (path === '/api/auth/login') return {ok:true,json:async()=>({provider:'Codex',state:'signing_in',message:'Complete browser login',command:'codex.cmd login'})};
    if (path.startsWith('/api/folders?')) return {ok:true,json:async()=>({path:'C:/project',parent:'C:/',folders})};
    if (path === '/api/run' && options?.method === 'POST') {
      if (authBlocked) return {ok:false,json:async()=>({error:'Sign in required',auth:{provider:'Codex',state:'sign_in_required',message:'Sign in required',command:'codex.cmd login'}})};
      const data = JSON.parse(options.body);
      assert.equal((data.agent || data.agents[0]).provider, 'Codex');
      assert.equal(options.headers['X-Workbench-Token'], 'token');
      serverRun = {id:'run'+(rev+1),...data,team:!!data.agents,agent:data.agent||data.agents[0],current:0,turns:[],artifacts:[],status:'running',events:[],output:'',started:Date.now()/1000};rev++;
    }
    if (path === '/api/control') {
      const data=JSON.parse(options.body);
      if(data.action==='pause') serverRun.pause_requested=true;
      else {serverRun.pause_requested=false;serverRun.status='running';serverRun.current=serverRun.next_index;serverRun.agent=serverRun.agents[serverRun.current];serverRun.auto_advance=data.auto_advance;}
      if(data.action==='extend'){serverRun.max_turns+=data.additional_turns;serverRun.pause_reason=null;}
      rev++;
    }
    if (path === '/api/stop') {serverRun.status='stopped';rev++;}
    return {ok:true,json:async()=>({revision:rev,run:serverRun})};
  }, $:el
});
vm.runInContext(`
let agents=[{id:'codex',name:'Reviewer',provider:'Codex',prompt:'Review',status:'queued'}];
let running=false,paused=false,waiting=false,outcome=null,staleRun=false,current=0;
let events=[],artifacts=[],history=[],lastSeconds=0,settingsBefore=null,trail=new Set();
const COLORS=['blue'];
const esc=s=>String(s);
const icon=name=>'<svg>'+name+'</svg>';
function render(){renderResults();} function renderResults(){} function persist(){} function renderHistory(){}
function stateOf(a){return ['',a.status];} function teamChanged(){}
function notify(message){globalThis.lastNotice=message;}
function updateWorkspace(){} function renderRecent(){} function primaryAction(){}
function showView(view){globalThis.currentView=view;}
function newBlankTeam(){agents=[];events=[];artifacts=[];outcome=null;return true;}
$('saveSettings').onclick=()=>{};
`, context);
vm.runInContext(fs.readFileSync('web/live.js','utf8'), context);
const run=s=>vm.runInContext(s,context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  await settle();
  el('liveMode').value='single';
  el('liveAgent').value='codex';
  run('render()');
  assert.equal(el('runButton').disabled,false);
  assert.equal(el('sandbox').value,'read-only');
  await run('primaryAction()');
  assert.equal(run('running'),true);
  assert.equal(el('runButton').disabled,true);
  assert.equal(run('artifacts.length'),0);
  serverRun.events=[{time:'12:00',level:'info',text:'Real update'}];
  serverRun.output='Real response';rev++;
  await timers.shift()();
  assert.equal(run('events[0].body'),'Real update');
  assert.equal(run('artifacts[0].body'),'Real response');
  el('browseButton').onclick(); await settle();
  assert.equal(el('useFolder').disabled,false);
  folders = [{name:'New folder',path:'C:/project/New folder',git:false}];
  now += 2000;
  await timers.shift()();
  assert(el('folderList').innerHTML.includes('New folder'));
  folders = [{name:'Renamed folder',path:'C:/project/Renamed folder',git:false}];
  await el('liveFolderRefresh').onclick();
  assert(el('folderList').innerHTML.includes('Renamed folder'));
  assert(!el('folderList').innerHTML.includes('New folder'));
  el('useFolder').onclick();
  assert.equal(el('folderDialog').open,false);
  el('workspacePath').value='C:/next-project';rev++;
  await timers.shift()();
  assert.equal(el('workspacePath').value,'C:/next-project');
  await el('stopButton').onclick();
  assert.equal(run('running'),false);
  assert.equal(el('runHeadline').textContent,'Reviewer · stopped');
  assert.equal(requests.filter(r=>r.options?.method==='POST'&&!r.path.startsWith('/api/auth/')).length,2);
  serverRun.status='failed';serverRun.auth_provider='Codex';serverRun.error='401 Unauthorized';rev++;
  await timers.shift()();
  assert.equal(el('dialog').open,true);
  assert(el('authProviders').innerHTML.includes('credentials were missing or expired'));
  assert.equal(requests.filter(r=>r.path==='/api/auth/login').length,0);
  await el('dialog').listeners.click({target:{closest(){return {disabled:false,dataset:{authAction:'login',authProvider:'Codex'}}}}});
  assert.equal(requests.filter(r=>r.path==='/api/auth/login').length,1);
  assert(el('authProviders').innerHTML.includes('signing in'));
  await el('authRecheck').onclick();
  assert(el('authProviders').innerHTML.includes('Signed in'));
  el('dialog').close();authBlocked=true;
  await run('primaryAction()');
  assert.equal(el('dialog').open,true);
  assert.equal(run('running'),false);
  assert.equal(run('lastNotice'),'Sign in required');
  authBlocked=false;el('dialog').close();
  run("agents=[{id:'planner',name:'Planner',provider:'Claude',model:'claude-model',status:'queued'},{id:'tester',name:'Tester',provider:'Codex',status:'queued'}]");
  el('liveMode').value='team';el('turnLimit').value='20';run('render()');
  assert.equal(el('runButton').disabled,true);
  el('convertCodex').onclick();
  assert.equal(run('agents[0].provider'),'Codex');assert.equal(run('agents[0].model'),'');
  el('sandbox').value='workspace-write';
  await run('primaryAction()');
  assert.equal(serverRun.team,true);assert.equal(serverRun.agents.length,2);
  assert.equal(el('runButton').innerHTML,'Pause after this agent');
  await run('primaryAction()');assert.equal(serverRun.pause_requested,true);
  serverRun.status='paused';serverRun.next_index=1;serverRun.agents[0].status='done';
  serverRun.artifacts=[{name:'PLAN.md',agent:'Planner',body:'Actual plan file',version:1,time:'12:00'}];rev++;
  await timers.shift()();
  assert.equal(run('waiting'),true);assert.equal(el('runButton').innerHTML,'Continue');assert.equal(el('runButton').disabled,false);
  assert.equal(run('artifacts[0].body'),'Actual plan file');
  serverRun.pause_reason='turn_limit';serverRun.turn_budget=20;rev++;
  await timers.shift()();
  assert.equal(el('runButton').innerHTML,'Allow 20 more turns');
  assert(el('runHelp').textContent.includes('No agent is running'));
  serverRun.artifacts[0].path='artifacts/PLAN.md';rev++;
  const openButton=el('openResult');openButton.dataset={artifact:'0'};resultButtons=[openButton];
  await timers.shift()();
  let stoppedClick=false;openButton.onclick({stopPropagation(){stoppedClick=true;}});
  assert.equal(stoppedClick,true);assert.equal(previews.at(-1).name,'PLAN.md');
  assert.equal(previews.at(-1).token,'token');
  assert(previews.at(-1).url.includes('/api/preview?run='));
  resultButtons=[];
  el('autoAdvance').checked=false;await run('primaryAction()');
  const extension=JSON.parse(requests.filter(r=>r.path==='/api/control').at(-1).options.body);
  assert.equal(extension.action,'extend');assert.equal(extension.additional_turns,20);
  assert.equal(serverRun.max_turns,40);
  assert.equal(serverRun.agent.name,'Tester');assert.equal(serverRun.auto_advance,false);
  await el('stopButton').onclick();assert.equal(run('running'),false);
  assert(el('historyList').innerHTML.includes('Prior literature review'));
  await el('historyList').listeners.click({target:{closest(){return {dataset:{savedView:'research-run'}}}}});
  assert.equal(run('currentView'),'home');
  assert.equal(el('goal').value,'Prior literature review');
  assert.equal(run('agents[0].name'),'Researcher');
  assert.equal(run('artifacts[0].name'),'REPORT.html');
  assert.equal(run('events[0].body'),'Earlier research finished');
  assert.equal(el('runHeadline').textContent,'Researcher · completed');
  serverRun.events.push({text:'Newer live update'});rev++;
  await timers.shift()();
  assert.equal(run('agents[0].name'),'Researcher');
  assert.equal(run('events[0].body'),'Earlier research finished');
  resultButtons=[openButton];run('render()');openButton.onclick({stopPropagation(){}});resultButtons=[];
  assert.equal(previews.at(-1).name,'REPORT.html');
  assert(previews.at(-1).url.includes('run=research-run'));
  el('returnToLive').onclick();
  assert.equal(run('agents[0].name'),'Planner');
  await el('historyList').listeners.click({target:{closest(){return {dataset:{savedContinue:'research-run'}}}}});
  assert.equal(el('workspacePath').value,'C:/research');
  assert.equal(el('goal').value,'');
  assert.equal(run('agents[0].name'),'Planner');
  assert.equal(run('currentView'),'home');
  el('goal').value='Implement code from the review';
  await run('primaryAction()');
  assert.equal(serverRun.source_run_id,'research-run');
  assert.equal(serverRun.goal,'Implement code from the review');
  assert.equal(serverRun.agents[0].name,'Planner');
  await el('stopButton').onclick();
  el('sourceRun').value='';el('sourceRun').onchange();
  el('goal').value='Unrelated work';await run('primaryAction()');
  assert.equal(serverRun.source_run_id,null);
  await el('historyList').listeners.click({target:{closest(){return {dataset:{savedView:'research-run'}}}}});
  assert.equal(el('runButton').disabled,true);
  assert.equal(el('returnToLive').textContent,'Back to active run');
  const postCount=requests.filter(r=>r.options?.method==='POST').length;
  await run('primaryAction()');
  assert.equal(requests.filter(r=>r.options?.method==='POST').length,postCount);
  el('returnToLive').onclick();
  assert.equal(run('running'),true);
  assert.equal(el('goal').value,'Unrelated work');
  await el('stopButton').onclick();
  await el('historyList').listeners.click({target:{closest(){return {dataset:{savedView:'research-run'}}}}});
  await run('primaryAction()');
  assert.equal(el('goal').value,'');
  assert.equal(el('goal').disabled,false);
  await timers.shift()();
  assert.equal(el('goal').value,'');
  run('newBlankTeam()');
  assert.equal(run('agents.length'),0);
  assert.equal(el('liveMode').value,'team');
  assert.equal(el('runButton').disabled,true);
  const blankPosts=requests.filter(r=>r.options?.method==='POST').length;
  await run('primaryAction()');
  assert.equal(requests.filter(r=>r.options?.method==='POST').length,blankPosts);
  assert.equal(run('lastNotice'),'Add at least one teammate before running.');
  rev++;await timers.shift()();
  assert.equal(run('agents.length'),0);
  // A refreshed page must not launch the new protocol against the old live server.
  run("agents=[{id:'plan-coordinator',name:'Coordinator',provider:'Codex',workflow:'markdown-plan-v2'}];render()");
  assert.equal(el('runButton').disabled,true);
  const beforePlan=requests.filter(r=>r.options?.method==='POST').length;
  await run('primaryAction()');
  assert.equal(requests.filter(r=>r.options?.method==='POST').length,beforePlan);
  assert.match(run('lastNotice'),/restart Workbench/);
  run("delete agents[0].workflow");
  el('goal').value='Implement the plan';await run('primaryAction()');
  serverRun.plan={outcome:'in_progress',current:'B1',steps:[{id:'S1',title:'Feature',status:'awaiting_tests'},{id:'S2',title:'Native check',status:'blocked'}],
    batches:[{id:'B1',title:'Feature batch',revision:2,status:'awaiting_tests',blockers:[{id:'vm',need:'Provide native image',resume:'Supply the image path'}]}],needs_input:[]};
  rev++;await timers.shift()();
  assert.match(el('planProgressBody').innerHTML,/0 of 2 steps verified/);
  assert.match(el('planProgressBody').innerHTML,/1 awaiting validation/);
  assert.match(el('planProgressBody').innerHTML,/Provide native image/);
  serverRun.status='paused';serverRun.pause_reason='needs_input';serverRun.next_index=0;
  serverRun.plan.notice='Waiting for the image';rev++;await timers.shift()();
  assert.equal(el('runHeadline').textContent,'Your input is needed');
  el('planResumeNote').value='Image is ready at images/native.img';
  await run('primaryAction()');
  const resume=JSON.parse(requests.filter(r=>r.path==='/api/control').at(-1).options.body);
  assert.equal(resume.note,'Image is ready at images/native.img');
  assert.equal(el('planResumeNote').value,'');
  serverRun.status='completed';serverRun.plan.outcome='blocked';rev++;await timers.shift()();
  assert.match(el('runHeadline').textContent,/Run finished · Plan blocked/);
  assert.doesNotMatch(el('runHeadline').textContent,/Plan complete/);
  console.log('Live adapter: execution, reports, folders, auth, saved history and follow-up selection passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
