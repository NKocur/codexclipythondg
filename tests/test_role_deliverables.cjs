const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('mockup/index-v2.html','utf8');
const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
new vm.Script(script); // Check syntax of the complete standalone UI.
const fields = new Map();
function field(id) {
  if (!fields.has(id)) fields.set(id, {value:'',focus(){},close(){},elements:{
    route:{value:'end'},provider:{value:'Codex'},glyph:{value:'bot'},color:{value:'0'}
  }});
  return fields.get(id);
}
const context = vm.createContext({$:field, notify:message=>context.notice=message});
vm.runInContext(`
const PROVIDERS=['Codex'], GLYPHS=['bot'], COLORS=['blue'];
let agents=[],editing=-1;
function teamChanged(){} function cleanRoutes(){} function persist(){} function render(){}
${script.slice(script.indexOf('function normalize('),script.indexOf('// Drop links'))}
${script.slice(script.indexOf('const cloneRoles='),script.indexOf('const currentPreset='))}
${script.slice(script.indexOf("$('agentForm').onsubmit="),script.indexOf("$('removeAgent').onclick="))}
`,context);
field('agentName').value='Reporter';
field('agentPrompt').value='Summarize findings';
field('agentEffort').value='Default';
field('agentHtml').value='REPORT.html';
field('agentPdf').value='REPORT.pdf';
const submit=()=>field('agentForm').onsubmit({preventDefault(){}});
submit();
assert.equal(vm.runInContext('agents[0].deliverables.join(",")',context),'REPORT.html,REPORT.pdf');
assert.equal(vm.runInContext('normalize(JSON.parse(JSON.stringify(agents[0])),0).deliverables.length',context),2);
assert.equal(vm.runInContext('normalize({name:"Old role"},0).deliverables.length',context),0);
assert.equal(vm.runInContext('snap(agents)===snap(cloneRoles(agents))',context),true);
vm.runInContext('editing=0',context);
field('agentPdf').value='../REPORT.pdf';
submit();
assert.match(context.notice,/simple filename/);
field('agentHtml').value=''; field('agentPdf').value='';
submit();
assert.equal(vm.runInContext('agents[0].deliverables.length',context),0);
console.log('Role deliverables: save, persistence normalization, cloning, validation and removal passed.');
vm.runInContext(`
let presets=[{id:'base',name:'Original team',roles:cloneRoles(agents)}],presetId='base';
let running=false,events=[],artifacts=[],outcome=null,staleRun=false,current=0,trail=new Set();
let allowReset=false,savedDraft=null;
function isEdited(){return true;} function confirm(){return allowReset;}
function toggleMenu(){} function hideTip(){} function showView(){}
function currentPreset(){return presets.find(p=>p.id===presetId);}
function persist(){savedDraft=JSON.stringify({presetId,agents,presets});}
${script.slice(script.indexOf('function newBlankTeam('),script.indexOf('function showTip('))}
`,context);
assert.equal(vm.runInContext('newBlankTeam()',context),false);
assert.equal(vm.runInContext('agents.length',context),1);
vm.runInContext('allowReset=true;newBlankTeam()',context);
assert.equal(vm.runInContext('agents.length',context),0);
assert.equal(vm.runInContext('presetId',context),null);
assert.equal(vm.runInContext('presets[0].roles.length',context),1);
vm.runInContext(`
const KEY='test',FIELDS=[];
const BUILTIN_PRESETS=[{id:'new-builtin',name:'New built-in',roles:[]}];
const validRole=a=>a&&typeof a.name==='string'&&typeof a.prompt==='string'&&typeof a.artifact==='string';
const localStorage={getItem(){return savedDraft;}};
agents=cloneRoles(presets[0].roles);presetId='base';
${script.slice(script.indexOf('try{\n const s=JSON.parse(localStorage.getItem(KEY));'),script.indexOf('\ncleanRoutes();'))}
`,context);
assert.equal(vm.runInContext('agents.length',context),0);
assert.equal(vm.runInContext('presetId',context),null);
assert.equal(vm.runInContext('presets.some(p=>p.id==="new-builtin")',context),true);
console.log('Blank team: creation, cancelled reset, preset preservation and empty draft reload passed.');
