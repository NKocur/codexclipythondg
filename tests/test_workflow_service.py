import tempfile
import unittest
import json
import sys
import time
from pathlib import Path

from handoff_protocol import HANDOFF_DONE, HANDOFF_END, handoff_marker, APPROVED
from workflow_service import RunService
from powershell_codex_viewer import CodexExecRunner


class Runner:
    instances = []

    def __init__(self, state, on_event, on_log, on_done):
        self.state, self.event, self.done = state, on_event, on_done
        self.instances.append(self)

    def start(self):
        pass

    def stop(self):
        self.done(1, 'Cancelled')


def role(name, route='next', allowed=None):
    return dict(id=name.lower(), name=name, prompt='Do your job.', provider='Codex',
                artifact=name.upper()+'.md', route=route, allowed=allowed or [])


def handoff(name):
    return f'{handoff_marker(name)}\nRead the shared artifacts and continue the assigned work.\n{HANDOFF_END}\n{HANDOFF_DONE}'


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = RunService(Runner)
        self.roles = [role('Planner'), role('Reviewer','choose',['planner','Implementer'.lower()]),
                      role('Implementer'), role('Tester'), role('Archivist','end')]
        self.data = dict(goal='Build a feature', cwd=self.temp.name, sandbox='workspace-write',
                         agents=self.roles, auto_advance=True)

    def tearDown(self):
        self.temp.cleanup()

    def start(self, **overrides):
        return self.service.start({**self.data, **overrides})['run']

    def complete(self, text='Finished the work.', write=True):
        run = self.service.snapshot()['run']
        if write:
            path = Path(run['cwd']) / run['artifact_dir'] / run['agent']['artifact']
            path.write_text(f"# {run['agent']['name']}\nReal file from turn {len(run['turns'])+1}",encoding='utf-8')
        runner=self.service.runner
        runner.event({'type':'item.completed','item':{'type':'agent_message','text':text}})
        runner.done(0,'')
        return self.service.snapshot()['run']

    def test_required_reports_and_downloads(self):
        self.roles[0]['deliverables'] = ['REPORT.html', 'REPORT.pdf']
        run = self.start()
        directory = Path(run['cwd']) / run['artifact_dir']
        self.assertIn('REPORT.pdf', self.service.runner.state.prompt)
        (directory / 'REPORT.html').write_text('<!doctype html><html><body>Report</body></html>')
        pdf = b'%PDF-1.4\nfixture\n%%EOF'
        (directory / 'REPORT.pdf').write_bytes(pdf)
        result = self.complete()
        self.assertEqual(result['agent']['name'], 'Reviewer')
        self.assertEqual({a['name'] for a in result['artifacts']}, {'PLANNER.md','REPORT.html','REPORT.pdf'})
        self.assertEqual(self.service.download(run['id'], 'REPORT.pdf'), pdf)
        for run_id, name in [(run['id'], '../REPORT.pdf'), ('old-run', 'REPORT.pdf')]:
            with self.assertRaises(ValueError): self.service.download(run_id, name)

    def test_missing_and_invalid_reports_stop_handoff(self):
        for content in [None, b'', b'not actually a PDF']:
            with self.subTest(content=content):
                self.roles[0]['deliverables'] = ['REPORT.pdf']
                run = self.start()
                if content is not None:
                    (Path(run['cwd']) / run['artifact_dir'] / 'REPORT.pdf').write_bytes(content)
                result = self.complete()
                self.assertEqual(result['status'], 'failed')
                self.assertEqual(result['current'], 0)
                self.assertIn('required output', result['error'])

    def test_deliverable_validation_and_single_agent(self):
        for filenames in [['../bad.pdf'], ['bad.exe'], ['same.pdf','SAME.pdf'], 'REPORT.pdf']:
            self.roles[0]['deliverables'] = filenames
            with self.assertRaises(ValueError): self.start()
        agent = role('Reporter', 'end')
        agent['deliverables'] = ['REPORT.html']
        data = dict(goal='Report', cwd=self.temp.name, agent=agent)
        with self.assertRaisesRegex(ValueError, 'Can edit files'): self.service.start(data)
        run = self.service.start({**data, 'sandbox':'workspace-write'})['run']
        (Path(run['cwd']) / run['artifact_dir'] / 'REPORT.html').write_text('<html>Report</html>')
        self.service.runner.done(0, '')
        self.assertEqual(self.service.run['status'], 'completed')
        self.assertEqual(self.service.run['artifacts'][0]['name'], 'REPORT.html')

    def test_full_team_shared_context_and_artifacts(self):
        first=self.start()
        self.assertIn(first['artifact_dir'],self.service.runner.state.prompt)
        self.complete()
        self.assertEqual(self.service.run['agent']['name'],'Reviewer')
        self.assertIn('PLANNER.md',self.service.runner.state.prompt)
        self.complete(handoff('Implementer'))
        self.assertIn('Read the shared artifacts',self.service.runner.state.prompt)
        self.complete();self.complete();result=self.complete()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['turns']),5)
        self.assertEqual(len(result['artifacts']),5)
        self.assertIn('Real file',result['artifacts'][0]['body'])
        self.assertTrue(all(a['status']=='done' for a in result['agents']))
        self.assertNotIn('Do not delegate or hand off.',self.service.runner.state.prompt)

    def test_send_back_then_forward(self):
        self.start();self.complete();self.complete(handoff('Planner'))
        self.assertEqual(self.service.run['current'],0)
        self.complete();self.complete(handoff('Implementer'))
        self.complete();self.complete();result=self.complete()
        self.assertEqual(result['status'],'completed')
        self.assertEqual(len(result['turns']),7)
        self.assertEqual(next(a for a in result['artifacts'] if a['agent']=='Planner')['version'],2)
        self.assertIn('1>0',result['trail'])

    def test_manual_continue_and_pause_after_current(self):
        run=self.start(auto_advance=False)
        result=self.complete()
        self.assertEqual(result['status'],'paused')
        self.assertEqual(result['next_index'],1)
        with self.assertRaises(RuntimeError): self.start()
        self.service.control(run['id'],'continue',True)
        self.assertEqual(self.service.run['agent']['name'],'Reviewer')
        self.service.control(run['id'],'pause')
        self.assertEqual(self.service.run['status'],'running')
        result=self.complete(handoff('Implementer'))
        self.assertEqual(result['status'],'paused')
        self.service.stop(run['id'])
        self.assertEqual(self.service.run['status'],'stopped')
        self.assertEqual(len(self.service.run['turns']),2)

    def test_stale_turn_callbacks_do_not_complete_next_agent(self):
        self.start();old=self.service.runner;self.complete()
        old.event({'type':'turn.failed','error':'late event'})
        old.done(1,'late failure')
        self.assertEqual(self.service.run['agent']['name'],'Reviewer')
        self.assertEqual(self.service.run['status'],'running')
        self.assertEqual(len(self.service.run['turns']),1)

    def test_limit_waits_for_explicit_extension_and_can_stop(self):
        self.start(max_turns=2);self.complete();result=self.complete(handoff('Planner'))
        self.assertEqual(result['status'],'paused')
        self.assertEqual(result['pause_reason'],'turn_limit')
        self.assertEqual(result['next_index'],0)
        self.assertIsNone(result['finished'])
        self.assertEqual(len(result['turns']),2)
        count=len(Runner.instances)
        for action, extra in [('continue',None),('extend',None),('extend',0),('extend',True),('extend',101)]:
            with self.assertRaises(ValueError): self.service.control(result['id'],action,additional_turns=extra)
        self.assertEqual(len(Runner.instances),count)
        self.service.control(result['id'],'extend',additional_turns=2)
        self.assertEqual(self.service.run['max_turns'],4)
        self.assertEqual(self.service.run['agent']['name'],'Planner')
        self.assertIsNone(self.service.run['pause_reason'])
        self.complete();result=self.complete(handoff('Planner'))
        self.assertEqual(result['status'],'paused')
        count=len(Runner.instances)
        self.service.stop(result['id'])
        self.assertEqual(self.service.run['status'],'stopped')
        self.assertEqual(len(Runner.instances),count)

    def test_manual_limit_pause_and_completion_at_limit(self):
        run=self.start(max_turns=1,auto_advance=False)
        self.complete()
        self.assertEqual(self.service.run['pause_reason'],'turn_limit')
        self.service.stop(run['id'])
        self.start(max_turns=1,agents=[role('Finisher','end')])
        self.assertEqual(self.complete()['status'],'completed')

    def test_invalid_handoffs_do_not_advance(self):
        for text in (handoff('Tester'), handoff('Unknown'), 'Missing marker',
                     handoff('Implementer')+'\n'+handoff('Unknown'),
                     '[[HANDOFF_TO_PLANNER]]too short[[END_HANDOFF]][[COMMANDDOCK_DONE]]',
                     APPROVED+' '+HANDOFF_DONE):
            with self.subTest(text=text):
                self.start();self.complete();result=self.complete(text)
                self.assertEqual(result['status'],'failed')
                self.assertEqual(len(result['turns']),2)

    def test_missing_artifact_is_a_failure_not_a_success(self):
        self.start();result=self.complete(write=False)
        self.assertEqual(result['status'],'failed')
        self.assertEqual(len(result['turns']),1)
        self.assertIn('PLANNER.md',result['error'])

    def test_final_reviewer_can_approve_and_end_early_skips_roles(self):
        self.start(agents=[role('Builder'),role('Final','choose',['builder'])])
        self.complete();result=self.complete(APPROVED+' '+HANDOFF_DONE)
        self.assertEqual(result['status'],'completed')
        self.start(agents=[role('Only','end'),role('Skipped')])
        result=self.complete()
        self.assertEqual(result['agents'][1]['status'],'skipped')

    def test_command_output_cannot_trigger_handoff(self):
        self.start();self.complete()
        self.service.runner.event({'type':'item.completed','item':{'type':'command_execution','aggregated_output':handoff('Planner')}})
        result=self.complete(handoff('Implementer'))
        self.assertEqual(result['current'],2)

    def test_validation_and_snapshot(self):
        for override in ({'sandbox':'read-only'},{'max_turns':0},
                         {'agents':[role('Same'),role('Same')]},
                         {'agents':[role('A','choose',['missing'])]},
                         {'agents':[{**role('A'),'artifact':'../escape.md'}]},
                         {'agents':[{**role('A'),'provider':'Claude'}]}):
            with self.subTest(override=override), self.assertRaises(ValueError): self.start(**override)
        run=self.start();self.roles[0]['name']='Changed'
        self.assertEqual(self.service.snapshot()['run']['agent']['name'],'Planner')
        self.assertNotEqual(self.service.snapshot()['run']['agents'][0]['name'],self.roles[0]['name'])
        self.assertTrue((Path(self.temp.name)/run['artifact_dir']).is_dir())

    def test_real_child_process_team_writes_artifacts_and_hands_back(self):
        class FixtureRunner(CodexExecRunner):
            def _build_command(self):
                script = r'''
import sys,json,re
from pathlib import Path
prompt=sys.stdin.read()
name=re.search(r'You are the (.+?) agent\.',prompt).group(1)
folder=Path(re.search(r'Shared artifact directory for THIS RUN: (.+)',prompt).group(1))
path=folder/(name.upper()+'.md')
prior=path.read_text() if path.exists() else ''
path.write_text('# '+name+'\nUpdated\n'+prior)
if name=='Reviewer':
    target='PLANNER' if not prior else 'IMPLEMENTER'
    text='[[HANDOFF_TO_'+target+']]\nRead the shared files and complete the next step.\n[[END_HANDOFF]]\n[[COMMANDDOCK_DONE]]'
else:
    text='Work completed.'
print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':text}}),flush=True)
print(json.dumps({'type':'turn.completed'}),flush=True)
'''
                return [sys.executable,'-u','-c',script]
        self.service=RunService(FixtureRunner)
        self.start()
        deadline=time.monotonic()+20
        while self.service.snapshot()['run']['status']=='running' and time.monotonic()<deadline:
            time.sleep(.02)
        run=self.service.snapshot()['run']
        if run['status']=='running':
            self.service.stop(run['id'])
        self.assertEqual(run['status'],'completed',run['error'])
        self.assertEqual(len(run['turns']),7)
        self.assertEqual(len(run['artifacts']),5)
        self.assertEqual(next(a for a in run['artifacts'] if a['agent']=='Planner')['version'],2)


if __name__=='__main__': unittest.main()
