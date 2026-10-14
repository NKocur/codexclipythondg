"""CLI-owned authentication: expose status, never credentials or OAuth output."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time

from powershell_codex_viewer import CodexExecRunner, CodexRunState, resolve_codex_command

PROVIDERS = ("Codex", "Claude")


def command_for(provider):
    if provider == "Codex":
        return resolve_codex_command()
    if provider == "Claude":
        return next((p for name in ("claude.exe", "claude.cmd", "claude")
                     if (p := shutil.which(name))), "claude")
    raise ValueError("Unknown authentication provider.")


def failure_kind(text):
    text = text.lower()
    if "could not find home directory" in text:
        return "environment"
    if any(s in text for s in ('model is at capacity', 'model at capacity', 'model is overloaded')):
        return 'model_capacity'
    if any(s in text for s in ("usage limit", "rate limit", "rate_limit", "quota", "insufficient credits", "credit balance", "429")):
        return "usage_limit"
    if (any(s in text for s in ("not logged in", "not authenticated", "authentication required",
                                "authentication_error", "invalid_api_key", "invalid api key",
                                "refresh_token_reused", "refresh token has expired", "please log in",
                                "please login", "please sign in", "sign in required", "unauthorized"))
            or re.search(r"\b401\b", text)
            or re.search(r"(?:token|credential)[^\n]{0,60}(?:expired|revoked|invalid)", text)):
        return "sign_in_required"
    if (re.search(r'(?:http(?:/\d(?:\.\d)?)?|status(?: code)?)\s*[:=]?\s*(?:500|502|503|504)\b', text)
            or any(s in text for s in ('service unavailable', 'bad gateway', 'gateway timeout',
                'stream disconnected before completion', 'connection reset by peer',
                'error sending request for url', 'connection timed out', 'request timed out',
                'reconnecting...'))):
        return 'provider_transient'
    return "error"


def result(provider, state, message):
    return {"provider": provider, "state": state, "message": message,
            "command": "codex.cmd login" if provider == "Codex" else "claude.cmd auth login"}


class LoginRunner(CodexExecRunner):
    """Reuse process-tree lifetime/stop handling; never expose login output."""
    def __init__(self, provider, on_done):
        self.provider = provider
        super().__init__(CodexRunState(prompt=""), lambda event: None, lambda log: None,
                         lambda code, stderr: on_done(code))

    def _build_command(self):
        args = ["login"] if self.provider == "Codex" else ["auth", "login", "--claudeai"]
        return [command_for(self.provider), *args]


class AuthService:
    def __init__(self):
        self.lock = threading.RLock()
        self.cache = {}
        self.logins = {}

    def status(self, provider, refresh=False):
        command = command_for(provider)
        with self.lock:
            if provider in self.logins:
                return result(provider, "signing_in", "Complete sign-in in the browser window opened by the CLI.")
            cached = self.cache.get(provider)
            if cached and not refresh and time.monotonic() - cached[0] < 5:
                return cached[1]
            args = ["login", "status"] if provider == "Codex" else ["auth", "status", "--json"]
            try:
                response = subprocess.run([command, *args], capture_output=True, text=True,
                                          encoding="utf-8", errors="replace", timeout=15,
                                          creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
                detail = response.stdout + response.stderr
                if failure_kind(detail) == "environment":
                    state = result(provider, "environment", "The CLI cannot access its home directory. Start Workbench from your normal terminal.")
                elif response.returncode == 0:
                    state = result(provider, "ready", "Signed in. Provider access and usage limits are checked when a task runs.")
                else:
                    signed_out = failure_kind(detail) == "sign_in_required"
                    if provider == "Claude":
                        try:
                            signed_out |= json.loads(response.stdout).get("loggedIn") is False
                        except (ValueError, AttributeError):
                            pass
                    state = result(provider, "sign_in_required" if signed_out else "error",
                                   "Sign in to continue." if signed_out else "Could not check sign-in. Check the CLI in your normal terminal, then try again.")
            except FileNotFoundError:
                state = result(provider, "not_installed", "CLI not found. Install it, then check again.")
            except (OSError, subprocess.TimeoutExpired):
                state = result(provider, "error", "Sign-in check failed or timed out. Check your CLI and connection, then try again.")
            self.cache[provider] = (time.monotonic(), state)
            return state

    def login(self, provider):
        command_for(provider)  # Validate before constructing any process.
        with self.lock:
            if provider in self.logins:
                return self.status(provider)
            state = self.status(provider, refresh=True)
            if state["state"] in {"not_installed", "environment"}:
                return state
            runner = LoginRunner(provider, lambda code: self.finished(provider, code))
            timer = threading.Timer(180, lambda: self.cancel(provider))
            timer.daemon = True
            self.logins[provider] = (runner, timer)
            runner.start()
            timer.start()
            return self.status(provider)

    def finished(self, provider, code):
        with self.lock:
            login = self.logins.pop(provider, None)
            if login:
                login[1].cancel()
            self.cache.pop(provider, None)
            if code:
                self.cache[provider] = (time.monotonic(), result(provider, "sign_in_required",
                    "Sign-in did not complete. Try again, or run the login command in a terminal."))

    def cancel(self, provider):
        command_for(provider)
        with self.lock:
            login = self.logins.get(provider)
        if login:
            login[0].stop()
        return self.status(provider)

    def close(self):
        with self.lock:
            providers = list(self.logins)
        for provider in providers:
            self.cancel(provider)
