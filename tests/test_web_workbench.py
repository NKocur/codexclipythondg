"""Run with python -m unittest discover -s tests -p test_web_workbench.py."""
import json
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from urllib.parse import quote
from pathlib import Path

from web_workbench import RunService, WorkbenchServer, check_codex_startup
from powershell_codex_viewer import CodexExecRunner


class FakeRunner:
    def __init__(self, state, on_event, on_log, on_done):
        self.state, self.on_event, self.on_done = state, on_event, on_done

    def start(self):
        self.on_event({"type": "thread.started", "thread_id": "test"})

    def stop(self):
        self.on_done(1, "Stopped test process")


class FakeAuth:
    def status(self, provider, refresh=False):
        return {"provider": provider, "state": "ready", "message": "Signed in"}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = RunService(FakeRunner)
        self.server = WorkbenchServer(("127.0.0.1", 0), self.service, FakeAuth())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.data = {"goal": "Inspect the project", "cwd": self.temp.name,
                     "sandbox": "read-only", "agent": {"id": "one", "name": "Inspector",
                     "prompt": "Describe the project", "provider": "Codex", "effort": "High"}}

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)
        self.temp.cleanup()

    def request(self, path, data=None, token=True, headers=None):
        h = {"Content-Type": "application/json"}
        if token:
            h["X-Workbench-Token"] = self.server.token
        h.update(headers or {})
        req = urllib.request.Request(self.url + path, headers=h,
                                     data=None if data is None else json.dumps(data).encode())
        try:
            response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            body = response.read()
            return response.status, json.loads(body) if response.headers.get_content_type() == "application/json" else body

    def test_report_download_requires_session_and_current_result(self):
        self.data['sandbox'] = 'workspace-write'
        self.data['agent']['deliverables'] = ['REPORT.html']
        run = self.service.start(self.data)['run']
        html = b'<html><body>Report</body></html>'
        (Path(self.temp.name) / run['artifact_dir'] / 'REPORT.html').write_bytes(html)
        self.service.runner.on_done(0, '')
        endpoint = '/api/artifact?run=' + run['id'] + '&name=REPORT.html'
        self.assertEqual(self.request(endpoint, token=False)[0], 403)
        self.assertEqual(self.request(endpoint), (200, html))
        self.assertEqual(self.request(endpoint.replace('REPORT.html','../private.html'))[0], 400)
        self.assertEqual(self.request(endpoint.replace(run['id'],'stale'))[0], 400)
        req = urllib.request.Request(self.url + endpoint, headers={'X-Workbench-Token':self.server.token})
        with urllib.request.urlopen(req) as response:
            self.assertEqual(response.headers.get_content_type(), 'application/octet-stream')
            self.assertEqual(response.headers['X-Content-Type-Options'], 'nosniff')

    def test_history_api_requires_token_and_can_launch_followup(self):
        run = self.service.start(self.data)['run']
        self.service.runner.on_done(0, '')
        self.assertEqual(self.request('/api/history',token=False)[0],403)
        self.assertEqual(self.request('/api/history?id='+run['id'],token=False)[0],403)
        self.assertEqual(self.request('/api/history')[1]['runs'][0]['id'],run['id'])
        self.assertEqual(self.request('/api/history?id='+run['id'])[1]['run']['goal'],self.data['goal'])
        code, snapshot = self.request('/api/run', {**self.data,'goal':'Compare prior findings','source_run_id':run['id']})
        self.assertEqual(code,200)
        self.assertEqual(snapshot['run']['source_run_id'],run['id'])
        self.assertIn('source-context.json',self.service.runner.state.prompt)

    def test_page_and_start_snapshot(self):
        code, body = self.request("/")
        self.assertEqual(code, 200)
        self.assertIn(b'<script src="/live.js"></script>', body)
        self.assertEqual(self.request("/live.js")[0], 200)
        code, snapshot = self.request("/api/run", self.data)
        self.assertEqual(code, 200)
        self.assertEqual(snapshot["run"]["status"], "running")
        self.assertFalse(self.service.runner.state.bypass_approvals_and_sandbox)
        self.assertTrue(self.service.runner.state.ephemeral)
        self.assertEqual(self.service.runner.state.model_reasoning_effort, "high")
        self.data["agent"]["name"] = "Changed"
        self.assertEqual(self.request("/api/run")[1]["run"]["agent"]["name"], "Inspector")
        self.assertEqual(self.request("/api/run", self.data)[0], 409)

    def test_output_failure_and_old_callbacks(self):
        first = self.service.start(self.data)["run"]["id"]
        runner = self.service.runner
        runner.on_event({"type": "item.completed", "item": {"type": "agent_message", "text": "Finished inspecting"}})
        runner.on_done(0, "")
        snapshot = self.service.snapshot()["run"]
        self.assertEqual(snapshot["status"], "completed")
        self.assertIn("Finished inspecting", snapshot["output"])
        self.service.start(self.data)
        self.service.done(first, 1, "Late callback")
        self.assertEqual(self.service.snapshot()["run"]["status"], "running")
        self.service.runner.on_event({"type": "turn.failed", "error": {"message": "Sign in required"}})
        self.service.runner.on_done(1, "")
        self.assertEqual(self.service.snapshot()["run"]["status"], "failed")
        self.assertIn("Sign in required", self.service.snapshot()["run"]["error"])

    def test_stop(self):
        run_id = self.service.start(self.data)["run"]["id"]
        self.assertEqual(self.request("/api/stop", {"id": "wrong"})[0], 400)
        self.assertEqual(self.request("/api/stop", {"id": run_id})[0], 200)
        deadline = time.monotonic() + 2
        while self.service.snapshot()["run"]["status"] == "stopping" and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertEqual(self.service.snapshot()["run"]["status"], "stopped")

    def test_request_boundaries(self):
        self.assertEqual(self.request("/api/run", self.data, token=False)[0], 403)
        self.assertEqual(self.request("/api/run", self.data, headers={"Origin": "https://example.com"})[0], 403)
        self.assertEqual(self.request("/api/bootstrap", headers={"Host": "example.com"})[0], 403)
        self.assertEqual(self.request("/api/run", ["invalid"])[0], 400)
        self.data["agent"]["provider"] = "Claude"
        self.assertEqual(self.request("/api/run", self.data)[0], 400)
        self.data["agent"]["provider"] = "Codex"
        self.data["cwd"] = str(Path(self.temp.name) / "missing")
        self.assertEqual(self.request("/api/run", self.data)[0], 400)
        self.assertEqual(self.request("/README.md")[0], 404)

    def test_folder_listing_refreshes_during_run(self):
        root = Path(self.temp.name)
        (root / "existing").mkdir()
        (root / "file.txt").write_text("not a directory")
        endpoint = "/api/folders?path=" + quote(str(root))
        self.assertEqual(self.request(endpoint, token=False)[0], 403)
        run = self.service.start(self.data)
        self.assertEqual([f["name"] for f in self.request(endpoint)[1]["folders"]], ["existing"])
        new = root / "New project & notes"
        new.mkdir()
        (new / ".git").mkdir()
        code, refreshed = self.request(endpoint)
        self.assertEqual(code, 200)
        self.assertEqual([f["name"] for f in refreshed["folders"]], ["existing", new.name])
        self.assertTrue(refreshed["folders"][1]["git"])
        self.assertEqual(self.request('/api/folders?path=' + quote(str(new)))[1]["parent"], str(root.resolve()))
        (root / "existing").rename(root / "renamed")
        self.assertEqual([f["name"] for f in self.request(endpoint)[1]["folders"]], [new.name, "renamed"])
        self.assertEqual(self.service.snapshot()["run"]["cwd"], run["run"]["cwd"])
        self.assertEqual(self.service.snapshot()["run"]["status"], "running")
        self.assertEqual(self.request('/api/folders?path=' + quote(str(root / 'missing')))[0], 400)
        self.assertEqual(self.request('/api/folders?path=' + quote(str(root / 'file.txt')))[0], 400)

    def test_auth_rejection_returns_recovery_without_starting_runner(self):
        with patch.object(self.server.auth, 'status', return_value={
            'provider':'Codex', 'state':'sign_in_required', 'message':'Sign in to continue.'
        }):
            code, payload = self.request('/api/run', self.data)
        self.assertEqual(code, 401)
        self.assertEqual(payload['auth']['provider'], 'Codex')
        self.assertIsNone(self.service.runner)
        self.assertEqual(self.request('/api/auth/status', {}, token=False)[0], 403)
        self.assertEqual(self.request('/api/auth/login', {'provider':'Codex'}, token=False)[0], 403)
        self.assertEqual(self.request('/api/auth/login', {'provider':'injected command'})[0], 400)

    def test_expired_credentials_offer_sign_in_but_quota_does_not(self):
        self.service.start(self.data)
        self.service.runner.on_done(1, '401 Unauthorized: token has expired')
        self.assertEqual(self.service.snapshot()['run']['auth_provider'], 'Codex')
        self.service.start(self.data)
        self.service.runner.on_done(1, '429 Rate limit reached')
        self.assertEqual(self.service.snapshot()['run']['failure_kind'], 'usage_limit')
        self.assertNotIn('auth_provider', self.service.snapshot()['run'])

    def test_team_http_manual_continuation(self):
        data={**self.data,'sandbox':'workspace-write','auto_advance':False,'agents':[
            {**self.data['agent'],'id':'one','artifact':'ONE.md','route':'next'},
            {**self.data['agent'],'id':'two','name':'Finisher','artifact':'TWO.md','route':'end'}]}
        code, snapshot=self.request('/api/run',data)
        self.assertEqual(code,200)
        run=snapshot['run']
        (Path(self.temp.name)/run['artifact_dir']/'ONE.md').write_text('First output')
        self.service.runner.on_done(0,'')
        self.assertEqual(self.request('/api/run')[1]['run']['status'],'paused')
        code, snapshot=self.request('/api/control',{'id':run['id'],'action':'continue'})
        self.assertEqual(code,200)
        self.assertEqual(snapshot['run']['agent']['name'],'Finisher')
        (Path(self.temp.name)/run['artifact_dir']/'TWO.md').write_text('Second output')
        self.service.runner.on_done(0,'')
        result=self.request('/api/run')[1]['run']
        self.assertEqual(result['status'],'completed')
        self.assertEqual([a['body'] for a in result['artifacts']],['First output','Second output'])
        self.assertEqual(self.request('/api/control',{'id':run['id'],'action':'continue'})[0],400)
        self.assertEqual(self.request('/api/control',{'id':run['id'],'action':'pause'},token=False)[0],403)


