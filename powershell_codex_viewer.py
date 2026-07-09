from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import dragongui as dg


MAX_LOG_LINES = 600
MAX_RAW_LINES = 300


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_ulonglong),
            ("WriteOperationCount", ctypes.c_ulonglong),
            ("OtherOperationCount", ctypes.c_ulonglong),
            ("ReadTransferCount", ctypes.c_ulonglong),
            ("WriteTransferCount", ctypes.c_ulonglong),
            ("OtherTransferCount", ctypes.c_ulonglong),
        ]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", _IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9


def resolve_codex_command() -> str:
    for name in ("codex.cmd", "codex.exe", "codex"):
        resolved = shutil.which(name)
        if resolved:
            return resolved

    appdata = os.environ.get("APPDATA")
    if appdata:
        npm_dir = Path(appdata) / "npm"
        for filename in ("codex.cmd", "codex.exe", "codex.ps1"):
            candidate = npm_dir / filename
            if candidate.exists():
                return str(candidate)

    return "codex"


@dataclass
class CodexRunState:
    prompt: str = ""
    codex_cmd: str = field(default_factory=resolve_codex_command)
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    resume_session_id: str = ""
    model: str = ""
    sandbox: str = "workspace-write"
    extra_args: str = ""
    bypass_approvals_and_sandbox: bool = True
    skip_git_check: bool = True
    ephemeral: bool = False


@dataclass
class CodexEventSummary:
    text: str
    level: str = "info"


class CodexExecRunner:
    def __init__(
        self,
        state: CodexRunState,
        on_event: Callable[[dict[str, Any]], None],
        on_log: Callable[[CodexEventSummary], None],
        on_done: Callable[[int, str], None],
    ) -> None:
        self.state = state
        self.on_event = on_event
        self.on_log = on_log
        self.on_done = on_done
        self.process: subprocess.Popen[str] | None = None
        self._job_handle: int | None = None
        self._stop_requested = threading.Event()

    def start(self) -> None:
        thread = threading.Thread(target=self._run, daemon=True)
        thread.start()

    def stop(self) -> None:
        self._stop_requested.set()
        process = self.process
        if process and process.poll() is None:
            process.terminate()

    def _run(self) -> None:
        cmd = self._build_command()
        cwd = self.state.cwd.strip() or str(Path.cwd())
        stderr_lines: list[str] = []
        stderr_thread: threading.Thread | None = None
        returncode = 1

        try:
            self.on_log(CodexEventSummary("Starting: " + subprocess.list2cmdline(cmd)))
            self.process = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            self._attach_process_lifetime_job()

            assert self.process.stderr is not None
            stderr_thread = threading.Thread(
                target=self._drain_stderr,
                args=(self.process.stderr, stderr_lines),
                daemon=True,
            )
            stderr_thread.start()

            assert self.process.stdin is not None
            prompt = self.state.prompt
            if not prompt.endswith("\n"):
                prompt += "\n"
            self.process.stdin.write(prompt)
            self.process.stdin.close()

            assert self.process.stdout is not None
            for line in self.process.stdout:
                if self._stop_requested.is_set():
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self.on_log(CodexEventSummary(f"Non-JSON stdout: {line}", "warning"))
                    continue
                self.on_event(event)

            if self._stop_requested.is_set() and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()

            returncode = self.process.wait()
            if stderr_thread:
                stderr_thread.join(timeout=1)
        except FileNotFoundError:
            stderr_lines.append(
                f"Could not find Codex command: {self.state.codex_cmd!r}. "
                "Click Detect, or set it to C:\\Users\\nkocur\\AppData\\Roaming\\npm\\codex.CMD."
            )
        except Exception as exc:
            stderr_lines.append(str(exc))
        finally:
            self._close_job_handle()
            self.on_done(returncode, "".join(stderr_lines))

    @staticmethod
    def _drain_stderr(stream: Any, stderr_lines: list[str]) -> None:
        for line in stream:
            stderr_lines.append(line)

    def _build_command(self) -> list[str]:
        codex_cmd = self.state.codex_cmd.strip() or resolve_codex_command()
        resume_session_id = self.state.resume_session_id.strip()
        if resume_session_id:
            cmd = [
                codex_cmd,
                "exec",
                "resume",
                "--json",
            ]
        else:
            cmd = [
                codex_cmd,
                "exec",
                "--json",
                "--color",
                "never",
            ]

        if self.state.bypass_approvals_and_sandbox:
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif not resume_session_id:
            cmd.extend(["--sandbox", self.state.sandbox])

        if not resume_session_id:
            cmd.extend([
                "--cd",
                self.state.cwd.strip() or str(Path.cwd()),
            ])

        if self.state.skip_git_check:
            cmd.append("--skip-git-repo-check")
        if self.state.ephemeral:
            cmd.append("--ephemeral")
        if self.state.model.strip():
            cmd.extend(["--model", self.state.model.strip()])
        if self.state.extra_args.strip():
            cmd.extend(shlex.split(self.state.extra_args))

        if resume_session_id:
            cmd.append(resume_session_id)
        cmd.append("-")
        return cmd

    def _attach_process_lifetime_job(self) -> None:
        if sys.platform != "win32" or self.process is None:
            return

        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return

        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = _kernel32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            _kernel32.CloseHandle(job)
            return

        if not _kernel32.AssignProcessToJobObject(job, self.process._handle):
            _kernel32.CloseHandle(job)
            return

        self._job_handle = job

    def _close_job_handle(self) -> None:
        if sys.platform == "win32" and self._job_handle:
            _kernel32.CloseHandle(self._job_handle)
            self._job_handle = None


