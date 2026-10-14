import tempfile
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, Mock

import workbench_tray as tray


class TrayTests(unittest.TestCase):
    def test_quit_preserves_paused_checkpoint(self):
        host=tray.TrayHost();host.server=Mock();host.icon=Mock()
        host.server.service.lock=threading.RLock()
        host.server.improvements.active.return_value=False
        host.server.service.snapshot.return_value={'run':{'id':'run','status':'paused'}}
        with patch.object(tray.ctypes.windll.user32,'MessageBoxW') as confirm:
            host.quit()
        confirm.assert_not_called()
        host.server.service.stop.assert_not_called()
        host.server.shutdown.assert_called_once()

    def test_attaches_without_creating_or_stopping_existing_server(self):
        host=tray.TrayHost()
        host.icon=Mock()
        with patch.object(tray,'existing_workbench',return_value=True), patch('web_workbench.WorkbenchServer') as server:
            host.start_server()
            self.assertIsNone(host.server)
            server.assert_not_called()
            host.quit()
            server.assert_not_called()
        host.icon.stop.assert_called_once()

    def test_status_includes_turn_limit(self):
        self.assertEqual(tray.status_text(None),'Idle')
        self.assertEqual(tray.status_text({'status':'paused','pause_reason':'turn_limit'}),'Waiting: turn limit')
        self.assertEqual(tray.status_text({'status':'running','agent':{'name':'Coder'}}),'Running: Coder')

    def test_startup_command_quotes_installed_path(self):
        with patch.object(tray.sys,'frozen',True,create=True), patch.object(tray.sys,'executable',r'C:\My Apps\WorkbenchTray.exe'):
            self.assertEqual(tray.startup_command(),r'"C:\My Apps\WorkbenchTray.exe" --startup')

    def test_startup_toggle_only_changes_own_value(self):
        import winreg
        key=Mock();key.__enter__=Mock(return_value=key);key.__exit__=Mock(return_value=False)
        with patch.object(winreg,'CreateKey',return_value=key), patch.object(winreg,'SetValueEx') as set_value, patch.object(winreg,'DeleteValue') as delete:
            tray.set_startup(True)
            self.assertEqual(set_value.call_args.args[1],tray.AUTOSTART_NAME)
            self.assertIn('--startup',set_value.call_args.args[-1])
            tray.set_startup(False)
            delete.assert_called_once_with(key,tray.AUTOSTART_NAME)

    def test_cancel_quit_preserves_owned_active_run(self):
        host=tray.TrayHost();host.server=Mock();host.icon=Mock()
        host.server.service.snapshot.return_value={'run':{'id':'run','status':'running'}}
        with patch.object(tray.ctypes.windll.user32,'MessageBoxW',return_value=7):
            host.quit()
        host.server.service.stop.assert_not_called()
        host.server.shutdown.assert_not_called()
        host.icon.stop.assert_not_called()

    def test_packaged_server_uses_writable_history_and_workspace(self):
        from web_workbench import WorkbenchServer
        with tempfile.TemporaryDirectory() as directory:
            with patch('web_workbench.sys.frozen',True,create=True), patch.dict('os.environ',{'LOCALAPPDATA':directory}):
                server=WorkbenchServer(('127.0.0.1',0))
                try:
                    self.assertTrue(server.service.history.directory.is_relative_to(Path(directory)))
                    self.assertEqual(server.default_workspace,Path.home()/'Documents')
                finally: server.server_close()

    def test_confirmed_quit_stops_owned_workflow_before_server(self):
        host=tray.TrayHost();host.server=Mock();host.icon=Mock()
        host.server.service.lock=threading.RLock()
        state={'run':{'id':'run','status':'running'}}
        host.server.service.snapshot.side_effect=lambda: state
        host.server.service.stop.side_effect=lambda run_id: state['run'].update(status='stopped')
        with patch.object(tray.ctypes.windll.user32,'MessageBoxW',return_value=6):
            host.quit()
        host.server.service.stop.assert_called_once_with('run')
        host.server.shutdown.assert_called_once()
        host.icon.stop.assert_called_once()

    def test_history_import_preserves_existing_records(self):
        from run_history import RunHistory
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'.codex-workbench-sessions'/'web-runs';source.mkdir(parents=True)
            run={'id':'a'*32,'status':'completed','goal':'original','started':1}
            original=json.dumps(run)
            (source/(run['id']+'.json')).write_text(original)
            server=Mock();server.service.history=RunHistory(root/'app-data'/'web-runs')
            with patch.object(tray.sys,'frozen',True,create=True),patch.object(tray.sys,'executable',str(root/'dist'/'WorkbenchTray'/'WorkbenchTray.exe')):
                tray.import_development_history(server)
                self.assertEqual(server.service.history.get(run['id'])['goal'],'original')
                server.service.history.save({**run,'goal':'newer'})
                tray.import_development_history(server)
                self.assertEqual(server.service.history.get(run['id'])['goal'],'newer')
            self.assertEqual((source/(run['id']+'.json')).read_text(),original)


if __name__=='__main__':unittest.main()
