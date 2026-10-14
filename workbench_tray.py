"""Windows tray host for Workbench. Does not take ownership of an existing server."""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser

APP_NAME = 'Multi-Agent Workbench'
PORT = 8765
URL = f'http://127.0.0.1:{PORT}'
ACTIVE = {'running', 'paused', 'stopping'}
AUTOSTART_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
AUTOSTART_NAME = 'MultiAgentWorkbench'


def startup_command():
    if getattr(sys, 'frozen', False):
        return subprocess.list2cmdline([sys.executable, '--startup'])
    interpreter = Path(sys.executable).with_name('pythonw.exe')
    return subprocess.list2cmdline([str(interpreter), str(Path(__file__).resolve()), '--startup'])


def startup_enabled():
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
            return winreg.QueryValueEx(key, AUTOSTART_NAME)[0] == startup_command()
    except FileNotFoundError:
        return False


def set_startup(enabled):
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, AUTOSTART_KEY) as key:
        if enabled:
            winreg.SetValueEx(key, AUTOSTART_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, AUTOSTART_NAME)
            except FileNotFoundError:
                pass


def request_json(path):
    with urllib.request.urlopen(URL + path, timeout=3) as response:
        return json.load(response)


def existing_workbench():
    try:
        info = request_json('/api/bootstrap')
        # Also recognize the pre-tray server without modifying or restarting it.
        return info.get('application') == 'multi-agent-workbench' or (
            isinstance(info.get('token'), str) and isinstance(info.get('cwd'), str)
            and isinstance(info.get('codex_available'), bool))
    except (OSError, ValueError, AttributeError):
        return False


def status_text(run):
    if not run:
        return 'Idle'
    if run['status'] == 'paused':
        return 'Waiting: turn limit' if run.get('pause_reason') == 'turn_limit' else 'Waiting for you'
    if run['status'] == 'running':
        return 'Running: ' + run.get('agent', {}).get('name', 'agent')
    return {'completed':'Finished', 'failed':'Failed', 'stopped':'Stopped', 'stopping':'Stopping'}.get(run['status'], run['status'])


def import_development_history(server):
    """A package launched from dist can retain this checkout's history, read-only."""
    if not getattr(sys, 'frozen', False):
        return
    source = Path(sys.executable).resolve().parents[2] / '.codex-workbench-sessions'
    destination = server.service.history
    for path in [*source.glob('web-run-*.json'), *(source / 'web-runs').glob('*.json')]:
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
            run = payload.get('run', payload)
            try:
                destination.get(run['id'])
            except ValueError:
                destination.save(run)
        except (OSError, ValueError, KeyError, TypeError):
            logging.exception('Could not import saved run %s', path.name)