class ProcessTests(unittest.TestCase):
    def test_startup_check_explains_restricted_home(self):
        import subprocess
        with patch('web_workbench.subprocess.run', return_value=subprocess.CompletedProcess(
            [], 1, '', 'Error finding codex home: Could not find home directory'
        )) as command:
            with self.assertRaisesRegex(RuntimeError, 'normal PowerShell terminal'):
                check_codex_startup()
            self.assertEqual(command.call_args.args[0][-2:], ['login', 'status'])

    def test_startup_check_accepts_authenticated_cli(self):
        import subprocess
        with patch('web_workbench.subprocess.run', return_value=subprocess.CompletedProcess(
            [], 0, '', 'Logged in using ChatGPT'
        )):
            check_codex_startup()

    def test_existing_runner_streams_a_real_child_process(self):
        class FixtureRunner(CodexExecRunner):
            def _build_command(self):
                script = (
                    "import sys,json;sys.stdin.read();"
                    "print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':'Real process output'}}),flush=True);"
                    "print(json.dumps({'type':'turn.completed'}),flush=True)"
                )
                return [sys.executable, "-u", "-c", script]

        service = RunService(FixtureRunner)
        with tempfile.TemporaryDirectory() as directory:
            service.start({"goal": "test", "cwd": directory, "agent": {
                "name": "Fixture", "prompt": "test", "provider": "Codex"}})
            deadline = time.monotonic() + 10
            while service.snapshot()["run"]["status"] == "running" and time.monotonic() < deadline:
                time.sleep(.02)
            result = service.snapshot()["run"]
            self.assertEqual(result["status"], "completed", result)
            self.assertIn("Real process output", result["output"])


if __name__ == "__main__":
    unittest.main()
