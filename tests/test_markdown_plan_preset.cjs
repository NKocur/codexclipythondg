const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const html=fs.readFileSync('mockup/index-v2.html','utf8');
const script=[...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m=>m[1]).join('\n');
const context=vm.createContext({});
vm.runInContext(`const GLYPHS=['map','shield','code','check','book','search','bug','chat','spark','bot'];
const PROVIDERS=['Codex','Claude','Ollama'];const COLORS=['blue'];
${script.slice(script.indexOf('function normalize('),script.indexOf('// Drop links'))}
${script.slice(script.indexOf('const R='),script.indexOf('const FIELDS='))}
globalThis.presets=BUILTIN_PRESETS;`,context);
context.preset=context.presets.find(p=>p.id===(process.argv.includes('--v2')?'markdown-plan-v2':'markdown-plan'));
const preset=JSON.parse(JSON.stringify(context.preset));
assert.equal(preset.roles.length,5);
assert.equal(preset.maxTurns,100);
assert(preset.roles.every(r=>r.provider==='Codex'));
assert.equal(preset.roles[4].route,'end');
if(process.argv.includes('--v2')) {
 assert.equal(preset.roles[0].workflow,'markdown-plan-v2');
 assert(preset.roles[0].allowed.includes('batch-tester'));
 assert(preset.roles[0].allowed.includes('batch-reviewer'));
}
if(process.argv.includes('--json'))process.stdout.write(JSON.stringify(preset));
else console.log('Markdown plan preset loaded with five Codex roles and a 100-turn budget.');
