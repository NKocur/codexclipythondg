import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from workflow_service import RunService
from test_workflow_service import Runner, handoff


class PlanWorkflowTests(unittest.TestCase):
    def test_preset_repairs_and_multiple_batches_before_summary(self):
        root=Path(__file__).resolve().parents[1]
        preset=json.loads(subprocess.check_output(['node','tests/test_markdown_plan_preset.cjs','--json'],cwd=root,text=True))
        with tempfile.TemporaryDirectory() as workspace:
            service=RunService(Runner)
            service.start(dict(goal='Implement docs/PLAN.md',cwd=workspace,sandbox='workspace-write',
                agents=preset['roles'],max_turns=preset['maxTurns']))
            def finish(expected, target=None):
                run=service.run
                self.assertEqual(run['agent']['name'],expected)
                (Path(workspace)/run['artifact_dir']/run['agent']['artifact']).write_text('Evidence for the current batch')
                service.runner.event({'type':'item.completed','item':{'type':'agent_message',
                    'text':handoff(target) if target else 'Done'}})
                service.runner.done(0,'')
                self.assertNotEqual(service.run['status'],'failed',service.run.get('error'))
            finish('Plan Coordinator','Batch Implementer')
            finish('Batch Implementer')
            finish('Batch Reviewer','Batch Implementer')
            finish('Batch Implementer')
            finish('Batch Reviewer','Batch Tester')
            finish('Batch Tester','Plan Coordinator')
            finish('Plan Coordinator','Batch Implementer')
            finish('Batch Implementer')
            finish('Batch Reviewer','Batch Tester')
            finish('Batch Tester','Plan Coordinator')
            finish('Plan Coordinator','Plan Reporter')
            finish('Plan Reporter')
            self.assertEqual(service.run['status'],'completed')
            ledger=next(a for a in service.run['artifacts'] if a['name']=='PLAN_PROGRESS.md')
            self.assertEqual(ledger['version'],3)


if __name__=='__main__':unittest.main()
