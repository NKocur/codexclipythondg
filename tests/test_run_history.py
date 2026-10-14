import json
import tempfile
import unittest
from pathlib import Path

from run_history import RunHistory
from workflow_service import RunService
from test_workflow_service import Runner, role, handoff


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.workspace = self.root / 'research'
        self.workspace.mkdir()
        self.history = self.root / 'sessions' / 'web-runs'
        self.service = RunService(Runner, self.history)

    def tearDown(self):
        self.temp.cleanup()

    def start(self, service=None, **kwargs):
        return (service or self.service).start(dict(
            goal='Literature review', cwd=str(self.workspace), sandbox='workspace-write',
            agents=[role('Researcher', 'end')], **kwargs))['run']

    def finish(self):
        run = self.service.run
        path = Path(run['cwd']) / run['artifact_dir'] / run['agent']['artifact']
        path.write_text('# Literature review\nPrior findings', encoding='utf-8')
        self.service.runner.event({'type':'item.completed','item':{'type':'agent_message','text':'Review complete.'}})
        self.service.runner.done(0, '')
        return path

    def paused_run(self, limit=20):
        run = self.service.start(dict(goal='Build', cwd=str(self.workspace), sandbox='workspace-write',
            agents=[role('Planner'), role('Coder', 'end')], auto_advance=False, max_turns=limit))['run']
        (self.workspace / run['artifact_dir'] / 'PLANNER.md').write_text('Plan')
        self.service.runner.event({'type':'item.completed','item':{'type':'agent_message','text':handoff('Coder')}})
        self.service.runner.done(0, '')
        self.assertEqual(self.service.run['status'], 'paused')
        return self.service.snapshot()['run']

    def test_restore_preserves_checkpoint_without_starting_agent(self):
        original = self.paused_run()
        restarted = RunService(Runner, self.history)
        self.assertEqual(restarted.list_runs()[0]['status'], 'suspended')
        count = len(Runner.instances)
        restored = restarted.restore(original['id'])['run']
        self.assertEqual(len(Runner.instances), count)
        for key in ('agents', 'turns', 'relay', 'next_index', 'max_turns', 'artifact_dir'):
            self.assertEqual(restored[key], original[key])
        restarted.control(original['id'], 'continue')
        self.assertEqual(restarted.run['agent']['name'], 'Coder')
        self.assertEqual(restarted.turn, original['turn_sequence'] + 1)
        self.assertIn('Read the shared artifacts', restarted.runner.state.prompt)

    def test_restore_keeps_limit_and_requires_explicit_extension(self):
        original = self.paused_run(limit=1)
        restarted = RunService(Runner, self.history)
        restarted.restore(original['id'])
        with self.assertRaises(ValueError): restarted.control(original['id'], 'continue')
        restarted.control(original['id'], 'extend', additional_turns=3)
        self.assertEqual(restarted.run['max_turns'], 4)
        self.assertEqual(restarted.run['agent']['name'], 'Coder')

    def test_restore_rejects_busy_missing_artifacts_and_interrupted_runs(self):
        original = self.paused_run()
        with self.assertRaises(RuntimeError): self.service.restore(original['id'])
        restarted = RunService(Runner, self.history)
        (self.workspace / original['artifact_dir'] / 'PLANNER.md').unlink()
        with self.assertRaises(OSError): restarted.restore(original['id'])
        self.assertIsNone(restarted.run)
        original['status'] = 'running'
        restarted.history.save(original)
        with self.assertRaises(ValueError): restarted.restore(original['id'])

    def test_restore_legacy_sequence_and_extended_budget(self):
        original = self.paused_run()
        original.pop('turn_sequence')
        original['max_turns'] = 150
        evidence = self.workspace / original['artifact_dir'] / 'evidence' / 'turn-0042'
        evidence.mkdir(parents=True)
        self.service.history.save(original)
        restarted = RunService(Runner, self.history)
        restarted.restore(original['id'])
        restarted.control(original['id'], 'continue')
        self.assertEqual(restarted.turn, 43)
        self.assertEqual(restarted.run['max_turns'], 150)

    def test_restore_rejects_invalid_handoff_without_changing_checkpoint(self):
        original = self.paused_run()
        original['next_index'] = 99
        self.service.history.save(original)
        path = self.history / (original['id'] + '.json')
        before = path.read_bytes()
        restarted = RunService(Runner, self.history)
        with self.assertRaises(ValueError): restarted.restore(original['id'])
        self.assertIsNone(restarted.run)
        self.assertEqual(path.read_bytes(), before)

    def test_restart_preserves_history_and_download(self):
        run = self.start()
        path = self.finish()
        restarted = RunService(Runner, self.history)
        self.assertEqual(restarted.list_runs()[0]['id'], run['id'])
        self.assertEqual(restarted.saved_run(run['id'])['status'], 'completed')
        self.assertEqual(restarted.download(run['id'], path.name), path.read_bytes())
        self.assertIsNone(restarted.run)

    def test_followup_uses_new_team_and_new_workspace(self):
        parent = self.start()
        path = self.finish()
        original = path.read_bytes()
        destination = self.root / 'implementation'
        destination.mkdir()
        restarted = RunService(Runner, self.history)
        child = restarted.start(dict(goal='Plan and implement the findings', cwd=str(destination),
            sandbox='workspace-write', agents=[role('Planner'), role('Coder','end')], source_run_id=parent['id']))['run']
        self.assertNotEqual(child['id'],parent['id'])
        self.assertEqual(child['source_run_id'],parent['id'])
        self.assertEqual(child['agent']['name'],'Planner')
        inputs = destination / child['artifact_dir']
        self.assertEqual((inputs/'source'/path.name).read_bytes(),original)
        context = json.loads((inputs/'source-context.json').read_text())
        self.assertEqual(context['goal'],'Literature review')
        self.assertEqual(context['final_response'],'Review complete.\n\n')
        self.assertIn('source-context.json',restarted.runner.state.prompt)
        self.assertIn('Plan and implement the findings',restarted.runner.state.prompt)
        (inputs/'source'/path.name).write_text('Changed copy')
        self.assertEqual(path.read_bytes(),original)

    def test_missing_source_file_prevents_launch(self):
        parent = self.start()
        path = self.finish()
        path.unlink()
        count = len(Runner.instances)
        with self.assertRaises(OSError): self.start(source_run_id=parent['id'])
        self.assertEqual(len(Runner.instances),count)
        self.assertEqual(self.service.run['id'],parent['id'])

    def test_followup_carries_nested_evidence_and_hash_manifest(self):
        import hashlib
        parent = self.start()
        self.finish()
        evidence = self.workspace / parent['artifact_dir'] / 'review-evidence' / 'checks.json'
        evidence.parent.mkdir()
        evidence.write_text('{"exit_code":0}')
        child = self.start(source_run_id=parent['id'])
        root = self.workspace / child['artifact_dir']
        copied = root / 'source/review-evidence/checks.json'
        self.assertEqual(copied.read_bytes(), evidence.read_bytes())
        manifest = json.loads((root / 'source-evidence-manifest.json').read_text())
        self.assertEqual(manifest['review-evidence/checks.json'], hashlib.sha256(copied.read_bytes()).hexdigest())

    def test_interrupted_runs_and_legacy_snapshots(self):
        run = self.start()
        self.assertEqual(self.service.list_runs()[0]['status'],'running')
        restarted = RunService(Runner,self.history)
        self.assertEqual(restarted.list_runs()[0]['status'],'interrupted')
        self.assertEqual(restarted.saved_run(run['id'])['status'],'interrupted')
        legacy = {**run, 'id':'a'*32,'status':'completed'}
        (self.history.parent / ('web-run-'+legacy['id']+'.json')).write_text(json.dumps({'run':legacy}))
        imported = RunHistory(self.history)
        self.assertEqual(imported.get(legacy['id'])['status'],'completed')
        self.assertEqual(len(imported.list()),2)

    def test_invalid_ids_and_artifact_escape_are_rejected(self):
        run = self.start()
        self.finish()
        for value in ['../outside','',123]:
            with self.assertRaises(ValueError): self.service.saved_run(value)
        self.service.run['artifact_dir'] = '../'
        with self.assertRaises((ValueError,OSError)):
            self.service.download(run['id'],'RESEARCHER.md')

    def test_single_response_is_available_as_followup_context(self):
        parent=self.service.start(dict(goal='Think about research',cwd=str(self.workspace),agent=role('Thinker')))['run']
        self.service.runner.event({'type':'item.completed','item':{'type':'agent_message','text':'Promising idea'}})
        self.service.runner.done(0,'')
        child=self.start(source_run_id=parent['id'])
        context=json.loads((self.workspace / child['artifact_dir'] / 'source-context.json').read_text())
        self.assertIn('Promising idea',context['final_response'])
        self.assertEqual(context['files'],[])


if __name__ == '__main__':
    unittest.main()
