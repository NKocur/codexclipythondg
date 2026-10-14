/* Served only by web_workbench.py; index-v2.html remains a standalone demo. */
(() => {
  'use strict';
  let token = '', latest = null, revision = -1, busy = false, connected = false;
  let planWorkflow = null;
  let savedRuns = [], sourceRunId = '', historyChecked = 0;
  let viewingSaved = false, liveSnapshot = null, historyRequest = 0;
  const liveActive = () => ['running','paused','stopping'].includes(liveSnapshot?.run?.status);
  const baseNewBlankTeam = newBlankTeam;
  newBlankTeam = function (view='home') {
    if (busy || liveActive()) return notify('Finish or stop the active run before creating a new team.',false);
    if (!baseNewBlankTeam(view)) return;
    viewingSaved = false; latest = null; revision = liveSnapshot?.revision ?? revision;
    $('liveMode').value = 'team';
    render();
  };
  function previewResult(runId, name) {
    return window.WorkbenchPreview.open({name, token,
      url:'/api/preview?run='+encodeURIComponent(runId)+'&name='+encodeURIComponent(name),
      onDownload:()=>downloadResult(runId,name)});
  }
  async function downloadResult(runId, name) {
    const response = await fetch('/api/artifact?run='+encodeURIComponent(runId)+'&name='+encodeURIComponent(name), {headers:{'X-Workbench-Token':token}});
    if (!response.ok) throw new Error((await response.json()).error);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement('a'); link.href = url; link.download = name;
    document.body.appendChild(link); link.click(); link.remove();
    setTimeout(()=>URL.revokeObjectURL(url), 1000);
  }
  const baseRender = render;
  const baseRenderResults = renderResults;
  renderResults = function () {
    baseRenderResults();
    document.querySelectorAll('#results [data-artifact]').forEach(button => {
      const artifact = artifacts[Number(button.dataset.artifact)];
      if (!artifact?.path) return;
      const runId = latest.id;
      button.onclick = e => { e.stopPropagation(); previewResult(runId,artifact.name); };
      const download = document.createElement('button');
      download.className = 'btn'; download.textContent = 'Download';
      download.onclick = async () => {
        download.disabled = true;
        try {
          await downloadResult(runId, artifact.name);
        } catch (error) { notify(error.message, false); }
        finally { download.disabled = false; }
      };
      button.after(download);
    });
  };
  const basePersist = persist;
  persist = function () {
    // The prototype stores field values directly. Do not persist unsaved dialog edits.
    if (!settingsBefore) return basePersist();
    const ids = ['workspacePath','sandbox','memory'];
    const drafts = ids.map(id => $(id).value);
    ids.forEach((id,i) => $(id).value = settingsBefore[i]);
    try { basePersist(); } finally { ids.forEach((id,i) => $(id).value = drafts[i]); }
  };
  const banner = document.createElement('section');
  banner.className = 'card';
  banner.style.cssText = 'padding:16px 20px;margin-bottom:20px';
  banner.innerHTML = '<div class="form-row"><label class="field"><span>Run</span><select id="liveMode"><option value="team">Whole team</option><option value="single">One agent</option></select></label><label class="field hidden" id="singleAgentField"><span>Codex agent</span><select id="liveAgent"></select></label><label class="field" id="turnLimitField"><span>Maximum agent turns</span><input id="turnLimit" type="number" min="1" max="100" value="20"></label></div><p id="liveConnection" style="font-size:13px" role="status">Connecting to the local runner…</p><p id="liveRunNote" style="font-size:12px;margin-top:5px">Teams share files and use the handoff rules shown below. Each agent starts with a fresh session.</p><button class="btn hidden" id="convertCodex" style="margin-top:10px">Use Codex for this team</button>';
  banner.insertAdjacentHTML('beforeend', '<div style="display:flex;gap:10px;align-items:center;margin-top:12px;flex-wrap:wrap"><button class="btn" id="manageSignIn">Provider sign-in</button><button class="btn primary hidden" id="authRecovery">Sign in to Codex</button><span id="authSummary" role="status" style="font-size:13px"></span></div>');
  $('homeView').prepend(banner);
  const planCard = document.createElement('section');
  planCard.className = 'card hidden';
  planCard.style.cssText = 'padding:20px;margin-bottom:20px';
  planCard.id = 'planProgress';
  planCard.innerHTML = '<h2>Plan progress</h2><div id="planProgressBody" style="margin-top:12px"></div><div id="planResume" class="hidden" style="margin-top:16px"><label class="field"><span>What changed or what should the team try next? (optional)</span><textarea id="planResumeNote" rows="3" maxlength="6000" placeholder="For example: the acceptance image is now available at…"></textarea></label><p style="font-size:13px;margin-top:8px">Continue sends your update to the coordinator. Stop preserves the run for a follow-up.</p></div>';
  banner.after(planCard);
  function renderPlan() {
    const plan = latest?.plan;
    planCard.classList.toggle('hidden', !plan);
    if (!plan) return;
    const steps = plan.steps || [], batches = plan.batches || [];
    const count = status => steps.filter(s=>s.status===status).length;
    const verified = count('verified'), blocked = count('blocked');
    const awaiting = count('awaiting_review') + count('awaiting_tests');
    const batch = batches.find(b=>b.id===plan.current);
    const ended = !['running','paused','stopping'].includes(latest.status);
    const label = plan.outcome === 'complete' ? 'Plan complete' : ended ? `Run ${latest.status==='completed'?'finished':latest.status} · Plan ${plan.outcome === 'blocked' ? 'blocked' : 'incomplete'}` : 'Plan in progress';
    const needs = [...(plan.needs_input || []), ...batches.flatMap(b=>(b.blockers || []).map(x=>({...x,batch:b.id})))];
    const unique = needs.filter((x,i)=>needs.findIndex(y=>y.id===x.id && y.batch===x.batch)===i);
    const open = $('planSteps')?.open;
    $('planProgressBody').innerHTML = `<p><strong>${esc(label)}</strong></p><p style="margin:8px 0">${verified} of ${steps.length} steps verified · ${awaiting} awaiting validation · ${blocked} blocked</p><progress value="${verified}" max="${Math.max(1,steps.length)}" aria-label="Verified plan steps" style="width:100%"></progress>${batch?`<p style="margin:12px 0"><strong>${esc(batch.id)}: ${esc(batch.title)}</strong><br>${esc(batch.status.replaceAll('_',' '))} · Revision ${batch.revision}${batch.attention?' · Repeated issue needs a different approach':''}</p>`:''}${plan.notice?`<p role="status" style="margin:12px 0">${esc(plan.notice)}</p>`:''}${unique.length?'<h3 style="margin-top:16px">Needs input</h3>'+unique.map(b=>`<div style="margin-top:10px"><strong>${b.batch?esc(b.batch)+': ':''}${esc(b.need)}</strong><p>${esc(b.resume)}</p></div>`).join(''):''}<details id="planSteps" ${open?'open':''} style="margin-top:16px"><summary>All plan steps</summary><ul style="padding-left:20px;margin-top:8px">${steps.map(s=>`<li style="margin:6px 0"><strong>${esc(s.id)}</strong> ${esc(s.title)} — ${esc(s.status.replaceAll('_',' '))}</li>`).join('')}</ul></details>`;
    $('planResume').classList.toggle('hidden', viewingSaved || latest.status !== 'paused');
    if (ended) $('runHeadline').textContent = label;
  }
  const authDialog = document.createElement('dialog');
  authDialog.id = 'authDialog';
  authDialog.setAttribute('aria-labelledby','authTitle');
  authDialog.innerHTML = '<div class="dialog-head"><div><h2 id="authTitle">Provider sign-in</h2><p>Complete sign-in on the provider’s own page. Workbench never asks for your password.</p></div><button class="icon-btn" data-close="authDialog" aria-label="Close">×</button></div><div class="dialog-body"><div id="authProviders"></div><p style="font-size:12px;margin-top:12px">Claude sign-in is available here; running Claude tasks is not connected yet. A successful check confirms stored credentials, not remaining subscription usage.</p></div><div class="dialog-foot"><button class="btn" id="authRecheck">Check sign-in again</button><button class="btn primary" data-close="authDialog">Done</button></div>';
  document.body.appendChild(authDialog);
  const authConnections = document.createElement('section');
  authConnections.className = 'card';
  authConnections.style.cssText = 'padding:20px;margin-top:20px';
  authConnections.innerHTML = '<h2>Codex & Claude sign-in</h2><p style="margin:8px 0">Use your existing CLI account, or open the provider’s login flow.</p><button class="btn" id="connectionSignIn">Manage sign-in</button>';
  $('settingsView').prepend(authConnections);
  document.querySelector('#settingsView .section-head p').textContent = 'Manage CLI sign-in here. Ollama configuration is still a prototype.';
  const picker = $('liveAgent');
  document.querySelector('.demo-badge').textContent = 'Local · live';
  document.querySelector('.footnote > span').textContent = 'Live Codex runs can read or change files according to your workspace setting.';
  $('browseButton').hidden = false;
  $('workspacePath').placeholder = 'Enter an absolute project folder path';
  $('memory').disabled = true;
  $('memory').value = 'Start fresh each time';
  $('autoAdvance').disabled = false;
  $('kbdHint').hidden = true;
  $('goal').onkeydown = null;
  banner.insertAdjacentHTML('beforeend', '<div class="field" style="margin-top:16px"><label for="sourceRun">Build on a previous run</label><select id="sourceRun"><option value="">Start fresh</option></select><small id="sourceRunHint">Choose earlier work, then pick any team and describe what to do next.</small></div>');
  banner.insertAdjacentHTML('afterbegin', '<div id="savedRunBanner" class="hidden" style="margin-bottom:16px"><strong id="savedRunLabel">Viewing a saved run</strong> <button class="btn" id="returnToLive">Back to latest run</button></div>');
  $('returnToLive').onclick = () => {
    ++historyRequest; viewingSaved = false; revision = -1;
    receive(liveSnapshot || {revision:0,run:null}, true);
  };
  document.querySelector('#historyView .section-head p').textContent = 'Saved runs, results, and new work built on earlier results.';
  history = [];
  renderHistory = function () {
    $('historyList').innerHTML = savedRuns.length ? savedRuns.map(r=>`<div class="history-row"><div style="flex:1;min-width:0"><strong>${esc(r.goal)}</strong><p>${esc(new Date(r.started*1000).toLocaleString())} · ${esc(r.status)}${r.plan_outcome?' · Plan '+esc(r.plan_outcome.replaceAll('_',' ')):''} · ${r.count} files${r.source_run_id?' · Follow-up':''}</p><small>${esc((r.team||[]).join(' → '))}</small></div><button class="btn" data-saved-view="${esc(r.id)}">View</button><button class="btn primary" data-saved-continue="${esc(r.id)}" ${running||busy||['running','paused','stopping'].includes(r.status)?'disabled':''}>Continue from this run</button></div>`).join('') : '<div class="empty">Your saved runs will appear here.</div>';
    $('sourceRun').innerHTML = '<option value="">Start fresh</option>'+savedRuns.filter(r=>!['running','paused','stopping'].includes(r.status)).map(r=>`<option value="${esc(r.id)}">${esc(new Date(r.started*1000).toLocaleString())} · ${esc(r.goal.slice(0,100))}</option>`).join('');
    $('sourceRun').value = sourceRunId;
    $('sourceRun').disabled = running || busy;
  };
  async function refreshHistory() {
    try {
      const data = await api('/api/history');
      savedRuns = data.runs || []; historyChecked = Date.now(); renderHistory();
    } catch (error) { $('historyList').textContent = error.message; historyChecked = Date.now(); }
  }
  function chooseSource(id) {
    if (running || busy || liveActive()) return;
    if (viewingSaved) { viewingSaved = false; revision = liveSnapshot?.revision ?? revision; latest = null; outcome = null; events = []; artifacts = []; agents.forEach(a=>a.status='queued'); }
    sourceRunId = id;
    const source = savedRuns.find(r=>r.id===id);
    if (source) {
      $('workspacePath').value = source.cwd; updateWorkspace();
      $('goal').value = '';
      $('sourceRunHint').textContent = 'Source: '+source.goal+'. Pick any team and enter new instructions. Results will be copied into the new run.';
      showView('home'); $('goal').focus();
    } else $('sourceRunHint').textContent = 'Choose earlier work, then pick any team and describe what to do next.';
    renderHistory(); render();
  }
  $('sourceRun').onchange = () => chooseSource($('sourceRun').value);
  $('historyList').addEventListener('click', async e => {
    const button = e.target.closest('[data-saved-view],[data-saved-continue]');
    if (!button || button.disabled) return;
    if (button.dataset.savedContinue) return chooseSource(button.dataset.savedContinue);
    try {
      const request = ++historyRequest;
      const {run} = await api('/api/history?id='+encodeURIComponent(button.dataset.savedView));
      if (request !== historyRequest) return;
      if (['running','paused','stopping'].includes(run.status)) {
        viewingSaved = false; revision = -1; receive(await api('/api/run'));
      } else {
        viewingSaved = true;
        receive({run}, true);
      }
      showView('home');
      $('homeView').scrollIntoView?.({behavior:'smooth',block:'start'});
    } catch(error) { notify(error.message,false); }
  });
  renderHistory();

  function refreshPicker() {
    const selected = picker.value;
    const available = agents.filter(a => a.provider === 'Codex');
    picker.innerHTML = available.map(a => `<option value="${esc(a.id)}">${esc(a.name)}</option>`).join('');
    if (available.some(a => a.id === selected)) picker.value = selected;
    picker.disabled = running || busy;
    const team = $('liveMode').value === 'team';
    $('singleAgentField').classList.toggle('hidden',team);
    $('turnLimitField').classList.toggle('hidden',!team);
    $('liveMode').disabled = running || busy;
    $('turnLimit').disabled = running || busy;
    $('autoAdvance').disabled = !team || (running && !waiting) || busy;
    $('convertCodex').classList.toggle('hidden',!team || !agents.some(a=>a.provider!=='Codex'));
    $('convertCodex').disabled = running || busy;
    $('liveRunNote').textContent = team ? 'Codex-only team execution. Each turn must save its output file. Pause waits for the current agent to finish; Stop ends it immediately.' : 'Runs only the selected Codex agent, with a fresh session.';
  }

  render = function () {
    baseRender();
    refreshPicker();
    renderHistory();
    const team = $('liveMode').value === 'team';
    $('runButton').innerHTML = running ? (waiting?'Continue':latest?.pause_requested?'Pausing after this agent':latest?.team?'Pause after this agent':'Running selected agent') : team?'Run team':'Run selected agent';
    if (waiting && latest?.pause_reason==='turn_limit') $('runButton').innerHTML = `Allow ${latest.turn_budget || 20} more turns`;
    $('runButton').disabled = busy || !connected || (running ? latest?.status==='stopping'||(latest?.pause_requested&&!waiting)||(!latest?.team&&!waiting) : team?!agents.length||agents.some(a=>a.provider!=='Codex'):!picker.value);
    $('runBarMain').innerHTML = $('runButton').innerHTML;
    $('runBarMain').disabled = $('runButton').disabled;
    $('stopButton').disabled = busy || latest?.status === 'stopping';
    $('runBarStop').disabled = $('stopButton').disabled;
    $('savedRunBanner').classList.toggle('hidden',!viewingSaved);
    if (viewingSaved) {
      $('savedRunLabel').textContent = 'Viewing saved run · '+new Date(latest.started*1000).toLocaleString();
      $('returnToLive').textContent = liveActive()?'Back to active run':'Back to latest run';
      $('runButton').innerHTML = $('runBarMain').innerHTML = 'Continue from this run';
      $('runButton').disabled = $('runBarMain').disabled = busy || !connected || liveActive();
      $('stopButton').disabled = $('runBarStop').disabled = true;
      $('goal').disabled = true;
    } else $('goal').disabled = false;
    if (latest) {
      const next = latest.agents?.[latest.next_index];
      $('runHeadline').textContent = waiting ? `Ready for ${next?.name || 'the next agent'}` : `${latest.agent.name} · ${latest.status}`;
      $('runHelp').textContent = latest.error || (latest.team ? (waiting?'Review the results, then Continue.':`${latest.turns?.length||0} turns finished · Limit ${latest.max_turns} · Files: ${latest.artifact_dir}`) : 'This is a single-agent run. Other teammates are not started.');
      if (latest.history_error) $('runHelp').textContent += ' '+latest.history_error;
      if (waiting && latest.pause_reason==='turn_limit') {
        $('runHeadline').textContent = 'Turn limit reached — continue?';
        $('runHelp').textContent = `${latest.turns.length} turns finished. Your work is saved. Allow ${latest.turn_budget || 20} more turns to resume with ${next?.name || 'the next agent'}, or choose Stop. No agent is running while you decide.`;
      }
      $('runBarTitle').textContent = $('runHeadline').textContent;
      $('runBarSub').textContent = latest.team?'Codex team workflow':'One Codex agent';
    } else {
      $('runHeadline').textContent = team?'Your team is ready':'Ready for a real Codex run';
      $('runHelp').textContent = team?'Check your team and project folder. Team runs need permission to edit files.':'Choose a Codex agent above, set your project folder, then run.';
      if (!agents.length) { $('runHeadline').textContent='Build your team'; $('runHelp').textContent='Add your first teammate below Your team. Save as a new team when you are ready to name it.'; }
    }
    renderPlan();
    if (waiting && ['needs_input','plan_validation'].includes(latest?.pause_reason)) {
      $('runHeadline').textContent = latest.pause_reason==='needs_input'?'Your input is needed':'Batch validation needs attention';
      $('runHelp').textContent = latest.plan?.notice || 'Review plan progress, then continue with updated instructions.';
    }
    if (!running && team && agents.some(a=>a.workflow==='markdown-plan-v2') && planWorkflow!=='markdown-plan-v2') {
      $('runButton').disabled = $('runBarMain').disabled = true;
      $('liveRunNote').textContent = 'This preset needs the updated server. Let the current run finish, then restart Workbench to enable verified batches.';
    }
    if (latest) $('runBarTitle').textContent = $('runHeadline').textContent;
  };

  async function api(path, data) {
    const response = await fetch(path, data === undefined ? {cache:'no-store',headers:{'X-Workbench-Token':token}} : {
      method:'POST', headers:{'Content-Type':'application/json','X-Workbench-Token':token}, body:JSON.stringify(data)
    });
    const payload = await response.json();
    if (!response.ok) {
      if (payload.auth) showAuthRequired(payload.auth);
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    return payload;
  }

  let providerStates = [], authBusy = false, authLastChecked = 0, failedAuthRun = null;
  function paintAuth() {
    $('authProviders').innerHTML = providerStates.map(p => `<section style="padding:14px 0;border-bottom:1px solid var(--line)"><h3>${esc(p.provider)} <span class="pill">${esc(p.state.replaceAll('_',' '))}</span></h3><p style="font-size:13px;margin:8px 0">${esc(p.message)}</p><button class="btn" data-auth-provider="${esc(p.provider)}" data-auth-action="${p.state==='signing_in'?'cancel':'login'}" ${running||authBusy||['not_installed','environment'].includes(p.state)?'disabled':''}>${p.state==='signing_in'?'Cancel sign-in':p.state==='ready'?'Sign in again':'Sign in to '+esc(p.provider)}</button><details style="margin-top:10px;font-size:12px"><summary>If the browser does not open</summary><p>Run this in a normal terminal, then check sign-in again:</p><code>${esc(p.command)}</code></details></section>`).join('');
    $('authRecheck').disabled = authBusy;
    const codex = providerStates.find(p=>p.provider==='Codex');
    if (codex) {
      $('authSummary').textContent = 'Codex: '+codex.state.replaceAll('_',' ');
      $('authRecovery').classList.toggle('hidden',codex.state!=='sign_in_required');
    }
  }
  function showAuthRequired(state) {
    providerStates = providerStates.filter(p=>p.provider!==state.provider).concat(state);
    paintAuth();
    if (!authDialog.open) authDialog.showModal();
  }
  async function refreshAuth(force=false) {
    if (!connected || authBusy) return;
    authBusy = true; paintAuth();
    try { providerStates = (await api('/api/auth/status',{refresh:force})).providers; }
    catch (error) { $('authSummary').textContent = error.message; }
    finally { authBusy=false;authLastChecked=Date.now();paintAuth(); }
  }
  async function openAuth() { authDialog.showModal(); await refreshAuth(true); }
  $('manageSignIn').onclick = openAuth;
  $('connectionSignIn').onclick = openAuth;
  $('authRecovery').onclick = openAuth;
  $('authRecheck').onclick = () => refreshAuth(true);
  authDialog.addEventListener('click', async e => {
    const button = e.target.closest('[data-auth-action]');
    if (!button || button.disabled || authBusy) return;
    authBusy=true;paintAuth();
    try {
      const state = await api('/api/auth/'+button.dataset.authAction,{provider:button.dataset.authProvider});
      providerStates = providerStates.filter(p=>p.provider!==state.provider).concat(state);
    } catch(error) { notify(error.message,false); }
    finally { authBusy=false;authLastChecked=Date.now();paintAuth(); }
  });

  // Replace the mock tree with filesystem requests. Refresh only changes the
  // listing; choosing a directory edits the workspace draft, never an active run.
  let folderListing = null, folderRequest = 0, folderChecked = 0;
  let folderSignature = '', folderLoading = false;
  $('folderDialog').innerHTML = `
    <div class="dialog-head"><div><h2 id="folderTitle">Choose a project folder</h2><p>Folders update automatically while this window is open.</p></div><button class="icon-btn" data-close="folderDialog" aria-label="Close">${icon('x')}</button></div>
    <div class="dialog-body">
      <div style="display:flex;gap:8px;align-items:center;margin-bottom:12px">
        <button class="icon-btn" id="folderUp" aria-label="Up one folder">${icon('up')}</button>
        <label class="field" style="flex:1;margin:0"><span class="sr-only">Folder path</span><input id="liveFolderPath" autocomplete="off"></label>
        <button class="btn" id="liveFolderGo">Open</button><button class="btn" id="liveFolderRefresh">Refresh</button>
      </div>
      <input class="fb-filter" id="folderFilter" placeholder="Filter folders…" aria-label="Filter folders" autocomplete="off">
      <p id="liveFolderError" role="status" style="color:var(--danger);font-size:13px"></p>
      <div class="folder-list" id="folderList" role="list"></div>
    </div>
    <div class="dialog-foot fb-foot"><div class="fb-choice"><span><strong>Selected folder</strong><small id="folderChoicePath"></small></span></div><button class="btn" data-close="folderDialog">Cancel</button><button class="btn primary" id="useFolder" disabled>Use this folder</button></div>`;

  function paintFolders() {
    const filter = $('folderFilter').value.trim().toLowerCase();
    const signature = JSON.stringify([folderListing,filter]);
    if (signature === folderSignature) return;
    folderSignature = signature;
    const items = (folderListing?.folders || []).filter(f => f.name.toLowerCase().includes(filter));
    // Avoid replacing unchanged rows on automatic refresh, preserving keyboard focus.
    $('folderList').innerHTML = items.length ? items.map(f => `<div class="folder-row" role="listitem"><button class="folder-open" data-live-folder="${esc(f.path)}">${icon('folder')}<span class="fname">${esc(f.name)}</span>${f.git?'<span class="git-pill">Git</span>':''}${icon('chev')}</button></div>`).join('') : '<div class="folder-empty">'+(filter?'No folders match that filter.':'No subfolders. You can select this folder.')+'</div>';
    $('folderChoicePath').textContent = folderListing?.path || '';
    $('folderUp').disabled = !folderListing || folderListing.path === folderListing.parent;
  }

  async function loadFolders(path, automatic=false) {
    if (!path) return;
    const request = ++folderRequest;
    folderLoading = true;
    if (!automatic) {
      $('liveFolderError').textContent = 'Loading folders…';
      $('useFolder').disabled = true;
    }
    try {
      const listing = await api('/api/folders?path='+encodeURIComponent(path));
      if (request !== folderRequest || !$('folderDialog').open) return;
      folderListing = listing;
      if (!automatic) $('liveFolderPath').value = listing.path;
      $('liveFolderError').textContent = '';
      $('useFolder').disabled = false;
      paintFolders();
    } catch (error) {
      if (request !== folderRequest || !$('folderDialog').open) return;
      $('liveFolderError').textContent = error.message;
      $('useFolder').disabled = true;
    } finally {
      if (request === folderRequest) { folderLoading = false; folderChecked = Date.now(); }
    }
  }
  $('browseButton').onclick = () => {
    folderListing = null; folderSignature = ''; $('folderFilter').value = '';
    $('folderList').innerHTML = ''; $('folderChoicePath').textContent = '';
    $('liveFolderPath').value = $('workspacePath').value;
    $('folderDialog').showModal(); loadFolders($('workspacePath').value);
  };
  $('folderDialog').addEventListener('close', () => { ++folderRequest; folderLoading = false; });
  $('folderList').addEventListener('click', e => {
    const button = e.target.closest('[data-live-folder]');
    if (button) { $('folderFilter').value = ''; loadFolders(button.dataset.liveFolder); }
  });
  $('folderFilter').addEventListener('input', paintFolders);
  $('folderUp').onclick = () => { if (folderListing) { $('folderFilter').value = ''; loadFolders(folderListing.parent); } };
  $('liveFolderGo').onclick = () => { $('folderFilter').value = ''; loadFolders($('liveFolderPath').value); };
  $('liveFolderPath').addEventListener('keydown', e => { if(e.key==='Enter'){e.preventDefault();$('liveFolderGo').click();} });
  $('liveFolderRefresh').onclick = () => loadFolders(folderListing?.path || $('liveFolderPath').value);
  $('useFolder').onclick = () => {
    if (!folderListing || $('useFolder').disabled) return;
    $('workspacePath').value = folderListing.path;
    $('folderDialog').close(); renderRecent();
    notify('Folder selected. Press Save to use it for the next run.');
  };

  function receive(snapshot, historical = false) {
    if (!historical) {
      liveSnapshot = snapshot;
      if (viewingSaved) { render(); return; }
      if (snapshot.revision === revision) return;
      revision = snapshot.revision;
    }
    const newRun = snapshot.run?.id !== latest?.id;
    const changedStatus = snapshot.run?.status !== latest?.status;
    latest = snapshot.run;
    if (newRun || changedStatus) historyChecked = 0;
    if (!latest) {
      running = paused = waiting = false; outcome = null;
      agents.forEach(a => a.status = 'queued'); events = []; artifacts = [];
      render(); return;
    }
    if (!historical && latest.auth_provider && failedAuthRun !== latest.id) {
      failedAuthRun = latest.id;
      showAuthRequired({provider:latest.auth_provider,state:'sign_in_required',message:'This run was rejected because its credentials were missing or expired. Sign in, then retry the run.',command:latest.auth_provider==='Codex'?'codex.cmd login':'claude.cmd auth login'});
    }
    running = ['running','stopping','paused'].includes(latest.status);
    // Keep an empty team draft across refreshes instead of replacing it with
    // the server's last completed team. History can still reopen that run.
    if (!historical && !running && !agents.length) {
      latest = null; paused = waiting = false; outcome = null; events = []; artifacts = [];
      render(); return;
    }
    if (newRun && running) {
      sourceRunId = latest.source_run_id || '';
      $('sourceRunHint').textContent = latest.source_goal ? 'Building on: '+latest.source_goal : 'Choose earlier work, then pick any team and describe what to do next.';
    }
    paused = false; waiting = latest.status === 'paused';
    outcome = running ? null : latest.status === 'completed' ? 'done' : 'stopped';
    staleRun = false;
    if (latest.team && latest.agents) {
      agents = latest.agents.map(a=>({...a}));
      $('liveMode').value='team';
      $('turnLimit').value=Math.min(100,latest.max_turns);
      if (!waiting || newRun) $('autoAdvance').checked=latest.auto_advance;
      trail = new Set(latest.trail || []);
    }
    if (historical && !latest.team) {
      agents = (latest.agents || [latest.agent]).map(a=>({...a}));
      $('liveMode').value = 'single'; picker.value = latest.agent.id;
      trail = new Set(latest.trail || []);
    }
    let index = latest.team ? latest.current : agents.findIndex(a => a.id === latest.agent.id && a.name === latest.agent.name);
    if (index < 0 && running) { agents.push({...latest.agent,status:'queued'}); index = agents.length - 1; }
    current = waiting && latest.next_index != null ? latest.next_index : Math.max(0, index);
    if (!latest.team) agents.forEach((a,i) => a.status = i === index ? (running ? 'running' : latest.status === 'completed' ? 'done' : 'queued') : 'queued');
    if (running || historical) {
      $('goal').value = latest.goal;
      if ((newRun || historical) && !$('runSettings').open) {
        $('workspacePath').value = latest.cwd;
        $('sandbox').value = latest.sandbox;
        updateWorkspace();
      }
    }
    events = (latest.events || []).map(e => ({time:e.time,title:e.level === 'error' ? 'Error' : e.agent || 'Codex',body:e.text,glyph:e.level === 'error'?'bug':'code',color:null,artifact:-1}));
    artifacts = (latest.artifacts || []).map(a=>({...a,color:COLORS[0],body:(a.truncated?'[Preview truncated to 100,000 characters. Full file: '+a.path+']\n\n':'')+a.body}));
    if (!latest.team && latest.output) artifacts.push({name:'Agent response',agent:latest.agent.name,time:'Latest run',version:1,color:COLORS[0],body:latest.output});
    lastSeconds = Math.round(((latest.finished || Date.now()/1000) - latest.started));
    render();
  }

  primaryAction = async function () {
    if (busy || !connected) return;
    if (viewingSaved) return chooseSource(latest.id);
    if (running) {
      if (!latest?.team || latest.status==='stopping' || (latest.pause_requested&&!waiting)) return;
      busy=true;render();
      try {
        const extend=waiting && latest.pause_reason==='turn_limit';
        const note = waiting && latest.plan ? $('planResumeNote').value.trim() : '';
        receive(await api('/api/control',{id:latest.id,action:extend?'extend':waiting?'continue':'pause',auto_advance:$('autoAdvance').checked,...(extend?{additional_turns:latest.turn_budget || 20}:{}),...(note?{note}:{})}));
        $('planResumeNote').value = '';
      }
      catch(error){notify(error.message,false);}
      finally{busy=false;render();}
      return;
    }
    const team = $('liveMode').value==='team';
    if (team && agents.some(a=>a.workflow==='markdown-plan-v2') && planWorkflow!=='markdown-plan-v2') return notify('Let the active run finish, then restart Workbench to enable verified batches.',false);
    const agent = agents.find(a => a.id === picker.value);
    if (!team && (!agent || agent.provider !== 'Codex')) return notify('Select a Codex agent.', false);
    if (team && agents.some(a=>a.provider!=='Codex')) return notify('Switch this team to Codex first.',false);
    if (team && !agents.length) return notify('Add at least one teammate before running.',false);
    busy = true; render();
    try {
      const snapshot = await api('/api/run', {goal:$('goal').value,cwd:$('workspacePath').value,sandbox:$('sandbox').value,source_run_id:sourceRunId||null,...(team?{agents,auto_advance:$('autoAdvance').checked,max_turns:Number($('turnLimit').value)}:{agent})});
      receive(snapshot);
    } catch (error) { notify(error.message, false); }
    finally { busy = false; render(); }
  };
  async function stop() {
    if (!latest || !running || busy) return;
    busy = true; render();
    try { receive(await api('/api/stop', {id:latest.id})); }
    catch (error) { notify(error.message, false); }
    finally { busy = false; render(); }
  }
  $('runButton').onclick = () => primaryAction();
  $('runBarMain').onclick = () => primaryAction();
  $('stopButton').onclick = stop;
  $('runBarStop').onclick = stop;
  picker.onchange = () => render();
  $('liveMode').onchange = () => render();
  $('convertCodex').onclick = () => {
    if(running||busy)return;
    agents=agents.map(a=>({...a,provider:'Codex',model:a.provider==='Codex'?a.model:'',status:'queued'}));
    teamChanged();persist();render();notify('Team switched to Codex. Models for converted roles use the Codex default.');
  };
  const originalStateOf=stateOf;
  stateOf=(a,i)=>a.status==='failed'?['paused','Failed']:a.status==='stopped'?['paused','Stopped']:originalStateOf(a,i);
  async function poll() {
    try {
      if (!connected) {
        const config = await api('/api/bootstrap');
        planWorkflow = config.plan_workflow || null;
        token = config.token; connected = true; revision = -1;
        let workspace = null;
        try { workspace = JSON.parse(localStorage.getItem('workbench-live-workspace')); } catch {}
        if (!viewingSaved) {
          $('workspacePath').value = workspace?.cwd || config.cwd;
          $('sandbox').value = workspace?.sandbox || 'read-only';
          updateWorkspace();
        }
        $('liveConnection').textContent = config.codex_available ? 'Codex found · connected to local runner' : 'Codex was not found. Install/sign in to Codex CLI, then restart this server.';
        render();
      }
      receive(await api('/api/run'));
      if (Date.now()-historyChecked>=5000) await refreshHistory();
      if ((authDialog.open || !authLastChecked) && !authBusy && Date.now()-authLastChecked>=3000) await refreshAuth();
      if ($('folderDialog').open && folderListing && !folderLoading && Date.now()-folderChecked>=1500) {
        await loadFolders(folderListing.path, true);
      }
    } catch (error) {
      connected = false;
      $('liveConnection').textContent = 'Connection lost. Reconnecting… A running agent may still be active.';
      render();
    } finally { setTimeout(poll, 700); }
  }
  const saveWorkspace = $('saveSettings').onclick;
  $('saveSettings').onclick = () => {
    saveWorkspace();
    if (!$('runSettings').open) {
      try { localStorage.setItem('workbench-live-workspace',JSON.stringify({cwd:$('workspacePath').value,sandbox:$('sandbox').value})); } catch {}
    }
  };
  render(); poll();
})();
