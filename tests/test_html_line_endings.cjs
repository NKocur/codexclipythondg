// Exercise the actual role/persistence assertions under both checkout formats.
const {spawnSync}=require('node:child_process');
const assert=require('node:assert/strict');
for(const ending of ['\n','\r\n']) {
  const script=`
    const fs=require('node:fs'), original=fs.readFileSync;
    fs.readFileSync=function(path,...args) {
      const result=original.call(this,path,...args);
      return String(path).replaceAll('\\\\','/').endsWith('mockup/index-v2.html') && typeof result==='string'
        ? result.replace(/\\r\\n/g,'\\n').replace(/\\n/g,${JSON.stringify(ending)}) : result;
    };
    require('./tests/test_role_deliverables.cjs');
  `;
  const result=spawnSync(process.execPath,['-e',script],{encoding:'utf8'});
  assert.equal(result.status,0,`${JSON.stringify(ending)} checkout failed:\n${result.stdout}\n${result.stderr}`);
}
console.log('Role and blank-team persistence checks pass with LF and CRLF HTML.');
