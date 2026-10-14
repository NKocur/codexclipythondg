import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from run_diagnostics import RunDiagnostics
from workflow_service import RunService
from test_workflow_service import Runner, role


class DiagnosticsTests(unittest.TestCase):
    def test_activity_is_readable_before_turn_finishes(self):
        with tempfile.TemporaryDirectory() as directory:
            service = RunService(Runner, Path(directory) / 'history')
            try:
                run = service.start(dict(goal='Inspect', cwd=directory, agent=role('Reader')))['run']
                service.runner.event({'type':'item.started', 'item':{
                    'id':'cmd1', 'type':'command_execution', 'command':'python check.py'}})
                records = [json.loads(line) for line in
                           (Path(run['diagnostics_dir']) / 'activity.jsonl').read_text().splitlines()]
                command = next(r for r in records if r['kind'] == 'command')
                self.assertEqual(command['command'], 'python check.py')
                self.assertEqual(command['event'], 'item.started')
                self.assertEqual(service.run['turns'], [])
                self.assertIn('utc', command)
            finally:
                service.close_diagnostics()

    def test_rotation_bounds_file_count(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RunDiagnostics(directory, max_bytes=250)
            for index in range(20):
                journal.write('activity', text='x' * 100, sequence=index)
            self.assertLessEqual(len(list(Path(directory).glob('*.jsonl'))), 4)
            latest = json.loads((Path(directory) / 'activity.jsonl').read_text().splitlines()[-1])
            self.assertEqual(latest['sequence'], 19)

    def test_disk_failure_does_not_fail_agent(self):
        with tempfile.TemporaryDirectory() as directory:
            service = RunService(Runner, Path(directory) / 'history')
            try:
                with patch.object(RunDiagnostics, 'write', side_effect=OSError('disk unavailable')):
                    service.start(dict(goal='Inspect', cwd=directory, agent=role('Reader')))
                    self.assertEqual(service.run['status'], 'running')
                    self.assertIn('disk unavailable', service.run['diagnostics_error'])
            finally:
                service.close_diagnostics()

    def test_sampler_records_resources_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = RunDiagnostics(directory)
            import threading
            written = threading.Event()
            records = []
            def capture(kind, **data):
                records.append((kind, data))
                written.set()
            with patch.object(journal, 'write', side_effect=capture):
                journal.start(7, interval=.01)
                try:
                    self.assertTrue(written.wait(3))
                finally:
                    journal.stop()
                    journal.thread.join(3)
            self.assertFalse(journal.thread.is_alive())
            self.assertEqual(records[0][0], 'resources')
            self.assertEqual(records[0][1]['turn'], 7)
            self.assertTrue(records[0][1]['processes'])