class CodexExecGui:
    def __init__(self) -> None:
        self.app = dg.App(theme=dg.Theme.dark())
        self.state = CodexRunState()
        self.runner: CodexExecRunner | None = None
        self.runner_lock = threading.Lock()

        self.event_lines: list[str] = []
        self.activity_lines: list[str] = []
        self.conversation_lines: list[str] = []
        self.raw_lines: list[str] = []
        self.final_text = ""
        self.thread_id = ""
        self.run_started_at = 0.0

        self.prompt_input: dg.TextArea
        self.codex_cmd_input: dg.TextInput
        self.cwd_input: dg.TextInput
        self.model_input: dg.TextInput
        self.extra_args_input: dg.TextInput
        self.sandbox_dropdown: dg.Dropdown
        self.bypass_checkbox: dg.Checkbox
        self.skip_git_checkbox: dg.Checkbox
        self.ephemeral_checkbox: dg.Checkbox
        self.conversation_output: dg.TextArea
        self.final_output: dg.TextArea
        self.activity_output: dg.TextArea
        self.event_output: dg.TextArea
        self.raw_output: dg.TextArea
        self.status: dg.Label

    def run(self) -> None:
        self.app.stylesheet(
            """
            Window { gap: 10px; }
            Panel { padding: 12px; gap: 10px; border-radius: 8px; }
            Button.primary { background: accent; color: background; }
            TextArea.mono { font-family: Consolas, monospace; font-size: 13px; }
            TextArea.prompt { font-family: Consolas, monospace; font-size: 13px; }
            Label.status { color: muted; }
            """
        )

        win = dg.Window("Codex Exec JSONL Console", width=1240, height=900, style={"overflow_y": "auto"})
        with dg.VLayout(style={"gap": 14}):
            with dg.Panel("Run Codex"):
                self.prompt_input = dg.TextArea(
                    value="List the key files in this workspace and explain what this app does.",
                    rows=6,
                    wrap=True,
                    class_="prompt",
                    on_change=self.on_prompt_changed,
                )
                self.state.prompt = "List the key files in this workspace and explain what this app does."

                with dg.HLayout(style={"gap": 8}):
                    dg.Label("Codex command", wrap=False, style={"width": 150})
                    self.codex_cmd_input = dg.TextInput(
                        value=self.state.codex_cmd,
                        on_change=self.on_codex_cmd_changed,
                        style={"flex_grow": 1},
                    )
                    dg.Button("Detect", on_click=self.detect_codex_command)

                with dg.HLayout(style={"gap": 8}):
                    dg.Label("Workspace", wrap=False, style={"width": 150})
                    self.cwd_input = dg.TextInput(
                        value=self.state.cwd,
                        on_change=self.on_cwd_changed,
                        style={"flex_grow": 1},
                    )
                    dg.Button("Use App Folder", on_click=self.use_app_folder)

                with dg.HLayout():
                    dg.Label("Model", wrap=False, style={"width": 55})
                    self.model_input = dg.TextInput(
                        value="",
                        placeholder="Leave blank for Codex config default",
                        on_change=self.on_model_changed,
                        style={"width": 260},
                    )
                    dg.Label("Sandbox", wrap=False, style={"width": 70})
                    self.sandbox_dropdown = dg.Dropdown(
                        ["workspace-write", "read-only", "danger-full-access"],
                        value=self.state.sandbox,
                        on_change=self.on_sandbox_changed,
                        style={"width": 190},
                    )
                    self.bypass_checkbox = dg.Checkbox(
                        "Normal local shell mode",
                        checked=self.state.bypass_approvals_and_sandbox,
                        on_change=self.on_bypass_changed,
                    )
                    self.skip_git_checkbox = dg.Checkbox(
                        "Skip git repo check",
                        checked=self.state.skip_git_check,
                        on_change=self.on_skip_git_changed,
                    )
                    self.ephemeral_checkbox = dg.Checkbox(
                        "Ephemeral",
                        checked=self.state.ephemeral,
                        on_change=self.on_ephemeral_changed,
                    )

                with dg.HLayout():
                    dg.Label("Extra args", wrap=False, style={"width": 150})
                    self.extra_args_input = dg.TextInput(
                        value="",
                        placeholder="Optional: --profile name, --config key=value, etc.",
                        on_change=self.on_extra_args_changed,
                        style={"flex_grow": 1},
                    )

                with dg.HLayout(style={"gap": 8}):
                    dg.Button("Run", on_click=self.start_run, class_="primary")
                    dg.Button("Stop", on_click=self.stop_run)
                    dg.Button("Clear", on_click=self.clear_outputs)
                    dg.Button("Copy Final", on_click=self.copy_final_to_clipboard)
                    dg.Button("Copy Raw JSONL", on_click=self.copy_raw_to_clipboard)

                self.status = dg.Label(
                    "Ready.",
                    class_="status",
                    wrap=True,
                    style={"min_height": 34, "width": "100%"},
                )

            with dg.Panel("Conversation"):
                self.conversation_output = dg.TextArea(
                    value="",
                    rows=12,
                    wrap=True,
                    class_="mono",
                )

            with dg.HLayout(style={"gap": 10}):
                with dg.Panel("Final Response", style={"flex_grow": 1, "flex_basis": 0}):
                    self.final_output = dg.TextArea(
                        value="",
                        rows=20,
                        wrap=True,
                        class_="mono",
                    )
                with dg.Panel("Activity", style={"flex_grow": 1, "flex_basis": 0}):
                    self.activity_output = dg.TextArea(
                        value="",
                        rows=20,
                        wrap=False,
                        class_="mono",
                    )

            with dg.HLayout(style={"gap": 10}):
                with dg.Panel("Event Log", style={"flex_grow": 1, "flex_basis": 0}):
                    self.event_output = dg.TextArea(
                        value="",
                        rows=12,
                        wrap=False,
                        class_="mono",
                    )
                with dg.Panel("Raw JSONL", style={"flex_grow": 1, "flex_basis": 0}):
                    self.raw_output = dg.TextArea(
                        value="",
                        rows=12,
                        wrap=False,
                        class_="mono",
                    )

        self.app.run(win)

    def gui(self, callback: Callable[[], None]) -> None:
        self.app.call_soon_threadsafe(callback)

    def on_prompt_changed(self, value: str) -> None:
        self.state.prompt = value

    def on_codex_cmd_changed(self, value: str) -> None:
        self.state.codex_cmd = value

    def detect_codex_command(self) -> None:
        self.state.codex_cmd = resolve_codex_command()
        self.codex_cmd_input.set_value(self.state.codex_cmd)
        self.status.set_value(f"Using Codex command: {self.state.codex_cmd}")

    def on_cwd_changed(self, value: str) -> None:
        self.state.cwd = value

    def on_model_changed(self, value: str) -> None:
        self.state.model = value

    def on_extra_args_changed(self, value: str) -> None:
        self.state.extra_args = value

    def on_sandbox_changed(self, value: str) -> None:
        self.state.sandbox = value

    def on_bypass_changed(self, checked: bool) -> None:
        self.state.bypass_approvals_and_sandbox = checked
        if checked:
            self.status.set_value("Normal local shell mode runs commands without Codex sandboxing.")

    def on_skip_git_changed(self, checked: bool) -> None:
        self.state.skip_git_check = checked
    def on_ephemeral_changed(self, checked: bool) -> None:
        self.state.ephemeral = checked

    def use_app_folder(self) -> None:
        self.state.cwd = str(Path(__file__).resolve().parent)
        self.cwd_input.set_value(self.state.cwd)

    def start_run(self) -> None:
        prompt = self.state.prompt.strip()
        if not prompt:
            self.status.set_value("Enter a prompt first.")
            return

        cwd = Path(self.state.cwd.strip() or Path.cwd())
        if not cwd.exists() or not cwd.is_dir():
            self.status.set_value(f"Workspace folder does not exist: {cwd}")
            return

        with self.runner_lock:
            if self.runner and self.runner.process and self.runner.process.poll() is None:
                self.status.set_value("Codex is already running. Stop it before starting another run.")
                return

            self.clear_outputs()
            self.append_conversation("User", self.state.prompt)
            self.run_started_at = time.time()
            self.status.set_value("Codex is starting...")

            run_state = CodexRunState(
                prompt=self.state.prompt,
                codex_cmd=self.state.codex_cmd,
                cwd=str(cwd),
                model=self.state.model,
                sandbox=self.state.sandbox,
                extra_args=self.state.extra_args,
                bypass_approvals_and_sandbox=self.state.bypass_approvals_and_sandbox,
                skip_git_check=self.state.skip_git_check,
                ephemeral=self.state.ephemeral,
            )
            self.runner = CodexExecRunner(
                run_state,
                on_event=lambda event: self.gui(lambda event=event: self.handle_event(event)),
                on_log=lambda summary: self.gui(lambda summary=summary: self.append_event(summary.text, summary.level)),
                on_done=lambda code, stderr: self.gui(lambda code=code, stderr=stderr: self.finish_run(code, stderr)),
            )
            self.runner.start()

    def stop_run(self) -> None:
        with self.runner_lock:
            if not self.runner:
                self.status.set_value("No Codex process is running.")
                return
            self.runner.stop()
        self.status.set_value("Stopping Codex...")

    def clear_outputs(self) -> None:
        self.event_lines.clear()
        self.activity_lines.clear()
        self.conversation_lines.clear()
        self.raw_lines.clear()
        self.final_text = ""
        self.thread_id = ""
        self.conversation_output.set_value("")
        self.final_output.set_value("")
        self.activity_output.set_value("")
        self.event_output.set_value("")
        self.raw_output.set_value("")
        self.status.set_value("Cleared.")

    def handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", "unknown"))
        self.raw_lines.append(json.dumps(event, ensure_ascii=False))
        self.raw_lines = self.raw_lines[-MAX_RAW_LINES:]
        self.raw_output.set_value("\n".join(self.raw_lines))

        if event_type == "thread.started":
            self.thread_id = str(event.get("thread_id") or event.get("id") or "")
            self.append_event(f"Thread started {self.thread_id}".strip())
            return

        if event_type == "turn.started":
            self.append_event("Turn started")
            return

        if event_type == "turn.completed":
            usage = event.get("usage")
            if usage:
                self.append_event("Turn completed | usage " + json.dumps(usage, ensure_ascii=False))
            else:
                self.append_event("Turn completed")
            return

        if event_type in {"turn.failed", "thread.error", "error"}:
            self.append_event(f"{event_type}: {self.compact_json(event)}", "error")
            return

        if event_type.startswith("item."):
            self.handle_item_event(event_type, event)
            return

        self.append_event(f"{event_type}: {self.compact_json(event)}")

    def handle_item_event(self, event_type: str, event: dict[str, Any]) -> None:
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        item_type = str(item.get("type") or item.get("item_type") or item.get("kind") or "item")

        if item_type == "agent_message":
            text = self.extract_text(item)
            if text:
                self.final_text = text
                self.final_output.set_value(text)
                self.append_conversation("Codex", text)
                self.append_event(f"{event_type}: agent message ({len(text):,} chars)")
            else:
                self.append_event(f"{event_type}: agent message")
            return

        if item_type == "command_execution":
            command = item.get("command") or item.get("cmd") or item.get("display_command") or "command"
            exit_code = item.get("exit_code")
            status = item.get("status") or event_type.rsplit(".", 1)[-1]
            self.append_activity(f"[{status} exit={exit_code}] {command}")
            output = item.get("aggregated_output") or item.get("output") or item.get("stdout")
            if output:
                self.append_activity(str(output).rstrip())
            self.append_event(f"{event_type}: command_execution")
            return

        if item_type == "file_change":
            changes = item.get("changes")
            if isinstance(changes, list) and changes:
                for change in changes:
                    if isinstance(change, dict):
                        kind = change.get("kind") or change.get("type") or "change"
                        path = change.get("path") or change.get("file") or ""
                        self.append_activity(f"[file {kind}] {path}".rstrip())
            else:
                self.append_activity("[file_change] " + self.compact_json(item))
            self.append_event(f"{event_type}: file_change")
            return

        if item_type in {"mcp_tool_call", "web_search", "todo_list"}:
            self.append_activity(f"[{item_type}] {self.compact_json(item)}")
            self.append_event(f"{event_type}: {item_type}")
            return

        self.append_event(f"{event_type}: {item_type}")

    def append_event(self, text: str, level: str = "info") -> None:
        prefix = time.strftime("%H:%M:%S")
        if level != "info":
            prefix = f"{prefix} {level.upper()}"
        self.event_lines.append(f"{prefix}  {text}")
        self.event_lines = self.event_lines[-MAX_LOG_LINES:]
        self.event_output.set_value("\n".join(self.event_lines))

    def append_activity(self, text: str) -> None:
        self.activity_lines.append(text)
        self.activity_lines = self.activity_lines[-MAX_LOG_LINES:]
        self.activity_output.set_value("\n".join(self.activity_lines))

    def append_conversation(self, speaker: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.conversation_lines.append(f"{speaker}:\n{text}")
        self.conversation_lines = self.conversation_lines[-80:]
        self.conversation_output.set_value("\n\n".join(self.conversation_lines))

    def finish_run(self, returncode: int, stderr: str) -> None:
        elapsed = time.time() - self.run_started_at if self.run_started_at else 0.0
        stderr = stderr.strip()
        if returncode == 0:
            if stderr:
                warn_count = sum(1 for line in stderr.splitlines() if "WARN" in line)
                if warn_count:
                    self.append_event(f"Codex emitted {warn_count} stderr warning line(s); run still succeeded.")
            self.status.set_value(f"Codex finished in {elapsed:.1f}s.")
        else:
            if stderr:
                if "CreateProcessWithLogonW failed: 1327" in stderr:
                    self.append_activity(
                        "[diagnosis] Windows sandbox process launch failed. "
                        "Set Sandbox to danger-full-access, or check "
                        "Normal local shell mode for a normal-terminal style run."
                    )
                self.append_activity("[stderr]\n" + stderr)
                if not self.final_text:
                    self.final_output.set_value(stderr)
            first_line = stderr.splitlines()[0] if stderr else "no stderr output"
            self.append_event(f"Codex exited with code {returncode}: {first_line}", "error")
            self.status.set_value(f"Codex exited with code {returncode} after {elapsed:.1f}s.")

    def copy_final_to_clipboard(self) -> None:
        self.copy_text(self.final_text, "final response")

    def copy_raw_to_clipboard(self) -> None:
        self.copy_text("\n".join(self.raw_lines), "raw JSONL")

    def copy_text(self, text: str, label: str) -> None:
        if not text:
            self.status.set_value(f"No {label} to copy.")
            return

        def worker() -> None:
            try:
                script = "[Console]::InputEncoding=[Text.Encoding]::UTF8; $input | Set-Clipboard"
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", script],
                    input=text,
                    encoding="utf-8",
                    errors="replace",
                    timeout=5,
                    check=True,
                )
                self.gui(lambda: self.status.set_value(f"Copied {label} to clipboard."))
            except Exception as exc:
                self.gui(lambda: self.status.set_value(f"Clipboard copy failed: {exc}"))

        threading.Thread(target=worker, daemon=True).start()

    @staticmethod
    def extract_text(item: dict[str, Any]) -> str:
        for key in ("text", "content", "message"):
            value = item.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, list):
                parts = []
                for part in value:
                    if isinstance(part, str):
                        parts.append(part)
                    elif isinstance(part, dict):
                        part_text = part.get("text") or part.get("content")
                        if isinstance(part_text, str):
                            parts.append(part_text)
                if parts:
                    return "\n".join(parts)
        return ""

    @staticmethod
    def compact_json(value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(text) > 420:
            return text[:417] + "..."
        return text


if __name__ == "__main__":
    CodexExecGui().run()
