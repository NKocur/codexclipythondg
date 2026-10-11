import json
import tempfile
import unittest
from pathlib import Path

from run_history import RunHistory
from workflow_service import RunService
from test_workflow_service import Runner, role


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
