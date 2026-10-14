import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from improvement_service import ImprovementService, git
from workflow_service import RunService
from test_workflow_service import Runner, role


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source = root / 'source'
        self.source.mkdir()
        (self.source / 'web_workbench.py').write_text('server = 1\n')
        (self.source / 'workflow_service.py').write_text('workflow = 1\n')
        git(self.source, 'init', '-q')
        self.main = RunService(Runner, history_dir=root / 'history')
        self.main.start(dict(goal='Review this project', cwd=str(self.source), agent=role('Inspector'), sandbox='read-only'))
        self.main_runner = self.main.runner
        self.service = ImprovementService(self.main, root / 'improvements', self.source, Runner)

    def start(self, mode='review', **extra):
        return self.service.start(dict(run_id=self.main.run['id'], message='Improve coordination', mode=mode, **extra))

    def prepare(self, result='passed'):
        self.start('prepare')
        candidate = Path(self.service.runner.state.cwd)
        (candidate / 'workflow_service.py').write_text('workflow = 2\n')
        (candidate / 'new_test.py').write_text('assert True\n')
        (candidate / 'IMPROVEMENT_REPORT.json').write_text(json.dumps(dict(summary='Improved routing',
            tests=[dict(command='python new_test.py', result=result, exit_code=0 if result=='passed' else 1, evidence='One assertion passed')],
            limitations=['Mocked provider only'])))
        self.service.runner.event({'type':'item.completed','item':{'type':'agent_message','text':'Prepared routing changes and tested them.'}})
        self.service.runner.done(0, '')
        self.assertEqual(self.service.state['status'], 'ready', self.service.state.get('error'))
        return self.service.state['proposal']

    def test_review_is_read_only_and_does_not_take_over_main_runner(self):
        self.start()
        self.assertEqual(self.service.runner.state.sandbox, 'read-only')
        self.assertEqual(self.service.runner.state.cwd, str(self.source.resolve()))
        self.service.runner.event({'type':'item.completed','item':{'type':'agent_message','text':'The reviewer is waiting for evidence.'}})
        self.service.runner.done(0, '')
        self.assertEqual(self.service.state['messages'][-1]['text'], 'The reviewer is waiting for evidence.')
        self.assertIs(self.main.runner, self.main_runner)
        self.assertEqual(self.main.run['status'], 'running')
        self.assertEqual((self.source/'workflow_service.py').read_text(), 'workflow = 1\n')
        loaded = ImprovementService(self.main, self.service.directory, self.source, Runner)
        self.assertEqual(loaded.state['messages'], self.service.state['messages'])

    def test_prepare_captures_dirty_source_and_only_applies_after_run_finishes(self):
        (self.source / 'web_workbench.py').write_text('existing_uncommitted_change = True\n')
        original_status = git(self.source, 'status', '--porcelain')
        proposal = self.prepare()
        self.assertNotEqual(Path(proposal['candidate']), self.source)
        self.assertIn('+workflow = 2', proposal['diff'])
        self.assertIn('new_test.py', proposal['diff'])
        self.assertNotIn('IMPROVEMENT_REPORT.json', proposal['diff'])
        self.assertEqual(git(self.source, 'status', '--porcelain'), original_status)
        self.assertNotIn('files', self.service.snapshot()['chat']['proposal'])
        with self.assertRaisesRegex(RuntimeError, 'active workflow'):
            self.service.apply(proposal['id'])
        self.main_runner.done(0, '')
        self.service.apply(proposal['id'])
        self.assertEqual((self.source / 'workflow_service.py').read_text(), 'workflow = 2\n')
        self.assertEqual((self.source / 'web_workbench.py').read_text(), 'existing_uncommitted_change = True\n')
        self.assertTrue((self.source / 'new_test.py').exists())
        with self.assertRaises(ValueError):
            self.service.apply(proposal['id'])

    def test_stale_source_or_candidate_refuses_entire_patch(self):
        proposal = self.prepare()
        self.main_runner.done(0, '')
        (self.source / 'workflow_service.py').write_text('user made another edit\n')
        with self.assertRaisesRegex(ValueError, 'changed since preparation'):
            self.service.apply(proposal['id'])
        self.assertFalse((self.source / 'new_test.py').exists())
        (self.source / 'workflow_service.py').write_text('workflow = 1\n')
        (Path(proposal['candidate']) / 'new_test.py').write_text('different from reviewed patch')
        with self.assertRaisesRegex(ValueError, 'changed since preparation'):
            self.service.apply(proposal['id'])

    def test_skipped_tests_cannot_apply(self):
        proposal = self.prepare('skipped')
        self.main_runner.done(0, '')
        self.assertFalse(proposal['can_apply'])
        with self.assertRaises(ValueError):
            self.service.apply(proposal['id'])

    def test_stop_only_stops_improvement_request_and_ignores_late_events(self):
        self.start()
        runner = self.service.runner
        self.service.stop()
        for _ in range(100):
            if self.service.state['status'] == 'stopped':
                break
            time.sleep(.01)
        self.assertEqual(self.service.state['status'], 'stopped')
        self.assertEqual(self.main.run['status'], 'running')
        self.start()
        runner.event({'type':'item.completed','item':{'type':'agent_message','text':'obsolete response'}})
        self.assertNotIn('obsolete response', str(self.service.state['messages']))
        self.service.runner.done(0, '')

    def test_missing_report_does_not_offer_apply(self):
        self.start('prepare')
        self.service.runner.done(0, '')
        self.assertEqual(self.service.state['status'], 'failed')
        self.assertIsNone(self.service.state['proposal'])

    def test_prepare_revision_includes_previous_diff_as_context(self):
        proposal = self.prepare()
        self.start('prepare')
        contexts = list(self.service.directory.glob('run-*.json'))
        previous = [json.loads(p.read_text())['previous_improvement_proposal'] for p in contexts]
        self.assertTrue(any(p and p['id'] == proposal['id'] for p in previous))
        self.service.runner.done(1, 'Test finished')

    def test_apply_rolls_back_on_write_error(self):
        proposal = self.prepare()
        self.main_runner.done(0, '')
        original = self.service._write
        calls = []
        def fail_once(path, body):
            if path.is_relative_to(self.source):
                calls.append(path)
                if len(calls) == 2:
                    raise OSError('Simulated write failure')
            original(path, body)
        with patch.object(self.service, '_write', side_effect=fail_once):
            with self.assertRaises(OSError):
                self.service.apply(proposal['id'])
        self.assertEqual((self.source / 'workflow_service.py').read_text(), 'workflow = 1\n')
        self.assertFalse((self.source / 'new_test.py').exists())
        self.assertFalse(proposal['applied'])

    def test_revert_preserves_unrelated_work_and_refuses_newer_edits(self):
        proposal = self.prepare()
        self.main_runner.done(0, '')
        self.service.apply(proposal['id'])
        (self.source / 'web_workbench.py').write_text('unrelated later work')
        (self.source / 'workflow_service.py').write_text('newer workflow change')
        with self.assertRaisesRegex(ValueError, 'newer work'):
            self.service.revert(proposal['id'])
        self.assertTrue((self.source / 'new_test.py').exists())
        (self.source / 'workflow_service.py').write_text('workflow = 2\n')
        self.service.revert(proposal['id'])
        self.assertEqual((self.source / 'workflow_service.py').read_text(), 'workflow = 1\n')
        self.assertEqual((self.source / 'web_workbench.py').read_text(), 'unrelated later work')
        self.assertFalse((self.source / 'new_test.py').exists())
        self.assertTrue(proposal['reverted'])

    def test_prepare_includes_deletions_and_revert_restores_them(self):
        self.start('prepare')
        candidate = Path(self.service.runner.state.cwd)
        (candidate / 'web_workbench.py').unlink()
        (candidate / 'IMPROVEMENT_REPORT.json').write_text(json.dumps(dict(summary='Remove obsolete file',
            tests=[dict(command='test removal', result='passed', exit_code=0, evidence='No remaining references')], limitations=[])))
        self.service.runner.done(0, '')
        proposal = self.service.state['proposal']
        self.assertIn('deleted file', proposal['diff'])
        self.main_runner.done(0, '')
        self.service.apply(proposal['id'])
        self.assertFalse((self.source / 'web_workbench.py').exists())
        self.service.revert(proposal['id'])
        self.assertEqual((self.source / 'web_workbench.py').read_text(), 'server = 1\n')


if __name__ == '__main__':
    unittest.main()
