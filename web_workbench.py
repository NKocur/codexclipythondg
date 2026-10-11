"""Local web workbench. Run: python web_workbench.py (no GUI dependencies)."""
from __future__ import annotations

import argparse
import json
import secrets
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from powershell_codex_viewer import resolve_codex_command
from provider_auth import AuthService, PROVIDERS
from workflow_service import ACTIVE, RunService, validate_request

ROOT = Path(__file__).resolve().parent


def check_codex_startup():
    """Verify configuration/auth access, not merely the executable on PATH."""
    try:
        result = subprocess.run(
            [resolve_codex_command(), "login", "status"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Codex startup check failed: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()[-3000:]
        if "Could not find home directory" in detail:
            raise RuntimeError(
                "Codex cannot access its home directory from this server process. "
                "If launched by a restricted coding tool, stop it and run "
                "`python web_workbench.py` in your normal PowerShell terminal. "
                "Do not move your Codex credentials or disable the agent sandbox.\n" + detail
            )
        raise RuntimeError(
            "Codex is not ready. Run `codex.cmd login status` in the same terminal "
            "and sign in if needed, then restart the server.\n" + detail
        )


def list_folders(path):
    """Read the directory on every request; never keep a startup tree/cache."""
    folder = Path(path).expanduser().resolve(strict=True)
    if not folder.is_dir():
        raise ValueError("Choose a directory, not a file.")
    children = []
    for child in folder.iterdir():
        try:
            if child.is_dir():
                children.append({"name": child.name, "path": str(child),
                                 "git": (child / ".git").exists()})
        except OSError:
            continue  # A child may disappear or become inaccessible during enumeration.
    return {"path": str(folder), "parent": str(folder.parent),
            "folders": sorted(children, key=lambda item: item["name"].casefold())}


class WorkbenchServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, service=None, auth=None):
        super().__init__(address, Handler)
        self.service = service or RunService(history_dir=ROOT / '.codex-workbench-sessions' / 'web-runs')
        self.token = secrets.token_urlsafe(32)
        self.auth = auth or AuthService()


class Handler(BaseHTTPRequestHandler):
    def send(self, status, data, content_type="application/json"):
        body = json.dumps(data).encode() if content_type == "application/json" else data
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(body)

    def trusted(self):
        port = self.server.server_port
        hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        return (self.headers.get("Host") in hosts and
                (not self.headers.get("Origin") or
                 self.headers["Origin"] in {f"http://{h}" for h in hosts}))

    def do_GET(self):
        if not self.trusted():
            return self.send(403, {"error": "Local requests only."})
        path = urlsplit(self.path).path
        if path == "/":
            html = (ROOT / "mockup/index-v2.html").read_text(encoding="utf-8")
            script = '<script src="/live.js"></script>'
            return self.send(200, html.replace("</body>", script + "</body>").encode(), "text/html; charset=utf-8")
        if path == "/live.js":
            return self.send(200, (ROOT / "web/live.js").read_bytes(), "text/javascript; charset=utf-8")
        if path == "/api/bootstrap":
            command = resolve_codex_command()
            return self.send(200, {"token": self.server.token, "cwd": str(ROOT),
                                   "codex_available": bool(shutil.which(command) or Path(command).is_file())})
        if path == "/api/run":
            return self.send(200, self.server.service.snapshot())
        if path == "/api/history":
            if not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.server.token):
                return self.send(403, {"error": "Reload the page to access saved runs."})
            try:
                run_id = parse_qs(urlsplit(self.path).query).get('id', [None])[0]
                return self.send(200, {'run': self.server.service.saved_run(run_id)} if run_id else {'runs': self.server.service.list_runs()})
            except (ValueError, OSError) as exc:
                return self.send(400, {'error': str(exc)})
        if path == "/api/artifact":
            if not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.server.token):
                return self.send(403, {"error": "Reload the page to download results."})
            query = parse_qs(urlsplit(self.path).query)
            try:
                body = self.server.service.download(query.get('run', [''])[0], query.get('name', [''])[0])
                # Always return bytes for download, never execute generated HTML on the app origin.
                return self.send(200, body, "application/octet-stream")
            except (ValueError, OSError) as exc:
                return self.send(400, {"error": str(exc)})
        if path == "/api/folders":
            if not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.server.token):
                return self.send(403, {"error": "Reload the page to reconnect to the folder browser."})
            folder = parse_qs(urlsplit(self.path).query).get("path", [str(ROOT)])[0]
            try:
                return self.send(200, list_folders(folder))
            except (OSError, ValueError) as exc:
                return self.send(400, {"error": f"Could not open that folder: {exc}"})
        return self.send(404, {"error": "Not found."})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                return self.send(413, {"error": "Request must be between 1 and 65536 bytes."})
            # Drain bounded request bodies before replying: Windows can reset a
            # connection closed with unread input, hiding the rejection response.
            self.connection.settimeout(5)
            body = self.rfile.read(length)
            if not self.trusted() or not secrets.compare_digest(self.headers.get("X-Workbench-Token", ""), self.server.token):
                return self.send(403, {"error": "Invalid local session. Reload the page."})
            if self.headers.get_content_type() != "application/json":
                return self.send(415, {"error": "JSON required."})
            data = json.loads(body)
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object.")
            path = urlsplit(self.path).path
            if path == "/api/run":
                validate_request(data)
                auth = self.server.auth.status("Codex", refresh=True)
                if auth["state"] != "ready":
                    return self.send(401 if auth["state"] == "sign_in_required" else 409,
                                     {"error": auth["message"], "auth": auth})
                result = self.server.service.start(data)
            elif path == "/api/stop":
                result = self.server.service.stop(data.get("id"))
            elif path == "/api/control":
                result = self.server.service.control(data.get("id"), data.get("action"), data.get("auto_advance"))
            elif path == "/api/auth/status":
                result = {"providers": [self.server.auth.status(p, refresh=bool(data.get("refresh"))) for p in PROVIDERS]}
            elif path in {"/api/auth/login", "/api/auth/cancel"}:
                provider = data.get("provider")
                if provider not in PROVIDERS:
                    raise ValueError("Choose Codex or Claude.")
                if path.endswith("login"):
                    active = self.server.service.snapshot()["run"]
                    if active and active["status"] in ACTIVE:
                        raise RuntimeError("Stop the active run before changing provider sign-in.")
                    result = self.server.auth.login(provider)
                else:
                    result = self.server.auth.cancel(provider)
            else:
                return self.send(404, {"error": "Not found."})
            self.send(200, result)
        except (ValueError, OSError) as exc:
            self.send(400, {"error": str(exc)})
        except RuntimeError as exc:
            self.send(409, {"error": str(exc)})

    def log_message(self, *args):
        pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    try:
        check_codex_startup()
    except RuntimeError as exc:
        print(f"Provider setup needed: {exc}\nOpen Connections in Workbench to sign in.", flush=True)
    server = WorkbenchServer(("127.0.0.1", args.port))
    print(f"Workbench: http://127.0.0.1:{server.server_port}", flush=True)
    print("Live mode supports Codex teams and single agents. Ctrl+C stops the server.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.auth.close()
        if server.service.runner and server.service.run["status"] in ACTIVE:
            server.service.runner.stop()
        server.server_close()


if __name__ == "__main__":
    main()
