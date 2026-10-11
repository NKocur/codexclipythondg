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
