/* Independent chat: never starts, stops, or changes the selected team run. */
(() => {
  'use strict';
  let token = '', ready = false, busy = false, chat = null, activeRun = false;
  let proposalId = null, messageStamp = '', runsStamp = '', initialized = false;
  let requestError = '', version = 0;
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const working = () => ['running','preparing','stopping'].includes(chat?.status);
  async function api(path, data) {
    const response = await fetch(path, {cache:'no-store', headers:{'X-Workbench-Token':token,...(data?{'Content-Type':'application/json'}:{})},
      ...(data?{method:'POST',body:JSON.stringify(data)}:{})});
    const body = await response.json();
    if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
    return body;
  }
  function render() {
    const active = working(), proposal = chat?.proposal;
    $('improveSend').disabled = !ready || busy || active || !$('improveRun').value;
    $('improveSend').textContent = $('improveMode').value==='prepare'?'Prepare improvements':'Send message';
    for (const id of ['improveRun','improveMode','improveSource']) $(id).disabled = busy || active;
    $('improveStop').classList.toggle('hidden', !active);
    $('improveStop').disabled = busy || chat?.status==='stopping';
    if (ready) $('improveStatus').textContent = requestError || chat?.error || (active ? 'Assistant is working independently. Your team run continues unchanged.' :
      chat ? `Conversation about: ${chat.run_goal}${chat.status==='ready'?' · Improvement ready for review':''}` : 'Choose a run and ask a question. Review mode makes no changes.');
    const stamp = JSON.stringify(chat?.messages || []);
    if (stamp !== messageStamp) {
      messageStamp = stamp;
      $('improveMessages').innerHTML = chat?.messages?.length ? chat.messages.map(m=>`<article style="padding:14px;border:1px solid var(--line);border-radius:12px;${m.role==='user'?'background:var(--surface-2)':''}"><strong>${m.role==='user'?'You':'Workbench assistant'}</strong><div style="white-space:pre-wrap;overflow-wrap:anywhere;margin-top:8px">${escape(m.text)}</div></article>`).join('') : '<p>Select a run, then ask what worked and what could improve.</p>';
      $('improveMessages').scrollTop = $('improveMessages').scrollHeight;
    }
    $('improveActivity').textContent = (chat?.activity || []).join('\n\n');
    $('improveProposal').classList.toggle('hidden', !proposal);
    if (proposalId !== proposal?.id) {
      proposalId = proposal?.id;
      $('improveApprove').checked = false;
    }
    if (proposal) {
      $('improveSummary').textContent = proposal.summary;
      $('improveDiff').textContent = proposal.diff;
      $('improveTests').innerHTML = '<strong>Tests reported by the assistant</strong><ul style="padding-left:20px;margin-top:8px">'+proposal.tests.map(t=>`<li><code>${escape(t.command)}</code> — ${escape(t.result)}${t.exit_code!=null?' (exit '+escape(t.exit_code)+')':''}<p style="white-space:pre-wrap">${escape(t.evidence)}</p></li>`).join('')+'</ul>'+(Array.isArray(proposal.limitations)&&proposal.limitations.length?'<p>Limitations: '+proposal.limitations.map(escape).join('; ')+'</p>':'');
      $('improveApply').disabled = !ready || busy || active || activeRun || proposal.applied || !proposal.can_apply || !$('improveApprove').checked;
      $('improveApprove').disabled = proposal.applied || busy || active;
      $('improveRevert').classList.toggle('hidden', !proposal.applied);
      $('improveRevert').disabled = !ready || busy || active || activeRun;
      $('improveApplyHint').textContent = proposal.reverted ? 'Reverted in source. Prepare a new proposal to make further changes.' : proposal.applied ? 'Applied to source. Restart manually when ready; packaged apps need rebuilding. Revert is available if these files have no newer edits.' : activeRun ? 'Wait for the active workflow to finish before applying.' : !proposal.can_apply ? 'Tests are failed or skipped. Ask the assistant to prepare a corrected improvement.' : 'Applies exactly the displayed proposal. No automatic restart, commit, or deployment.';
    }
  }
  $('improveForm').onsubmit = async event => {
    event.preventDefault();
    if (!ready || busy || working()) return;
    const message = $('improveMessage').value.trim();
    if (!message) return;
    busy = true; requestError = ''; version++; render();
    try {
      const result = await api('/api/improve', {message,mode:$('improveMode').value,run_id:$('improveRun').value,source_root:$('improveSource').value});
      chat = result.chat; $('improveMessage').value = '';
    } catch (error) { requestError = error.message; }
    finally { busy = false; render(); }
  };
  $('improveStop').onclick = async () => {
    if (busy) return;
    busy = true; requestError = ''; version++; render();
    try { chat = (await api('/api/improve/stop', {})).chat; }
    catch(error) { requestError = error.message; }
    finally { busy = false; render(); }
  };
  $('improveApply').onclick = async () => {
    if ($('improveApply').disabled || !chat?.proposal) return;
    busy = true; requestError = ''; version++; render();
    try { chat = (await api('/api/improve/apply', {proposal_id:chat.proposal.id})).chat; }
    catch(error) { requestError = error.message; }
    finally { busy = false; render(); }
  };
  $('improveApprove').onchange = render;
  $('improveRevert').onclick = async () => {
    if ($('improveRevert').disabled || !chat?.proposal?.applied) return;
    busy = true; requestError = ''; version++; render();
    try { chat = (await api('/api/improve/revert', {proposal_id:chat.proposal.id})).chat; }
    catch(error) { requestError = error.message; }
    finally { busy = false; render(); }
  };
  $('improveMode').onchange = render;
  $('improveRun').onchange = render;
  async function poll() {
    try {
      if (!ready) {
        const config = await api('/api/bootstrap');
        if (!config.improvement_chat) {
          $('improveStatus').textContent = 'Let the current run finish, then restart Workbench to enable improvement chat.';
          return;
        }
        token = config.token; ready = true;
      }
      // A POST owns its response; do not let an older poll overwrite it.
      if (!busy) {
        const pollVersion = version;
        const [state, history] = await Promise.all([api('/api/improve'), api('/api/history')]);
        if (busy || pollVersion !== version) return;
        chat = state.chat;
        const runs = history.runs || [];
        activeRun = runs.some(r=>['running','paused','stopping'].includes(r.status));
        const stamp = JSON.stringify(runs.map(r=>[r.id,r.goal,r.status]));
        if (stamp !== runsStamp) {
          runsStamp = stamp;
          const selected = $('improveRun').value || chat?.run_id;
          $('improveRun').innerHTML = '<option value="">Choose a run</option>'+runs.map(r=>`<option value="${escape(r.id)}">${escape(r.goal.slice(0,100))} · ${escape(r.status)}</option>`).join('');
          $('improveRun').value = runs.some(r=>r.id===selected)?selected:runs[0]?.id || '';
        }
        if (!initialized) {
          $('improveSource').value = chat?.source_root || state.source_root;
          initialized = true;
        }
      }
      render();
    } catch(error) {
      ready = false; render(); $('improveStatus').textContent = 'Connection unavailable: '+error.message;
    } finally { setTimeout(poll, 1500); }
  }
  poll();
})();