def tray_image(status='idle'):
    from PIL import Image, ImageDraw
    image = Image.new('RGBA', (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((2, 2, 62, 62), radius=15, fill='#162239')
    for x, y in [(17, 34), (32, 18), (47, 34)]:
        draw.line((32, 45, x, y), fill='#9bb6e3', width=4)
        draw.ellipse((x-6, y-6, x+6, y+6), fill='#eaf2ff')
    color = {'running':'#42d69c', 'paused':'#f4c45e', 'failed':'#ff7474'}.get(status, '#86a5ec')
    draw.ellipse((24, 37, 40, 53), fill=color)
    return image


class TrayHost:
    def __init__(self):
        self.server = None
        self.server_thread = None
        self.status = 'Starting'
        self.run = None
        self.exit_event = threading.Event()
        self.quitting = False
        self.icon = None

    def start_server(self):
        if existing_workbench():
            return  # This server belongs to the user's existing terminal/session.
        from web_workbench import WorkbenchServer
        self.server = WorkbenchServer(('127.0.0.1', PORT))
        import_development_history(self.server)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()

    def snapshot(self):
        return self.server.service.snapshot() if self.server else request_json('/api/run')

    def open_browser(self, *_):
        webbrowser.open(URL)

    def toggle_startup(self, *_):
        try:
            set_startup(not startup_enabled())
            self.icon.update_menu()
        except OSError as exc:
            self.icon.notify(f'Could not update sign-in startup: {exc}', APP_NAME)

    def watch(self):
        previous = None
        while not self.exit_event.is_set():
            try:
                run = self.snapshot()['run']
                self.run = run
                self.status = status_text(run)
                transition = (run['id'], run['status'], run.get('pause_reason')) if run else None
                if transition != previous:
                    self.icon.icon = tray_image(run['status'] if run else 'idle')
                    if run and run['status'] in {'paused','completed','failed'}:
                        self.icon.notify(self.status + '. Open Workbench for details.', APP_NAME)
                previous = transition
            except (OSError, ValueError, KeyError):
                self.status = 'Server unavailable'
            self.icon.title = (APP_NAME + ' — ' + self.status)[:127]
            self.icon.update_menu()
            self.exit_event.wait(2)

    def quit(self, *_):
        if self.quitting:
            return
        if self.server:
            run = self.server.service.snapshot()['run']
            confirmed = bool(run and run['status'] in ACTIVE) or self.server.improvements.active() is True
            if confirmed:
                answer = ctypes.windll.user32.MessageBoxW(None,
                    'An agent workflow or improvement request is active or waiting. Stop it and quit Workbench?\nCompleted work will remain saved.',
                    APP_NAME, 0x00000004 | 0x00000030 | 0x00000100)
                if answer != 6:
                    return
            with self.server.service.lock:
                run = self.server.service.snapshot()['run']
                if (bool(run and run['status'] in ACTIVE) or self.server.improvements.active() is True) and not confirmed:
                    self.icon.notify('A workflow just started. Choose Quit again to stop it.', APP_NAME)
                    return
                self.quitting = True
                # Reject new runs while shutdown is underway.
                self.server.closing = True
                if run and run['status'] in ACTIVE:
                    self.server.service.stop(run['id'])
            self.server.improvements.close()
            deadline = time.monotonic() + 12
            while (self.server.service.snapshot()['run'] and self.server.service.snapshot()['run']['status'] in ACTIVE) or self.server.improvements.active() is True:
                if time.monotonic() >= deadline:
                    self.quitting = False
                    self.server.closing = False
                    self.server.improvements.closed = False
                    self.icon.notify('The agent is still stopping. Workbench will stay open; try Quit again shortly.', APP_NAME)
                    return
                time.sleep(.1)
            self.server.auth.close()
            self.server.shutdown()
            self.server.server_close()
        self.exit_event.set()
        self.icon.stop()

    def run_tray(self, startup=False):
        import pystray
        self.start_server()
        self.icon = pystray.Icon('MultiAgentWorkbench', tray_image(), APP_NAME,
            menu=pystray.Menu(
                pystray.MenuItem('Open Workbench', self.open_browser, default=True),
                pystray.MenuItem(lambda item: self.status, None, enabled=False),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Start with Windows', self.toggle_startup, checked=lambda item: startup_enabled()),
                pystray.MenuItem('Quit' if self.server else 'Quit tray (existing server stays running)', self.quit)))
        def ready(icon):
            icon.visible = True
            threading.Thread(target=self.watch, daemon=True).start()
            if not startup:
                self.open_browser()
        self.icon.run(setup=ready)


def self_test(output):
    """Exercise the packaged assets on a temporary port; never touch port 8765."""
    import tempfile
    import pystray
    from document_preview import render_markdown
    from web_workbench import WorkbenchServer
    with tempfile.TemporaryDirectory() as directory:
        server = WorkbenchServer(('127.0.0.1', 0), history_dir=Path(directory)/'history', default_workspace=directory)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            base = f'http://127.0.0.1:{server.server_port}'
            for path in ('/', '/live.js', '/preview.js', '/improve.js', '/api/bootstrap'):
                with urllib.request.urlopen(base+path, timeout=5) as response:
                    assert response.status == 200 and response.read()
            assert '<h1>Check</h1>' in render_markdown('# Check')
            assert tray_image().size == (64, 64)
            assert pystray.Icon
            Path(output).write_text(json.dumps({'ok':True,'frozen':bool(getattr(sys,'frozen',False)), 'port':server.server_port}), encoding='utf-8')
        finally:
            server.shutdown(); server.server_close(); thread.join(5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--startup', action='store_true')
    parser.add_argument('--self-test-output')
    args = parser.parse_args()
    if args.self_test_output:
        self_test(args.self_test_output)
        return
    if sys.platform != 'win32':
        raise RuntimeError('The tray launcher currently supports Windows only.')
    data = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'MultiAgentWorkbench'
    data.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, handlers=[RotatingFileHandler(data/'tray.log', maxBytes=1_000_000, backupCount=2)])
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = 'Local\\MultiAgentWorkbenchTray-' + hashlib.sha256(str(Path.home()).encode()).hexdigest()[:16]
    mutex = kernel.CreateMutexW(None, False, name)
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if ctypes.get_last_error() == 183:
            if not args.startup:
                webbrowser.open(URL)
            return
        TrayHost().run_tray(args.startup)
    except Exception:
        logging.exception('Tray launcher failed')
        ctypes.windll.user32.MessageBoxW(None, f'Could not start Workbench. The existing server has not been stopped.\nDetails: {data / "tray.log"}', APP_NAME, 0x10)
    finally:
        kernel.CloseHandle(mutex)


if __name__ == '__main__':
    main()
