"""Structured, opt-in Markdown-plan workflow; existing runs remain unchanged.

Agents submit one bounded JSON update per turn. The service owns accepted state,
revision fingerprints and receipts; Markdown reports remain the human evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

VERSION = 'markdown-plan-v2'
ROLES = ('plan-coordinator', 'batch-implementer', 'batch-reviewer', 'batch-tester', 'plan-reporter')


def initial_state():
    return dict(source=None, steps=[], batches=[], current=None, outcome='in_progress',
                needs_input=[], history=[])


def text(value, name, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f'{name} must be nonempty text, at most {limit} characters.')
    return value.strip()


def strings(value, name, maximum=1000, empty=False):
    if not isinstance(value, list) or len(value) > maximum or (not value and not empty):
        raise ValueError(f'{name} must be a list of up to {maximum} entries.')
    result = [text(v, name, 500) for v in value]
    if len(set(result)) != len(result):
        raise ValueError(f'{name} contains duplicates.')
    return result


def local_path(root, name):
    name = text(name, 'Path', 1000)
    relative = Path(name)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Use workspace-relative paths without parent traversal.')
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('Plan evidence must remain inside the workspace.')
    return path


def digest(path):
    if not path.exists():
        return None  # Deleted files are part of a revision, too.
    if not path.is_file():
        raise ValueError('Revision paths must identify files.')
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fingerprints(root, names):
    return {name: digest(local_path(root, name)) for name in names}


def blockers(value):
    if not isinstance(value, list) or len(value) > 30:
        raise ValueError('blockers must be a list of at most 30 missing inputs.')
    if any(not isinstance(item, dict) for item in value):
        raise ValueError('Each blocker needs id, need and resume fields.')
    return [{key: text(item.get(key), key) for key in ('id', 'need', 'resume')} for item in value]


def refresh(state, root):
    """Invalidate stale approvals, including previously verified work."""
    for batch in state['batches']:
        if batch.get('fingerprints') is not None and fingerprints(root, batch['files']) != batch['fingerprints']:
            batch.update(status='needs_changes', review=None, tests=None)
            batch['stale'] = True
    verified = {s for b in state['batches'] if b['status'] == 'verified' for s in b['steps']}
    # Dependency invalidation propagates even when downstream files did not change.
    changed = True
    while changed:
        changed = False
        for b in state['batches']:
            if b['status'] == 'verified' and any(
                    d not in verified and d not in b['steps']
                    for s in state['steps'] if s['id'] in b['steps'] for d in s['depends_on']):
                b.update(status='needs_changes', review=None, tests=None)
                verified.difference_update(b['steps'])
                changed = True
    for step in state['steps']:
        batch = next((b for b in state['batches'] if step['id'] in b['steps']), None)
        step['status'] = batch['status'] if batch else 'pending'
    state['outcome'] = 'in_progress'


def apply_update(previous, update, role, turn, root, report):
    """Validate transactionally. Return accepted state and optional routing override."""
    if not isinstance(update, dict) or update.get('turn') != turn or isinstance(update.get('turn'), bool):
        raise ValueError(f'PLAN_UPDATE.json must contain this turn number: {turn}.')
    state = copy.deepcopy(previous)
    refresh(state, root)
    override = None
    if state['source'] and digest(local_path(root, state['source']['path'])) != state['source']['sha256']:
        raise ValueError('The source plan changed. Restore it or start a follow-up to reconcile the new plan.')
    if role == 'plan-coordinator':
        if not state['source'] and update.get('source'):
            path = text(update['source'], 'source')
            sha = digest(local_path(root, path))
            if sha is None:
                raise ValueError('The source plan does not exist.')
            raw = update.get('steps')
            if not isinstance(raw, list) or not 1 <= len(raw) <= 2000:
                raise ValueError('Initialize every source-plan step (1–2000 entries).')
            steps = []
            for item in raw:
                if not isinstance(item, dict):
                    raise ValueError('Each step must have id, title and depends_on.')
                steps.append(dict(id=text(item.get('id'), 'Step ID', 80),
                                  title=text(item.get('title'), 'Step title', 500),
                                  depends_on=strings(item.get('depends_on', []), 'depends_on', empty=True), status='pending'))
            ids = [s['id'] for s in steps]
            if len(set(ids)) != len(ids) or any(d not in ids or d == s['id'] for s in steps for d in s['depends_on']):
                raise ValueError('Step IDs must be unique and dependencies must reference other known steps.')
            pending, resolved = list(steps), set()
            while pending:
                ready = [s for s in pending if set(s['depends_on']) <= resolved]
                if not ready:
                    raise ValueError('Plan dependencies contain a cycle.')
                resolved.update(s['id'] for s in ready)
                pending = [s for s in pending if s not in ready]
            state.update(source=dict(path=path, sha256=sha), steps=steps)
        elif 'steps' in update or 'source' in update:
            raise ValueError('The source and step inventory are immutable after initialization.')
        action = update.get('action')
        if action == 'select':
            if not state['source']:
                raise ValueError('Initialize the source plan before selecting a batch.')
            batch_id = text(update.get('batch'), 'batch', 80)
            batch = next((b for b in state['batches'] if b['id'] == batch_id), None)
            if not batch:
                selected = strings(update.get('steps_in_batch'), 'steps_in_batch', 100)
                ids = {s['id'] for s in state['steps']}
                used = {s for b in state['batches'] for s in b['steps']}
                if not set(selected) <= ids:
                    raise ValueError('Select known plan steps.')
                if set(selected) & used:
                    parent = next((b for b in state['batches'] if b['id'] == update.get('split_from')), None)
                    if not parent or parent['status'] not in ('blocked', 'needs_changes') or not set(selected) < set(parent['steps']):
                        raise ValueError('Assigned steps need their existing batch, or split_from a blocked batch with a strict subset of its steps.')
                    parent['steps'] = [s for s in parent['steps'] if s not in selected]
                    parent.update(review=None, tests=None, status='blocked')
                batch = dict(id=batch_id, title=text(update.get('title'), 'Batch title', 500), steps=selected,
                             acceptance=strings(update.get('acceptance'), 'acceptance', 100),
                             revision=0, files=[], fingerprints=None, review=None, tests=None,
                             status='implementing', blockers=[], failures={}, attempts=0)
                state['batches'].append(batch)
            if batch['status'] == 'verified':
                raise ValueError('This batch is already verified; choose remaining work.')
            verified = {s['id'] for s in state['steps'] if s['status'] == 'verified'}
            if any(d not in verified and d not in batch['steps'] for s in state['steps']
                   if s['id'] in batch['steps'] for d in s['depends_on']):
                raise ValueError('This batch has unverified dependencies. Select independent work.')
            if batch.get('attention'):
                strategy = text(update.get('strategy'), 'A changed approach or new input is required')
                if strategy == batch.get('strategy'):
                    raise ValueError('Do not repeat the same failed approach.')
                batch.update(strategy=strategy, attention=False, failures={})
            state.update(current=batch_id, needs_input=[])
        elif action in ('wait', 'report'):
            state['needs_input'] = blockers(update.get('blockers', []))
            if action == 'wait' and not state['needs_input']:
                raise ValueError('Explain the missing input and how to resume.')
            override = 'wait' if action == 'wait' else 'plan-reporter'
        else:
            raise ValueError('Coordinator action must be select, wait or report.')
    elif role == 'plan-reporter':
        verdict = update.get('outcome')
        if verdict not in ('complete', 'partial', 'blocked'):
            raise ValueError('Reporter outcome must be complete, partial or blocked.')
        if verdict == 'complete' and (not state['steps'] or any(s['status'] != 'verified' for s in state['steps'])):
            raise ValueError('Plan completion requires current review and passing tests for every step.')
        state['outcome'] = verdict
        if verdict != 'complete':
            state['needs_input'] = blockers(update.get('blockers', []))
    else:
        batch = next((b for b in state['batches'] if b['id'] == state['current']), None)
        if not batch or update.get('batch') != batch['id']:
            raise ValueError('Update must identify the selected batch.')
        batch['blockers'] = blockers(update.get('blockers', []))
        if role == 'batch-implementer':
            names = strings(update.get('files'), 'files', 2000, empty=True)
            if state['source']['path'] in names:
                raise ValueError('Do not modify the source plan.')
            # Retain earlier changed paths, so repairs cannot drop evidence from the fingerprint.
            batch['files'] = sorted(set(batch['files']) | set(names))
            batch.update(revision=batch['revision'] + 1, review=None, tests=None, stale=False,
                         fingerprints=fingerprints(root, batch['files']), implementation=report,
                         status='blocked' if batch['blockers'] else 'awaiting_review')
            if batch['blockers']:
                for blocker in batch['blockers']:
                    key = blocker['id']
                    batch['failures'][key] = batch['failures'].get(key, 0) + 1
                batch['attention'] = max(batch['failures'].values()) >= 3
                override = 'plan-coordinator'
        else:
            if update.get('revision') != batch['revision'] or not batch['revision'] or batch.get('stale'):
                raise ValueError('Evidence must match the current unchanged implementation revision. Resubmit changed files through the implementer.')
            verdict = update.get('verdict')
            if verdict not in ('passed', 'failed', 'blocked'):
                raise ValueError('Review/test verdict must be passed, failed or blocked.')
            checks = update.get('checks')
            if not isinstance(checks, list) or len(checks) > 100:
                raise ValueError('Provide acceptance checks with criterion, result and evidence.')
            for check in checks:
                if not isinstance(check, dict) or check.get('criterion') not in batch['acceptance']:
                    raise ValueError('Every check must reference an exact batch acceptance criterion.')
                if check.get('result') not in ('passed', 'failed', 'skipped'):
                    raise ValueError('Check result must be passed, failed or skipped.')
                text(check.get('evidence'), 'Check evidence', 4000)
            if verdict == 'passed' and (batch['blockers'] or
                    {c['criterion'] for c in checks if c['result'] == 'passed'} != set(batch['acceptance']) or
                    any(c['result'] != 'passed' for c in checks)):
                raise ValueError('Passing requires evidence for every acceptance criterion, without skipped checks or blockers.')
            field = 'review' if role == 'batch-reviewer' else 'tests'
            if role == 'batch-tester' and verdict == 'passed' and not (batch['review'] and batch['review']['verdict'] == 'passed'):
                raise ValueError('Passing tests cannot verify a batch without current independent review approval.')
            batch[field] = dict(revision=batch['revision'], verdict=verdict, report=report, checks=checks)
            if verdict == 'passed':
                batch['status'] = 'awaiting_tests' if field == 'review' else 'verified'
                override = 'batch-tester' if field == 'review' else 'plan-coordinator'
            else:
                issues = strings(update.get('issues'), 'Stable issue IDs', 30)
                for issue in issues:
                    batch['failures'][issue] = batch['failures'].get(issue, 0) + 1
                batch['attempts'] += 1
                batch['attention'] = max(batch['failures'].values()) >= 3
                batch['status'] = 'blocked' if verdict == 'blocked' or batch['attention'] else 'needs_changes'
                override = 'plan-coordinator' if batch['status'] == 'blocked' else 'batch-implementer'
    # Refresh derived step status without erasing the reporter's outcome.
    outcome = state['outcome']
    refresh(state, root)
    if outcome == 'complete' and any(s['status'] != 'verified' for s in state['steps']):
        raise ValueError('Files changed during completion validation. Revalidate affected batches.')
    state['outcome'] = outcome
    state['history'].append(dict(turn=turn, role=role, batch=state['current'], report=report))
    return state, override


def prompt(state, role, turn, directory):
    batch = next((b for b in state['batches'] if b['id'] == state['current']), None)
    summary = dict(source=state['source'], current=batch, counts={
        status: sum(s['status'] == status for s in state['steps'])
        for status in ('pending', 'implementing', 'awaiting_review', 'awaiting_tests', 'needs_changes', 'blocked', 'verified')},
        needs_input=state['needs_input'])
    schemas = {
        'plan-coordinator': 'First visit: add "source":"docs/PLAN.md", "steps":[{"id":"S1","title":"Short original requirement","depends_on":[]}]. Thereafter omit source/steps. Use "action":"select", "batch":"B001". For NEW batches also provide "title", "steps_in_batch":["S1"], "acceptance":["Exact measurable criterion"]. Resume an existing batch by ID, then hand off directly to the role it needs. To separate independent steps from an existing blocked batch, create a NEW batch with "split_from":"old-batch-id" and a strict subset of its steps; approvals are not inherited. For repeated failures include a genuinely different "strategy". Use "action":"wait", "blockers":[{"id":"env","need":"Specific missing input","resume":"What to supply or change"}] ONLY when no independent work remains; the service pauses for user input. Or use "action":"report" to finish with a complete/partial/blocked report.',
        'batch-implementer': 'Provide "batch":"B001", "files":["workspace/relative/changed-file.py"], "blockers":[]. List EVERY changed product, test and documentation file (including deletions), excluding run bookkeeping. Service increments revision and fingerprints files; never copy old approval. A blocked submission goes to the coordinator.',
        'batch-reviewer': 'Provide "batch":"B001", "revision":1, "verdict":"passed|failed|blocked", "checks":[{"criterion":"Exact acceptance criterion","result":"passed|failed|skipped","evidence":"Specific inspected diff/test evidence"}], "blockers":[], "issues":["stable-finding-id"] for failed/blocked verdicts. Inspect actual files and verify the implementation file list is exhaustive. Passed review routes to tester; failures to implementer; blockers/repeated failures to coordinator.',
        'batch-tester': 'Provide "batch":"B001", "revision":1, "verdict":"passed|failed|blocked", "checks":[{"criterion":"Exact acceptance criterion","result":"passed|failed|skipped","evidence":"Command, exit status, observed result and evidence path"}], "blockers":[], "issues":["stable-finding-id"] for failed/blocked verdicts. Do not modify product/tests: request changes through implementer so review remains current. Passing requires current review and all criteria passed. Diagnostic testing of blocked work may report blocked/failed but cannot verify it.',
        'plan-reporter': 'Provide "outcome":"complete|partial|blocked", "blockers":[]. Complete is rejected unless every registered step has matching review and passing tests. Ensure the registered step inventory covers the entire source plan; report partial if any requirement is missing.',
    }
    return (f'\nStructured plan protocol (takes precedence over older bookkeeping instructions):\n'
            f'Write {directory}/PLAN_UPDATE.json as a JSON object with "turn":{turn} before ending this turn. '
            f'{schemas[role]} Every blocker uses id, need and resume text fields.\n'
            f'The server owns PLAN_STATE.json; read it for the full step inventory, never edit it. '
            'Keep PLAN_PROGRESS.md compact: current batch, decisions, blockers and links. Do not copy the source plan or maintain an append-only transcript. '
            'Reports are archived automatically per turn. Read relevant source sections and current evidence first, not every historical report. '
            'For coordinator wait, include the normal final handoff to Plan Reporter; the service pauses before starting it. '
            'Group related steps into meaningful reviewable outcomes; separate environment-gated acceptance from independent steps before assigning batches. '
            'Retain required repository checks. Never treat missing or skipped checks as passed.\n'
            f'Accepted state summary:\n{json.dumps(summary, ensure_ascii=False)}\n')
