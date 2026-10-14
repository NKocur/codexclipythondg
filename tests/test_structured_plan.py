import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from workflow_service import RunService
from plan_workflow import apply_update, initial_state
from test_workflow_service import Runner, handoff


class StructuredPlanTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.preset = json.loads(subprocess.check_output(
            ['node', 'tests/test_markdown_plan_preset.cjs', '--v2', '--json'], text=True,
            cwd=Path(__file__).resolve().parents[1]))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'PLAN.md').write_text('# Plan\n1. Feature\n2. Next feature', encoding='utf-8')
        (self.root / 'feature.py').write_text('version = 1', encoding='utf-8')
        self.service = RunService(Runner, history_dir=self.root / 'history')
        self.service.start(dict(goal='Implement PLAN.md', cwd=str(self.root), sandbox='workspace-write',
                                agents=self.preset['roles'], max_turns=100))

    def finish(self, update, target=None):
        run = self.service.run
        directory = self.root / run['artifact_dir']
        (directory / run['agent']['artifact']).write_text('Evidence from ' + run['agent']['name'], encoding='utf-8')
        (directory / 'PLAN_UPDATE.json').write_text(json.dumps({'turn': self.service.turn, **update}), encoding='utf-8')
        self.service.runner.event({'type': 'item.completed', 'item': {'type': 'agent_message',
            'text': handoff(target) if target else 'Finished'}})
        self.service.runner.done(0, '')
        return self.service.run

    def initialize(self, steps=None):
        return self.finish(dict(source='PLAN.md', steps=steps or [dict(id='S1', title='Feature', depends_on=[])],
            action='select', batch='B1', title='Feature', steps_in_batch=['S1'], acceptance=['Feature works']), 'Batch Implementer')

    def implement(self, blockers=None):
        return self.finish(dict(batch='B1', files=['feature.py'], blockers=blockers or []), 'Batch Reviewer')

    def verdict(self, verdict='passed', revision=1, issues=None, result=None):
        return dict(batch='B1', revision=revision, verdict=verdict, issues=issues or [],
                    checks=[dict(criterion='Feature works', result=result or verdict, evidence='Ran fixture: exit 0')])

    def review_and_test(self, revision=1):
        self.finish(self.verdict(revision=revision), 'Batch Tester')
        return self.finish(self.verdict(revision=revision), 'Plan Coordinator')

    def test_full_cycle_archives_and_persists_verified_completion(self):
        self.initialize()
        self.assertIn('"turn":2', self.service.runner.state.prompt)
        self.implement()
        self.review_and_test()
        self.assertEqual(self.service.run['plan']['steps'][0]['status'], 'verified')
        self.finish(dict(action='report'), 'Plan Reporter')
        run = self.finish(dict(outcome='complete'))
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['plan']['outcome'], 'complete')
        report = self.root / run['plan']['batches'][0]['review']['report']
        self.assertEqual(report.read_text(), 'Evidence from Batch Reviewer')
        saved = self.service.history.get(run['id'])
        self.assertEqual(saved['plan']['outcome'], 'complete')
        self.assertEqual(len(saved['plan']['history']), 6)

    def test_premature_complete_pauses_without_claiming_success(self):
        self.initialize()
        self.implement()
        self.finish(self.verdict('blocked', issues=['no-vm'], result='skipped'), 'Plan Coordinator')
        self.finish(dict(action='report'), 'Plan Reporter')
        run = self.finish(dict(outcome='complete'))
        self.assertEqual(run['status'], 'paused')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.assertNotEqual(run['plan']['outcome'], 'complete')

    def test_skip_cannot_pass_and_stale_update_cannot_replay(self):
        self.initialize()
        self.implement()
        run = self.finish(self.verdict(result='skipped'), 'Batch Tester')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.assertIn('skipped', run['plan']['notice'])
        self.service.control(run['id'], 'continue')
        run = self.finish(dict(turn=1, action='select', batch='B1'), 'Batch Reviewer')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.assertIn('turn number', run['plan']['notice'])

    def test_file_change_invalidates_review_and_requires_new_revision(self):
        self.initialize()
        self.implement()
        self.finish(self.verdict(), 'Batch Tester')
        (self.root / 'feature.py').write_text('version = 2')
        run = self.finish(self.verdict(), 'Plan Coordinator')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.assertIsNone(run['plan']['batches'][0]['review'])
        self.assertEqual(run['plan']['steps'][0]['status'], 'needs_changes')
        self.service.control(run['id'], 'continue', note='Review the new feature version.')
        self.assertIn('Review the new feature version.', self.service.runner.state.prompt)
        self.finish(dict(action='select', batch='B1'), 'Batch Implementer')
        self.implement()
        self.assertEqual(run['plan']['batches'][0]['revision'], 2)
        self.review_and_test(2)
        self.assertEqual(run['plan']['steps'][0]['status'], 'verified')

    def test_direct_diagnostic_testing_does_not_bypass_review(self):
        self.initialize()
        self.implement([dict(id='env', need='Native image', resume='Supply image')])
        self.assertEqual(self.service.run['agent']['id'], 'plan-coordinator')
        self.finish(dict(action='select', batch='B1'), 'Batch Tester')
        run = self.finish(self.verdict(), 'Plan Coordinator')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.assertIn('independent review', run['plan']['notice'])

    def test_repeated_issue_routes_to_coordinator_then_requires_changed_strategy(self):
        self.initialize()
        for revision in range(1, 4):
            self.implement()
            run = self.finish(self.verdict('failed', revision, ['same-bug']), 'Batch Implementer')
        self.assertEqual(run['agent']['id'], 'plan-coordinator')
        self.assertTrue(run['plan']['batches'][0]['attention'])
        run = self.finish(dict(action='select', batch='B1'), 'Batch Implementer')
        self.assertEqual(run['pause_reason'], 'plan_validation')

    def test_wait_resume_and_independent_work_with_blocker_visible(self):
        self.initialize([dict(id='S1', title='Native', depends_on=[]), dict(id='S2', title='Docs', depends_on=[])])
        blocker = dict(id='image', need='Provide native image', resume='Supply its path')
        self.implement([blocker])
        run = self.finish(dict(action='select', batch='B2', title='Docs', steps_in_batch=['S2'], acceptance=['Docs correct']), 'Batch Implementer')
        self.assertEqual(run['plan']['batches'][0]['blockers'], [blocker])
        self.finish(dict(batch='B2', files=[], blockers=[blocker]), 'Plan Coordinator')
        run = self.finish(dict(action='wait', blockers=[blocker]), 'Plan Reporter')
        self.assertEqual(run['status'], 'paused')
        self.assertEqual(run['pause_reason'], 'needs_input')
        self.service.control(run['id'], 'continue', note='Image is at images/native.img')
        self.assertIn('images/native.img', self.service.runner.state.prompt)
        self.assertEqual(run['agent']['id'], 'plan-coordinator')

    def test_dependencies_source_integrity_and_transactional_rejection(self):
        self.initialize([dict(id='S1', title='First', depends_on=[]), dict(id='S2', title='Second', depends_on=['S1'])])
        self.implement([dict(id='env', need='Image', resume='Supply image')])
        before = copy.deepcopy(self.service.run['plan'])
        run = self.finish(dict(action='select', batch='B2', title='Second', steps_in_batch=['S2'], acceptance=['Works']), 'Batch Implementer')
        self.assertEqual(len(run['plan']['batches']), len(before['batches']))
        self.assertIn('dependencies', run['plan']['notice'])
        self.service.control(run['id'], 'continue')
        (self.root / 'PLAN.md').write_text('Changed requirements')
        run = self.finish(dict(action='select', batch='B1'), 'Batch Implementer')
        self.assertIn('source plan changed', run['plan']['notice'])

    def test_blocked_batch_can_split_independent_steps_without_inheriting_approval(self):
        state, _ = apply_update(initial_state(), dict(turn=1, source='PLAN.md',
            steps=[dict(id='S1', title='Native'), dict(id='S2', title='Docs')], action='select',
            batch='B1', title='Combined', steps_in_batch=['S1', 'S2'], acceptance=['Both work']),
            'plan-coordinator', 1, self.root, 'report.md')
        state, _ = apply_update(state, dict(turn=2, batch='B1', files=[],
            blockers=[dict(id='env', need='Image', resume='Supply image')]), 'batch-implementer', 2, self.root, 'report.md')
        state, _ = apply_update(state, dict(turn=3, action='select', batch='B2', title='Independent docs',
            split_from='B1', steps_in_batch=['S2'], acceptance=['Docs correct']), 'plan-coordinator', 3, self.root, 'report.md')
        self.assertEqual(state['batches'][0]['steps'], ['S1'])
        self.assertEqual(state['batches'][1]['steps'], ['S2'])
        self.assertIsNone(state['batches'][1]['review'])

    def test_paths_cannot_escape_and_step_inventory_cannot_be_replaced(self):
        self.initialize()
        run = self.finish(dict(batch='B1', files=['../outside.py']), 'Batch Reviewer')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.service.control(run['id'], 'continue')
        run = self.finish(dict(action='select', batch='B1', steps=[]), 'Batch Implementer')
        self.assertIn('immutable', run['plan']['notice'])

    def test_two_dependency_ordered_batches_complete_and_followup_revalidates(self):
        self.initialize([dict(id='S1', title='First', depends_on=[]), dict(id='S2', title='Next', depends_on=['S1'])])
        self.implement()
        self.review_and_test()
        self.finish(dict(action='select', batch='B2', title='Next feature', steps_in_batch=['S2'], acceptance=['Next works']), 'Batch Implementer')
        (self.root / 'next.py').write_text('next_feature = True')
        self.finish(dict(batch='B2', files=['next.py']), 'Batch Reviewer')
        verdict = dict(batch='B2', revision=1, verdict='passed', checks=[
            dict(criterion='Next works', result='passed', evidence='Ran next feature acceptance: exit 0')])
        self.finish(verdict, 'Batch Tester')
        self.finish(verdict, 'Plan Coordinator')
        self.finish(dict(action='report'), 'Plan Reporter')
        source = self.finish(dict(outcome='complete'))
        self.assertEqual([s['status'] for s in source['plan']['steps']], ['verified', 'verified'])
        self.service.start(dict(goal='Continue PLAN.md', cwd=str(self.root), sandbox='workspace-write',
                                agents=self.preset['roles'], source_run_id=source['id']))
        context = json.loads((self.root / self.service.run['artifact_dir'] / 'source-context.json').read_text())
        self.assertEqual(context['plan']['outcome'], 'complete')
        self.assertEqual(self.service.run['plan']['steps'], [])

    def test_changed_verified_dependency_invalidates_downstream_acceptance(self):
        self.initialize([dict(id='S1', title='First', depends_on=[]), dict(id='S2', title='Next', depends_on=['S1'])])
        self.implement()
        self.review_and_test()
        self.finish(dict(action='select', batch='B2', title='Next', steps_in_batch=['S2'], acceptance=['Next works']), 'Batch Implementer')
        self.finish(dict(batch='B2', files=[]), 'Batch Reviewer')
        verdict = dict(batch='B2', revision=1, verdict='passed', checks=[dict(criterion='Next works', result='passed', evidence='Observed required behavior')])
        self.finish(verdict, 'Batch Tester')
        self.finish(verdict, 'Plan Coordinator')
        (self.root / 'feature.py').write_text('version = 3')
        self.finish(dict(action='report'), 'Plan Reporter')
        self.assertEqual([s['status'] for s in self.service.run['plan']['steps']], ['needs_changes', 'needs_changes'])
        run = self.finish(dict(outcome='partial'))
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['plan']['outcome'], 'partial')

    def test_invalid_inventory_cycles_and_changed_role_layout_rejected(self):
        run = self.finish(dict(source='PLAN.md', steps=[dict(id='S1', title='First', depends_on=['S2']),
            dict(id='S2', title='Second', depends_on=['S1'])], action='report'), 'Plan Reporter')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.assertIn('cycle', run['plan']['notice'])
        self.service.stop(run['id'])
        with self.assertRaisesRegex(ValueError, 'five original role IDs'):
            self.service.start(dict(goal='Plan', cwd=str(self.root), sandbox='workspace-write', agents=self.preset['roles'][:-1]))

    def test_missing_structured_update_pauses_and_stop_does_not_claim_complete(self):
        run = self.service.run
        directory = self.root / run['artifact_dir']
        (directory / 'PLAN_PROGRESS.md').write_text('Missing JSON update')
        self.service.runner.event({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': handoff('Batch Implementer')}})
        self.service.runner.done(0, '')
        self.assertEqual(run['pause_reason'], 'plan_validation')
        self.service.stop(run['id'])
        self.assertEqual(run['plan']['outcome'], 'partial')


if __name__ == '__main__':
    unittest.main()
