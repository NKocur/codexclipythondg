from __future__ import annotations

import importlib
import json
import os
import shlex
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from PyQt6 import QtCore, QtGui, QtWidgets

CodexExecRunner: Any = None
CodexRunState: Any = None
_resolve_codex_command: Callable[[], str] | None = None

PROVIDERS = ("Codex", "Claude", "Ollama")
ROLE_PROVIDER_DEFAULT_LABEL = "Default / Global"


MAX_LOG_LINES = 600
MAX_RAW_LINES = 300
MAX_INLINE_ACTIVITY_OUTPUT_CHARS = 6000
INLINE_ACTIVITY_OUTPUT_PREVIEW_CHARS = 2000
MAX_COMMAND_OUTPUT_SAVE_CHARS = 200_000
MAX_COMMAND_OUTPUT_FILES = 50
MAX_COMMAND_OUTPUT_TOTAL_BYTES = 50_000_000
HANDOFF_MIN_LENGTH = 20
HANDOFF_MAX_LENGTH = 6000
HANDOFF_END = "[[END_HANDOFF]]"
HANDOFF_DONE = "[[COMMANDDOCK_DONE]]"
APPROVED = "[[APPROVED]]"


@dataclass
class DependencyStatus:
    ok: bool
    message: str


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    label: str


def load_runtime_dependencies() -> DependencyStatus:
    global CodexExecRunner, CodexRunState, _resolve_codex_command
    if CodexExecRunner is not None and CodexRunState is not None:
        return DependencyStatus(True, "Codex runner available.")

    try:
        viewer = importlib.import_module("powershell_codex_viewer")
    except ModuleNotFoundError as exc:
        return DependencyStatus(False, f"Missing runtime dependency: {exc}")
    except Exception as exc:
        return DependencyStatus(False, f"Could not load runtime dependencies: {exc}")

    CodexExecRunner = viewer.CodexExecRunner
    CodexRunState = viewer.CodexRunState
    _resolve_codex_command = viewer.resolve_codex_command
    return DependencyStatus(True, "Codex runner available.")


def dependency_preflight() -> DependencyStatus:
    return load_runtime_dependencies()


def resolve_codex_command() -> str:
    status = load_runtime_dependencies()
    if status.ok and _resolve_codex_command is not None:
        return _resolve_codex_command()
    return "codex"


def resolve_claude_command() -> str:
    """Keep the Claude CLI configurable while working with PATH installations."""
    return "claude"


@dataclass
class ClaudeRunState:
    prompt: str
    claude_cmd: str
    cwd: str
    resume_session_id: str = ""
    model: str = ""
    extra_args: str = ""
    accept_edits: bool = True


class ClaudeExecRunner:
    """Adapt Claude Code's stream-json output to the workbench event contract."""

    def __init__(
        self,
        state: ClaudeRunState,
        on_event: Callable[[dict[str, Any]], None],
        on_log: Callable[[Any], None],
        on_done: Callable[[int, str], None],
    ) -> None:
        self.state = state
        self.on_event = on_event
        self.on_log = on_log
        self.on_done = on_done
        self.process: subprocess.Popen[str] | None = None
        self._stop_requested = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop_requested.set()
        if self.process and self.process.poll() is None:
            self.process.terminate()

    def _run(self) -> None:
        stderr_lines: list[str] = []
        text_parts: list[str] = []
        session_id = self.state.resume_session_id
        command = [self.state.claude_cmd or "claude", "--bare", "-p", self.state.prompt,
                   "--output-format", "stream-json", "--verbose", "--include-partial-messages"]
        if self.state.model.strip():
            command.extend(["--model", self.state.model.strip()])
        if session_id:
            command.extend(["--resume", session_id])
        command.extend(["--permission-mode", "acceptEdits" if self.state.accept_edits else "dontAsk"])
        if self.state.extra_args.strip():
            try:
                command.extend(shlex.split(self.state.extra_args, posix=False))
            except ValueError as exc:
                self.on_done(1, f"Invalid extra arguments: {exc}")
                return
        try:
            self.on_log(type("Log", (), {"text": "Starting: " + subprocess.list2cmdline(command), "level": "info"})())
            self.process = subprocess.Popen(
                command, cwd=self.state.cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
            )
            assert self.process.stdout is not None
            for line in self.process.stdout:
                if self._stop_requested.is_set():
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self.on_log(type("Log", (), {"text": f"Non-JSON stdout: {line.strip()}", "level": "warning"})())
                    continue
                if str(event.get("type")) == "system" and str(event.get("subtype")) == "init":
                    session_id = str(event.get("session_id") or session_id)
                    self.on_event({"type": "thread.started", "thread_id": session_id})
                elif str(event.get("type")) == "stream_event":
                    delta = event.get("event", {}).get("delta", {}) if isinstance(event.get("event"), dict) else {}
                    if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                        text_parts.append(delta["text"])
                elif str(event.get("type")) == "result":
                    result = event.get("result")
                    if isinstance(result, str) and result:
                        text_parts = [result]
                    if event.get("is_error"):
                        self.on_event({"type": "turn.failed", "message": result or event.get("error")})
            if self.process.stderr:
                stderr_lines.extend(self.process.stderr.readlines())
            returncode = self.process.wait()
            if text_parts:
                self.on_event({"type": "item.completed", "item": {"type": "agent_message", "text": "".join(text_parts)}})
            if returncode == 0:
                self.on_event({"type": "turn.completed"})
            self.on_done(returncode, "".join(stderr_lines))
        except FileNotFoundError:
            self.on_done(1, f"Could not find Claude command: {self.state.claude_cmd!r}. Set the Claude command in Execution Settings.")
        except Exception as exc:
            self.on_done(1, str(exc))


@dataclass
class OllamaRunState:
    prompt: str
    base_url: str
    model: str
    keep_alive: str = "1h"
    request_timeout_seconds: int = 600
    command_timeout_seconds: int = 120
    verify_tls: bool = True
    ca_bundle_path: str = ""
    context_length: int = 0
    expected_artifact_path: str = ""
    expected_artifact_fingerprint: str = ""


class OllamaExecRunner:
    """Run a bounded Ollama chat/tool loop and adapt it to workbench events."""

    def __init__(
        self,
        state: OllamaRunState,
        execute_tool: Callable[[str, dict[str, Any]], str],
        on_event: Callable[[dict[str, Any]], None],
        on_log: Callable[[Any], None],
        on_done: Callable[[int, str], None],
        bearer_token: str = "",
    ) -> None:
        self.state = state
        self.execute_tool = execute_tool
        self.on_event = on_event
        self.on_log = on_log
        self.on_done = on_done
        self.bearer_token = bearer_token
        self.response: Any = None
        self._stop_requested = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._run, daemon=True).start()

    def stop(self) -> None:
        self._stop_requested.set()
        response = self.response
        if response is not None:
            response.close()

    def _log(self, text: str, level: str = "info") -> None:
        self.on_log(type("Log", (), {"text": text, "level": level})())

    @staticmethod
    def tools_schema() -> list[dict[str, Any]]:
        return [
            {"type": "function", "function": {"name": "list_workspace", "description": "List files below the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}}}},
            {"type": "function", "function": {"name": "read_workspace_file", "description": "Read a UTF-8 file below the workspace.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
            {"type": "function", "function": {"name": "write_workspace_file", "description": "Write a UTF-8 file below the workspace. path must be a relative file path, for example artifacts/PLAN.md.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}}},
            {"type": "function", "function": {"name": "apply_workspace_patch", "description": "Edit an existing UTF-8 workspace file by replacing one exact old_text occurrence with new_text. Use this instead of shell scripts for code or HTML edits.", "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "old_text": {"type": "string"}, "new_text": {"type": "string"}}, "required": ["path", "old_text", "new_text"]}}},
            {"type": "function", "function": {"name": "run_workspace_command", "description": "Run a Windows PowerShell command in the workspace. Use PowerShell syntax, not bash commands such as grep or shell comments.", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
        ]

    def _request_stream(self, messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        base_url = self.state.base_url.rstrip("/")
        body: dict[str, Any] = {
            "model": self.state.model,
            "messages": messages,
            "tools": self.tools_schema(),
            "stream": True,
            "keep_alive": self.state.keep_alive,
        }
        if self.state.context_length > 0:
            body["options"] = {"num_ctx": self.state.context_length}
        payload = json.dumps(body).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/x-ndjson"}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"
        request = urllib.request.Request(f"{base_url}/chat", data=payload, headers=headers, method="POST")
        content_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        context = None
        if self.state.base_url.lower().startswith("https://"):
            context = ssl.create_default_context(cafile=self.state.ca_bundle_path or None)
            if not self.state.verify_tls:
                context.check_hostname = False
                context.verify_mode = ssl.CERT_NONE
        self.response = urllib.request.urlopen(request, timeout=self.state.request_timeout_seconds, context=context)
        try:
            for raw_line in self.response:
                if self._stop_requested.is_set():
                    raise RuntimeError("Ollama run stopped")
                try:
                    chunk = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._log(f"Ollama stream parse warning: {exc}", "warning")
                    continue
                message = chunk.get("message") if isinstance(chunk.get("message"), dict) else {}
                content = message.get("content")
                if isinstance(content, str) and content:
                    content_parts.append(content)
                calls = message.get("tool_calls")
                if isinstance(calls, list):
                    tool_calls.extend(call for call in calls if isinstance(call, dict))
                if chunk.get("done"):
                    break
        finally:
            self.response.close()
            self.response = None
        return "".join(content_parts), tool_calls

    def _run(self) -> None:
        if not self.state.model.strip():
            self.on_done(1, "Ollama requires a model. Select or enter one in Execution Settings.")
            return
        messages: list[dict[str, Any]] = [{"role": "user", "content": self.state.prompt}]
        self.on_event({"type": "thread.started", "thread_id": f"ollama-{uuid.uuid4()}"})
        self._log(f"Starting Ollama model {self.state.model} at {self.state.base_url.rstrip('/')}")
        try:
            for iteration in range(16):
                content, tool_calls = self._request_stream(messages)
                assistant_message: dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_message["tool_calls"] = tool_calls
                messages.append(assistant_message)
                if not tool_calls:
                    artifact = Path(self.state.expected_artifact_path)
                    artifact_ready = artifact.is_file() and (
                        not self.state.expected_artifact_fingerprint
                        or self._file_fingerprint(artifact) != self.state.expected_artifact_fingerprint
                    )
                    if self.state.expected_artifact_path and not artifact_ready:
                        self._log(
                            f"Required artifact is still missing: {self.state.expected_artifact_path}. Asking the model to create it.",
                            "warning",
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                f"You have not created the required artifact at {self.state.expected_artifact_path}. "
                                "Use write_workspace_file now to create or update it. The path argument must be a relative file path, "
                                "such as artifacts/PLAN.md. Do not finish with a text-only answer."
                            ),
                        })
                        continue
                    self.on_event({"type": "item.completed", "item": {"type": "agent_message", "text": content}})
                    self.on_event({"type": "turn.completed"})
                    self.on_done(0, "")
                    return
                for call in tool_calls:
                    function = call.get("function") if isinstance(call.get("function"), dict) else {}
                    name = str(function.get("name") or "")
                    arguments = function.get("arguments") if isinstance(function.get("arguments"), dict) else {}
                    if name == "run_workspace_command":
                        self.on_event({"type": "item.started", "item": {"type": "command_execution", "command": arguments.get("command") or "command"}})
                    else:
                        self.on_event({"type": "item.started", "item": {"type": "mcp_tool_call", "name": name}})
                    result = self.execute_tool(name, arguments)
                    messages.append({"role": "tool", "tool_name": name, "content": result})
                    if name == "run_workspace_command":
                        exit_code = 1
                        if result.startswith("exit="):
                            try:
                                exit_code = int(result.split("\n", 1)[0].split("=", 1)[1])
                            except ValueError:
                                pass
                        self.on_event({"type": "item.completed", "item": {"type": "command_execution", "command": arguments.get("command") or "command", "exit_code": exit_code, "output": result}})
                    else:
                        self.on_event({"type": "item.completed", "item": {"type": "mcp_tool_call", "name": name, "output": result}})
            self.on_done(1, "Ollama tool loop exceeded 16 iterations.")
        except urllib.error.HTTPError as exc:
            self.on_done(1, f"Ollama HTTP {exc.code}: {exc.reason}")
        except urllib.error.URLError as exc:
            self.on_done(1, f"Could not connect to Ollama: {exc.reason}")
        except Exception as exc:
            self.on_done(1, str(exc))

    @staticmethod
    def _file_fingerprint(path: Path) -> str:
        try:
            stat = path.stat()
            return f"{stat.st_mtime_ns}:{stat.st_size}"
        except OSError:
            return ""


def handoff_marker(role_name: str) -> str:
    normalized = role_name.strip().upper().replace(" ", "_")
    return f"[[HANDOFF_TO_{normalized}]]"


def extract_latest_valid_handoff(buffer: str, marker: str) -> str:
    candidates = extract_handoff_candidates(buffer, marker)
    for _start, body, between_end_and_done in reversed(candidates):
        if len(between_end_and_done) > 80:
            continue
        return body
    return ""


def extract_handoff_candidates(buffer: str, marker: str) -> list[tuple[int, str, str]]:
    candidates: list[tuple[int, str, str]] = []
    search_from = 0
    while search_from < len(buffer):
        start = buffer.find(marker, search_from)
        if start == -1:
            break
        body_start = start + len(marker)
        end = buffer.find(HANDOFF_END, body_start)
        if end == -1:
            break
        done = buffer.find(HANDOFF_DONE, end + len(HANDOFF_END))
        if done == -1:
            search_from = end + len(HANDOFF_END)
            continue
        between_end_and_done = buffer[end + len(HANDOFF_END):done].strip()
        body = buffer[body_start:end].strip()
        candidates.append((start, body, between_end_and_done))
        search_from = done + len(HANDOFF_DONE)
    return candidates


def handoff_rejection_reason(body: str) -> str:
    trimmed = body.strip()
    if len(trimmed) < HANDOFF_MIN_LENGTH:
        return f"too-short:{len(trimmed)}"
    if len(trimmed) > HANDOFF_MAX_LENGTH:
        return f"too-long:{len(trimmed)}"
    if trimmed.lower() == "and":
        return "template-fragment"
    if "Direct message to the implementer:\nFirst stopping point:\nEvidence/tests expected:" in trimmed:
        return "placeholder-template"
    return ""


def has_approved_done(buffer: str) -> bool:
    return APPROVED in buffer and HANDOFF_DONE in buffer[buffer.rfind(APPROVED):]


@dataclass
class Handoff:
    source_role: str
    target_role: str
    marker: str
    body: str


@dataclass
class AgentRole:
    name: str
    artifact_name: str
    prompt: str
    status: str = "queued"
    session_id: str = ""
    allowed_handoffs: list[str] | None = None
    model: str = ""
    model_reasoning_effort: str = ""
    provider: str = ""
    session_provider: str = ""


@dataclass
class WorkflowState:
    goal: str = (
        "Build a compact multi-agent Codex workflow that plans, reviews, implements, "
        "tests, and archives through markdown handoffs."
    )
    codex_cmd: str = field(default_factory=resolve_codex_command)
    claude_cmd: str = field(default_factory=resolve_claude_command)
    provider: str = "Codex"
    ollama_mode: str = "local"
    ollama_base_url: str = "http://localhost:11434/api"
    ollama_keep_alive: str = "1h"
    ollama_request_timeout_seconds: int = 600
    ollama_command_timeout_seconds: int = 120
    ollama_verify_tls: bool = True
    ollama_ca_bundle_path: str = ""
    ollama_context_length: int = 0
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    model: str = ""
    sandbox: str = "workspace-write"
    extra_args: str = ""
    bypass_approvals_and_sandbox: bool = True
    skip_git_check: bool = True
    ephemeral: bool = False
    auto_advance: bool = True
    current_role_index: int = 0


SESSION_FILE_FILTER = "Codex Workbench Session (*.codex-workbench.json);;JSON Files (*.json);;All Files (*)"
SESSION_FILE_SUFFIX = ".codex-workbench.json"
SESSION_INDEX_FILE_NAME = "saved_sessions.json"
SESSION_DIR_NAME = ".codex-workbench-sessions"


ROLE_PROMPTS = [
    AgentRole(
        "Planner",
        "PLAN.md",
        (
            "Create artifacts/PLAN.md from the user's goal. First inspect the local workspace, then do current research "
            "when the task depends on modern tools, libraries, APIs, design patterns, or other fast-moving technology. "
            "Use authoritative sources where possible, identify what current advanced approaches exist, and fold the "
            "findings into the plan. Include concrete steps, role handoffs, risks, source notes, and stop conditions."
        ),
    ),
    AgentRole(
        "Reviewer",
        "REVIEW.md",
        "Read artifacts/PLAN.md. Write artifacts/REVIEW.md with findings, gaps, risks, and approval status.",
    ),
    AgentRole(
        "Implementer",
        "IMPLEMENTATION_NOTES.md",
        (
            "Read artifacts/PLAN.md and artifacts/REVIEW.md. Make the smallest code changes needed. "
            "Write artifacts/IMPLEMENTATION_NOTES.md with what changed and what remains."
        ),
    ),
    AgentRole(
        "Tester",
        "TEST_RESULTS.md",
        "Run appropriate verification. Write artifacts/TEST_RESULTS.md with commands, results, failures, and next steps.",
    ),
    AgentRole(
        "Archivist",
        "SUMMARY.md",
        "Read the workflow artifacts and write artifacts/SUMMARY.md with the final outcome and important decisions.",
    ),
]

ARTIFACT_SPECS = [
    ArtifactSpec("PLAN.md", "PLAN.md"),
    ArtifactSpec("REVIEW.md", "REVIEW.md"),
    ArtifactSpec("IMPLEMENTATION_NOTES.md", "IMPLEMENTATION"),
    ArtifactSpec("TEST_RESULTS.md", "TESTS"),
    ArtifactSpec("SUMMARY.md", "SUMMARY.md"),
]

def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_dir() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).resolve()


LIBRARY_FILE = app_dir() / "role_presets.json"
BUNDLED_LIBRARY_FILE = bundle_dir() / "role_presets.json"

# Roles are saved independently, keyed by name. Presets are just named
# orderings of role names that reference this shared library.

MAINTAINABILITY_REVIEWER_PROMPT = (
    "Read artifacts/IMPLEMENTATION_NOTES.md and inspect the actual diff/repo (not just the other agents' "
    "summaries). Audit the change for the architectural failure patterns that make LLM-generated code hard to "
    "maintain, even when it passes tests. For each pattern, actively look for it in the real files touched; do "
    "not just restate the Implementer's own description.\n\n"
    "Failure patterns to check:\n"
    "1. Duplication instead of reuse - reimplemented validation, formatting, parsing, API wrappers, or "
    "components that already exist elsewhere in the repo.\n"
    "2. God files / multi-responsibility functions - large files or functions mixing validation, persistence, "
    "logging, formatting, and UI/state in one place; growing conditional branches per feature.\n"
    "3. Modular mirage - code that looks modular (services/managers/adapters/providers) but isn't: layers that "
    "import backward, interfaces with only one implementation, pass-through services, manager classes that do "
    "everything.\n"
    "4. Code in the wrong layer - business rules in UI, DB calls in route handlers instead of "
    "repositories/services, domain objects doing formatting, dependency direction violations.\n"
    "5. Shallow or cargo-culted abstractions - new classes/wrappers (e.g. XManager, DataProcessor, "
    "ConfigHandler) that don't isolate change, hide complexity, reduce coupling, or clarify ownership.\n"
    "6. Happy-path logic and swallowed errors - bare except/catch blocks, exceptions logged and discarded, "
    "error handling that masks the actual failure cause.\n"
    "7. Version/dependency confusion - unnecessary new dependencies, deprecated APIs, mixed library-version "
    "patterns, assumptions that don't match the repo's actual lockfile/manifest/framework version.\n"
    "8. Weak tests - tests that only cover the happy path, assert implementation details or exact internal call "
    "order, or would pass against almost any implementation rather than the intended behavior/contract.\n\n"
    "Also answer the reuse-audit questions directly: Did it reuse the existing domain model? Did it duplicate "
    "validation/formatting/parsing/API access? Is the code in the right layer? Does each new abstraction earn "
    "its existence? Was dependency direction preserved? Did it delete obsolete code, or only add more? Could a "
    "future developer understand why this change exists without asking the AI?\n\n"
    "Write artifacts/MAINTAINABILITY_REVIEW.md with: a findings list (pattern, file/location, description, "
    "severity: blocking/major/minor, concrete recommended fix), direct answers to the reuse-audit questions "
    "above, and an overall maintainability verdict (clean / needs follow-up refactor / blocking) with a short "
    "rationale. If you find nothing significant, say so explicitly rather than leaving the report empty."
)


def default_role_library() -> dict[str, dict[str, Any]]:
    roles: dict[str, dict[str, Any]] = {
        role.name: {"artifact_name": role.artifact_name, "prompt": role.prompt} for role in ROLE_PROMPTS
    }
    roles.update(
        {
            "Summarizer": {
                "artifact_name": "SUMMARY_OF_PAPER.md",
                "prompt": (
                    "Read the target paper (path or URL given in the goal). Write artifacts/SUMMARY_OF_PAPER.md "
                    "summarizing the claimed contributions, methodology, datasets/experiments, and headline results."
                ),
            },
            "Critic": {
                "artifact_name": "CRITIQUE.md",
                "prompt": (
                    "Read artifacts/SUMMARY_OF_PAPER.md and the paper itself. Write artifacts/CRITIQUE.md identifying "
                    "methodological weaknesses, unsupported claims, missing baselines/comparisons, and threats to validity."
                ),
            },
            "Reproducibility Checker": {
                "artifact_name": "REPRODUCIBILITY.md",
                "prompt": (
                    "Read artifacts/SUMMARY_OF_PAPER.md and artifacts/CRITIQUE.md. Write artifacts/REPRODUCIBILITY.md "
                    "assessing whether the experiments and any released code/data are reproducible, and what's missing."
                ),
            },
            "Synthesizer": {
                "artifact_name": "REVIEW_REPORT.md",
                "prompt": (
                    "Read all prior artifacts. Write artifacts/REVIEW_REPORT.md with a final review: strengths, "
                    "weaknesses, and a recommendation (accept / minor revision / major revision / reject) with rationale."
                ),
            },
            "Scoper": {
                "artifact_name": "RESEARCH_SCOPE.md",
                "prompt": (
                    "From the user's goal, write artifacts/RESEARCH_SCOPE.md defining the research question, "
                    "background/context, what's already known, and what would count as a satisfying answer."
                ),
            },
            "Investigator": {
                "artifact_name": "FINDINGS.md",
                "prompt": (
                    "Read artifacts/RESEARCH_SCOPE.md. Gather information (search, read docs/sources, run experiments "
                    "as needed) and write artifacts/FINDINGS.md documenting what you found, with sources."
                ),
            },
            "Analyst": {
                "artifact_name": "ANALYSIS.md",
                "prompt": (
                    "Read artifacts/RESEARCH_SCOPE.md and artifacts/FINDINGS.md. Write artifacts/ANALYSIS.md analyzing "
                    "the findings: patterns, disagreements between sources, gaps, and implications."
                ),
            },
            "Reporter": {
                "artifact_name": "RESEARCH_REPORT.md",
                "prompt": (
                    "Read all prior artifacts. Write artifacts/RESEARCH_REPORT.md with a final, well-cited report "
                    "answering the research question, plus open questions and suggested next steps."
                ),
            },
            "Spec Writer": {
                "artifact_name": "SPEC.md",
                "prompt": (
                    "From the user's goal, inspect the local workspace and write artifacts/SPEC.md describing the "
                    "concrete change: scope, affected files/modules, edge cases, and out-of-scope items."
                ),
            },
            "Code Implementer": {
                "artifact_name": "IMPLEMENTATION_NOTES.md",
                "prompt": (
                    "Read artifacts/SPEC.md. Make the smallest code changes needed to satisfy it. "
                    "Write artifacts/IMPLEMENTATION_NOTES.md with what changed and what remains."
                ),
            },
            "Test Writer": {
                "artifact_name": "TEST_RESULTS.md",
                "prompt": (
                    "Read artifacts/SPEC.md and artifacts/IMPLEMENTATION_NOTES.md. Run or write appropriate tests. "
                    "Write artifacts/TEST_RESULTS.md with commands, results, failures, and next steps."
                ),
            },
            "Code Reviewer": {
                "artifact_name": "CODE_REVIEW.md",
                "prompt": (
                    "Read all prior artifacts and the actual diff. Write artifacts/CODE_REVIEW.md with findings, "
                    "risks, and an approval status."
                ),
            },
            "Maintainability Reviewer": {
                "artifact_name": "MAINTAINABILITY_REVIEW.md",
                "prompt": MAINTAINABILITY_REVIEWER_PROMPT,
            },
            "Section Planner": {
                "artifact_name": "SECTIONS_INDEX.md",
                "prompt": (
                    "Inspect the workspace and break the user's goal into small, independently implementable sections. "
                    "Create artifacts/sections/INDEX.md with ordered section ids, titles, scope, dependencies, acceptance criteria, "
                    "and status: pending. Create artifacts/sections/01-<slug>/PLAN.md for every section. Create "
                    "artifacts/devs/DEVIATIONS.md with a heading and an empty dated-entry template. Write artifacts/SECTIONS_INDEX.md "
                    "summarizing the section plan. Do not implement product changes. Do not use a standalone handoff file as a substitute for "
                    "the workbench protocol. In your final response, use the Section Manager handoff marker with the first pending section id."
                ),
                "allowed_handoffs": ["Section Manager"],
            },
            "Section Manager": {
                "artifact_name": "MANAGER_STATUS.md",
                "prompt": (
                    "Act as the sole coordinator for the sectioned implementation workflow. Read artifacts/sections/INDEX.md, "
                    "the per-section artifacts, artifacts/devs/DEVIATIONS.md, and the relayed message. Select exactly one pending "
                    "or returned section. Write artifacts/MANAGER_STATUS.md with the active section, its status, evidence, and next action. "
                    "Only you may dispatch a new section. Send the active section id, exact scope, acceptance criteria, and relevant paths "
                    "to Section Implementer. After receiving reviewer/tester approval, mark that section complete in INDEX.md before moving "
                    "to the next pending section. Record material scope/design deviations in artifacts/devs/DEVIATIONS.md. Once every section "
                    "is complete and approved, hand off to Section Archivist instead of starting another section."
                ),
                "allowed_handoffs": ["Section Implementer", "Section Archivist"],
            },
            "Section Implementer": {
                "artifact_name": "SECTION_IMPLEMENTATION.md",
                "prompt": (
                    "Implement only the section identified by Section Manager. Read its artifacts/sections/<section>/PLAN.md and do not "
                    "expand into another section. Update the product files, then create or update that section's IMPLEMENTATION.md with files "
                    "changed, behavior delivered, tests to run, and deviations. Also write artifacts/SECTION_IMPLEMENTATION.md with the active "
                    "section id and a concise implementation summary. If the plan cannot be followed, document the proposed deviation "
                    "in artifacts/devs/DEVIATIONS.md and explain it to the reviewer. Hand off to Section Reviewer with the section id and evidence."
                ),
                "allowed_handoffs": ["Section Reviewer"],
            },
            "Section Reviewer": {
                "artifact_name": "SECTION_REVIEW.md",
                "prompt": (
                    "Review only the active section against its PLAN.md and the real workspace diff. Write or update that section's REVIEW.md "
                    "with blocking findings, non-blocking findings, and an explicit verdict. Also write artifacts/SECTION_REVIEW.md with the active "
                    "section id and verdict. On a blocking finding, hand off to Section Implementer "
                    "with the section id and precise required correction. If code review passes, hand off to Section Tester with the section id, "
                    "acceptance criteria, and verification focus. Do not approve unrelated sections."
                ),
                "allowed_handoffs": ["Section Implementer", "Section Tester"],
            },
            "Section Tester": {
                "artifact_name": "SECTION_TESTS.md",
                "prompt": (
                    "Test only the active section using its PLAN.md acceptance criteria and the implementation/review artifacts. Write or update "
                    "that section's TESTS.md with commands, observed results, failures, and an explicit verdict. Also write artifacts/SECTION_TESTS.md "
                    "with the active section id and verdict. On failure, hand off to Section "
                    "Implementer with reproducible evidence. On success, hand off to Section Manager with the section id, approval, and evidence. "
                    "Do not start another section."
                ),
                "allowed_handoffs": ["Section Implementer", "Section Manager"],
            },
            "Section Archivist": {
                "artifact_name": "SECTION_SUMMARY.md",
                "prompt": (
                    "Read artifacts/sections/INDEX.md, every section folder, and artifacts/devs/DEVIATIONS.md. Verify that every indexed section "
                    "is marked complete with review and test evidence. Write artifacts/SECTION_SUMMARY.md with delivered sections, deviations, "
                    "verification evidence, and any remaining risks. Do not make product changes or dispatch further work."
                ),
                "allowed_handoffs": [],
            },
        }
    )
    return roles


def default_presets() -> dict[str, list[str]]:
    return {
        "Default Pipeline": [role.name for role in ROLE_PROMPTS],
        "Paper Review": ["Summarizer", "Critic", "Reproducibility Checker", "Synthesizer"],
        "Research": ["Scoper", "Investigator", "Analyst", "Reporter"],
        "Code Implementation": [
            "Spec Writer",
            "Code Implementer",
            "Test Writer",
            "Maintainability Reviewer",
            "Code Reviewer",
        ],
        "Sectioned Implementation": [
            "Section Planner",
            "Section Manager",
            "Section Implementer",
            "Section Reviewer",
            "Section Tester",
            "Section Archivist",
        ],
    }


def _migrate_legacy_library(data: dict[str, Any]) -> tuple[dict[str, dict[str, str]], dict[str, list[str]]]:
    """Old format stored full role dicts inline per preset. Flatten into a shared role library."""
    roles: dict[str, dict[str, str]] = {}
    presets: dict[str, list[str]] = {}
    for preset_name, items in data.items():
        if not isinstance(items, list):
            continue
        order: list[str] = []
        for item in items:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            name = str(item["name"])
            artifact_name, _warning = safe_artifact_name(str(item.get("artifact_name") or ""), name)
            roles[name] = {
                "artifact_name": artifact_name,
                "prompt": str(item.get("prompt") or ""),
            }
            order.append(name)
        if order:
            presets[preset_name] = order
    return roles, presets


def load_library() -> tuple[dict[str, dict[str, Any]], dict[str, list[str]]]:
    data: dict[str, Any] = {}
    source_file = LIBRARY_FILE if LIBRARY_FILE.exists() else BUNDLED_LIBRARY_FILE
    if source_file.exists():
        try:
            loaded = json.loads(source_file.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except (OSError, json.JSONDecodeError):
            data = {}

    if isinstance(data.get("roles"), dict) and isinstance(data.get("presets"), dict):
        roles = dict(data["roles"])
        presets = {name: list(order) for name, order in data["presets"].items()}
    elif data:
        roles, presets = _migrate_legacy_library(data)
        save_library(roles, presets)
    else:
        roles, presets = default_role_library(), default_presets()
        save_library(roles, presets)

    builtin_roles = default_role_library()
    builtin_presets = default_presets()
    for name, entry in builtin_roles.items():
        roles.setdefault(name, dict(entry))
    for name, order in builtin_presets.items():
        presets.setdefault(name, list(order))
    for name, entry in list(roles.items()):
        if not isinstance(entry, dict):
            roles[name] = {"artifact_name": fallback_artifact_name(name), "prompt": ""}
            continue
        artifact_name, _warning = safe_artifact_name(str(entry.get("artifact_name") or ""), name)
        entry["artifact_name"] = artifact_name
        entry["prompt"] = str(entry.get("prompt") or "")

    return roles, presets


def save_library(roles: dict[str, dict[str, Any]], presets: dict[str, list[str]]) -> None:
    safe_roles: dict[str, dict[str, Any]] = {}
    for name, entry in roles.items():
        safe_entry = dict(entry)
        artifact_name, _warning = safe_artifact_name(str(safe_entry.get("artifact_name") or ""), name)
        safe_entry["artifact_name"] = artifact_name
        safe_roles[name] = safe_entry
    atomic_write_json(LIBRARY_FILE, {"roles": safe_roles, "presets": presets})


def optional_string_list(data: dict[str, Any], key: str) -> list[str] | None:
    if key not in data:
        return None
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def role_library_entry(role: AgentRole) -> dict[str, Any]:
    artifact_name, _warning = safe_artifact_name(role.artifact_name, role.name)
    entry: dict[str, Any] = {
        "artifact_name": artifact_name,
        "prompt": role.prompt,
    }
    if role.model.strip():
        entry["model"] = role.model.strip()
    if role.model_reasoning_effort.strip():
        entry["model_reasoning_effort"] = role.model_reasoning_effort.strip()
    if role.provider in PROVIDERS:
        entry["provider"] = role.provider
    if role.allowed_handoffs is not None:
        entry["allowed_handoffs"] = list(role.allowed_handoffs)
    return entry


def role_to_dict(role: AgentRole) -> dict[str, Any]:
    artifact_name, _warning = safe_artifact_name(role.artifact_name, role.name)
    data = {
        "name": role.name,
        "artifact_name": artifact_name,
        "prompt": role.prompt,
        "status": role.status,
        "session_id": role.session_id,
        "session_provider": role.session_provider,
    }
    if role.model.strip():
        data["model"] = role.model.strip()
    if role.model_reasoning_effort.strip():
        data["model_reasoning_effort"] = role.model_reasoning_effort.strip()
    if role.provider in PROVIDERS:
        data["provider"] = role.provider
    if role.allowed_handoffs is not None:
        data["allowed_handoffs"] = list(role.allowed_handoffs)
    return data


def role_from_dict(data: dict[str, Any]) -> AgentRole | None:
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    artifact_name, _warning = safe_artifact_name(str(data.get("artifact_name") or ""), name)
    return AgentRole(
        name=name,
        artifact_name=artifact_name,
        prompt=str(data.get("prompt") or ""),
        status=str(data.get("status") or "queued"),
        session_id=str(data.get("session_id") or ""),
        session_provider=str(data.get("session_provider") or ("Codex" if data.get("session_id") else "")),
        allowed_handoffs=optional_string_list(data, "allowed_handoffs"),
        model=str(data.get("model") or ""),
        model_reasoning_effort=str(data.get("model_reasoning_effort") or ""),
        provider=str(data.get("provider") or "") if str(data.get("provider") or "") in PROVIDERS else "",
    )


def workflow_state_to_dict(state: WorkflowState) -> dict[str, Any]:
    return {
        "goal": state.goal,
        "codex_cmd": state.codex_cmd,
        "claude_cmd": state.claude_cmd,
        "provider": state.provider,
        "ollama_mode": state.ollama_mode,
        "ollama_base_url": state.ollama_base_url,
        "ollama_keep_alive": state.ollama_keep_alive,
        "ollama_request_timeout_seconds": state.ollama_request_timeout_seconds,
        "ollama_command_timeout_seconds": state.ollama_command_timeout_seconds,
        "ollama_verify_tls": state.ollama_verify_tls,
        "ollama_ca_bundle_path": state.ollama_ca_bundle_path,
        "ollama_context_length": state.ollama_context_length,
        "cwd": state.cwd,
        "model": state.model,
        "sandbox": state.sandbox,
        "extra_args": state.extra_args,
        "bypass_approvals_and_sandbox": state.bypass_approvals_and_sandbox,
        "skip_git_check": state.skip_git_check,
        "ephemeral": state.ephemeral,
        "auto_advance": state.auto_advance,
        "current_role_index": state.current_role_index,
    }


def workflow_state_from_dict(data: dict[str, Any]) -> WorkflowState:
    state = WorkflowState()
    state.goal = str(data.get("goal") or state.goal)
    state.codex_cmd = str(data.get("codex_cmd") or state.codex_cmd)
    state.claude_cmd = str(data.get("claude_cmd") or state.claude_cmd)
    provider = str(data.get("provider") or state.provider)
    state.provider = provider if provider in PROVIDERS else "Codex"
    state.ollama_mode = str(data.get("ollama_mode") or "local")
    state.ollama_base_url = str(data.get("ollama_base_url") or state.ollama_base_url)
    state.ollama_keep_alive = str(data.get("ollama_keep_alive") or state.ollama_keep_alive)
    try:
        state.ollama_request_timeout_seconds = max(10, int(data.get("ollama_request_timeout_seconds", 600)))
    except (TypeError, ValueError):
        state.ollama_request_timeout_seconds = 600
    try:
        state.ollama_command_timeout_seconds = max(10, int(data.get("ollama_command_timeout_seconds", 120)))
    except (TypeError, ValueError):
        state.ollama_command_timeout_seconds = 120
    state.ollama_verify_tls = bool(data.get("ollama_verify_tls", True))
    state.ollama_ca_bundle_path = str(data.get("ollama_ca_bundle_path") or "")
    try:
        state.ollama_context_length = max(0, int(data.get("ollama_context_length", 0)))
    except (TypeError, ValueError):
        state.ollama_context_length = 0
    state.cwd = str(data.get("cwd") or state.cwd)
    state.model = str(data.get("model") or "")
    state.sandbox = str(data.get("sandbox") or state.sandbox)
    state.extra_args = str(data.get("extra_args") or "")
    state.bypass_approvals_and_sandbox = bool(
        data.get("bypass_approvals_and_sandbox", state.bypass_approvals_and_sandbox)
    )
    state.skip_git_check = bool(data.get("skip_git_check", state.skip_git_check))
    state.ephemeral = bool(data.get("ephemeral", state.ephemeral))
    state.auto_advance = bool(data.get("auto_advance", state.auto_advance))
    try:
        state.current_role_index = int(data.get("current_role_index", 0))
    except (TypeError, ValueError):
        state.current_role_index = 0
    return state


def handoff_to_dict(handoff: Handoff | None) -> dict[str, str] | None:
    if handoff is None:
        return None
    return {
        "source_role": handoff.source_role,
        "target_role": handoff.target_role,
        "marker": handoff.marker,
        "body": handoff.body,
    }


def handoff_from_dict(data: Any) -> Handoff | None:
    if not isinstance(data, dict):
        return None
    source_role = str(data.get("source_role") or "")
    target_role = str(data.get("target_role") or "")
    body = str(data.get("body") or "")
    if not source_role or not target_role or not body:
        return None
    marker = str(data.get("marker") or handoff_marker(target_role))
    return Handoff(source_role, target_role, marker, body)


def workspace_path(cwd: str) -> Path:
    return Path(cwd.strip() or str(app_dir())).expanduser()


def session_dir_for_workspace(workspace: Path) -> Path:
    return workspace / SESSION_DIR_NAME


def session_index_file(workspace: Path) -> Path:
    return session_dir_for_workspace(workspace) / SESSION_INDEX_FILE_NAME


def path_key(path: Path) -> str:
    return str(path).lower()


def path_for_session_index(workspace: Path, path: Path) -> str:
    safe_path = contained_session_path(workspace, path)
    if safe_path is None:
        return ""
    return str(safe_path.relative_to(workspace.resolve(strict=False)))


def load_session_index(workspace: Path, warn: Callable[[str], None] | None = None) -> list[Path]:
    try:
        loaded = json.loads(session_index_file(workspace).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        if warn:
            warn(f"Session index is malformed and was ignored: {exc}")
        loaded = {}
    except OSError:
        loaded = {}
    if isinstance(loaded, dict):
        paths = loaded.get("sessions")
    else:
        paths = loaded
    if not isinstance(paths, list):
        paths = []

    seen: set[str] = set()
    result: list[Path] = []
    for item in paths:
        path_text = str(item or "").strip()
        if not path_text:
            continue
        path = contained_session_path(workspace, Path(path_text))
        if path is None:
            if warn:
                warn(f"Unsafe session index entry dropped: {path_text}")
            continue
        key = path_key(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    session_dir = session_dir_for_workspace(workspace)
    try:
        discovered = sorted(session_dir.glob(f"*{SESSION_FILE_SUFFIX}"), key=lambda item: item.stat().st_mtime, reverse=True)
    except OSError:
        discovered = []
    for discovered_path in discovered:
        path = contained_session_path(workspace, discovered_path)
        if path is None:
            continue
        key = path_key(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def save_session_index(workspace: Path, paths: list[Path]) -> None:
    index_file = session_index_file(workspace)
    safe_paths = [path_for_session_index(workspace, path) for path in paths]
    payload = {"sessions": [path for path in safe_paths if path]}
    atomic_write_json(index_file, payload)


def session_slug(text: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:48] or "session"


def bounded_command_output(output: str) -> tuple[str, bool]:
    if len(output) <= MAX_COMMAND_OUTPUT_SAVE_CHARS:
        return output, False
    # Keep head and tail; errors and summaries usually appear at the end.
    half = MAX_COMMAND_OUTPUT_SAVE_CHARS // 2
    omitted = len(output) - half * 2
    marker = f"\n...[{omitted:,} chars omitted by workbench output cap]...\n"
    return output[:half] + marker + output[-half:], True


def fallback_artifact_name(role_name: str) -> str:
    return f"{session_slug(role_name)}.md"


def validate_artifact_name(name: str) -> str:
    candidate = str(name or "").strip()
    win = PureWindowsPath(candidate)
    if (
        not candidate
        or Path(candidate).is_absolute()
        or win.is_absolute()
        or win.drive
        or win.root
        or "/" in candidate
        or "\\" in candidate
    ):
        raise ValueError("artifact name must be a relative file name")
    parts = Path(candidate).parts
    if len(parts) != 1 or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("artifact name must not contain traversal or path separators")
    return candidate


def safe_artifact_name(name: str, role_name: str) -> tuple[str, str | None]:
    try:
        return validate_artifact_name(name), None
    except ValueError as exc:
        fallback = fallback_artifact_name(role_name)
        return fallback, f"Invalid artifact name for {role_name!r}: {name!r} ({exc}); using {fallback!r}."


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def contained_session_path(workspace: Path, path: Path) -> Path | None:
    session_dir = session_dir_for_workspace(workspace)
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    try:
        resolved = candidate.resolve(strict=False)
        resolved_dir = session_dir.resolve(strict=False)
        resolved.relative_to(resolved_dir)
    except (OSError, ValueError):
        return None
    if resolved.name == SESSION_INDEX_FILE_NAME or not resolved.name.endswith(SESSION_FILE_SUFFIX):
        return None
    return resolved


MONO_FONT_FAMILY = "Consolas"

STYLESHEET = """
QMainWindow { background: #1e1f22; }
QWidget { background: #1e1f22; color: #d6d6d6; }
QGroupBox {
    border: 1px solid #3a3b3f; border-radius: 8px; margin-top: 10px; padding: 10px; font-weight: 600;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
QPlainTextEdit, QTextEdit {
    background: #16171a; color: #d6d6d6; border: 1px solid #3a3b3f; border-radius: 6px;
}
QLineEdit, QComboBox {
    background: #16171a; color: #d6d6d6; border: 1px solid #3a3b3f; border-radius: 4px; padding: 3px 6px;
}
QPushButton {
    background: #2b2d31; color: #d6d6d6; border: 1px solid #3a3b3f; border-radius: 5px; padding: 5px 12px;
}
QPushButton:hover { background: #34363b; }
QPushButton:pressed { background: #24262a; }
QPushButton#primary { background: #4f8cff; color: #0b0c0e; font-weight: 700; border: none; }
QPushButton#primary:hover { background: #6a9dff; }
QLabel#section { font-weight: 700; }
QLabel#status { color: #9a9ba0; }
QTabWidget::pane { border: 1px solid #3a3b3f; border-radius: 6px; }
QTabBar::tab { background: #2b2d31; padding: 6px 12px; border: 1px solid #3a3b3f; border-bottom: none; }
QTabBar::tab:selected { background: #16171a; }
QStatusBar { background: #16171a; border-top: 1px solid #3a3b3f; }
QCheckBox { spacing: 6px; }
"""


def mono_textedit(read_only: bool = True, wrap: bool = True) -> QtWidgets.QPlainTextEdit:
    widget = QtWidgets.QPlainTextEdit()
    widget.setReadOnly(read_only)
    widget.setFont(QtGui.QFont(MONO_FONT_FAMILY, 10))
    widget.setLineWrapMode(
        QtWidgets.QPlainTextEdit.LineWrapMode.WidgetWidth
        if wrap
        else QtWidgets.QPlainTextEdit.LineWrapMode.NoWrap
    )
    return widget


class WorkbenchSignals(QtCore.QObject):
    event_ready = QtCore.pyqtSignal(int, dict)
    log_ready = QtCore.pyqtSignal(int, str, str)
    done_ready = QtCore.pyqtSignal(int, int, str)


SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


class StatusIndicator(QtWidgets.QLabel):
    """Small glyph that spins while an agent is active and freezes into a
    checkmark/cross/dot once the run settles, so state is visible at a glance."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedWidth(20)
        self.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.setFont(QtGui.QFont(MONO_FONT_FAMILY, 13))
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(80)
        self._timer.timeout.connect(self._tick)
        self._frame = 0
        self.set_idle()

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(SPINNER_FRAMES)
        self.setText(SPINNER_FRAMES[self._frame])

    def set_busy(self) -> None:
        self.setStyleSheet("color: #4f8cff; font-weight: 700;")
        if not self._timer.isActive():
            self._timer.start()

    def set_done(self) -> None:
        self._timer.stop()
        self.setText("✓")
        self.setStyleSheet("color: #43b581; font-weight: 700;")

    def set_error(self) -> None:
        self._timer.stop()
        self.setText("✗")
        self.setStyleSheet("color: #e05252; font-weight: 700;")

    def set_waiting(self) -> None:
        self._timer.stop()
        self.setText("●")
        self.setStyleSheet("color: #d7a83a; font-weight: 700;")

    def set_idle(self) -> None:
        self._timer.stop()
        self.setText("○")
        self.setStyleSheet("color: #6d6f75; font-weight: 700;")


class MultiAgentCodexWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.dependency_status = load_runtime_dependencies()
        if not self.dependency_status.ok:
            raise RuntimeError(self.dependency_status.message)

        self.state = WorkflowState()
        self.roles = [
            AgentRole(
                role.name,
                role.artifact_name,
                role.prompt,
                role.status,
                role.session_id,
                list(role.allowed_handoffs) if role.allowed_handoffs is not None else None,
                role.model,
                role.model_reasoning_effort,
                role.provider,
                role.session_provider,
            )
            for role in ROLE_PROMPTS
        ]
        self.runner: Any = None
        self._ollama_tool_process: subprocess.Popen[str] | None = None
        self._ollama_tool_process_lock = threading.Lock()
        self.active_role: AgentRole | None = None
        self.active_run_id: int | None = None
        self._next_run_id = 0
        self.run_lifecycle = "idle"
        self.run_started_at = 0.0
        self.last_run_error = ""
        self.session_file: Path | None = None

        self.conversation_lines: list[str] = []
        self.activity_lines: list[str] = []
        self.event_lines: list[str] = []
        self.raw_lines: list[str] = []
        self.final_text_by_role: dict[str, str] = {}
        self.agent_buffers: dict[str, str] = {}
        self.seen_handoffs: set[str] = set()
        self.pending_handoff: Handoff | None = None
        self.pending_relay_message = ""
        self._last_artifact_refresh = 0.0
        self.phase = "Idle"
        self.phase_detail = "No agent is running."
        self.artifact_outputs: dict[str, QtWidgets.QPlainTextEdit] = {}
        self.role_library, self.presets = load_library()
        self.current_preset_name = "Default Pipeline" if "Default Pipeline" in self.presets else ""
        self._role_button_widgets: list[QtWidgets.QPushButton] = []
        self._preset_dropdowns: list[QtWidgets.QComboBox] = []
        self.saved_session_paths: list[Path] = []
        self.session_list: QtWidgets.QListWidget | None = None

        self.signals = WorkbenchSignals()
        self.signals.event_ready.connect(self.handle_event)
        self.signals.log_ready.connect(self.handle_runner_log)
        self.signals.done_ready.connect(self.finish_run)

        self.setWindowTitle("Multi-Agent Codex Workbench (PyQt)")
        self.resize(1360, 860)
        self.setStyleSheet(STYLESHEET)

        self._build_ui()
        self.refresh_session_list()
        self._refresh_role_list()
        self.refresh_all_outputs()

    # ------------------------------------------------------------------ UI

    def _build_ui(self) -> None:
        self._build_menu()

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        central_layout = QtWidgets.QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)

        self.main_tabs = QtWidgets.QTabWidget()
        self.main_tabs.addTab(self._build_run_tab(), "Run")
        self.main_tabs.addTab(self._build_agents_tab(), "Agents")
        self.main_tabs.addTab(self._build_ollama_tab(), "Ollama")
        central_layout.addWidget(self.main_tabs)

        self._build_status_bar()

    def _build_run_tab(self) -> QtWidgets.QWidget:
        run_tab = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(run_tab)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        root.addWidget(self._build_sessions_panel(), 0)
        root.addWidget(self._build_sidebar(), 0)
        root.addWidget(self._build_main_column(), 1)
        return run_tab

    def _build_agents_tab(self) -> QtWidgets.QWidget:
        agents_tab = QtWidgets.QWidget()
        root = QtWidgets.QHBoxLayout(agents_tab)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_agents_navigation_panel())
        splitter.addWidget(self._build_role_prompt_tab())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 980])
        root.addWidget(splitter)
        return agents_tab

    def _build_ollama_tab(self) -> QtWidgets.QWidget:
        tab = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(tab)
        layout.setContentsMargins(12, 12, 12, 12)
        connection = QtWidgets.QGroupBox("Connection")
        form = QtWidgets.QFormLayout(connection)

        self.ollama_mode_combo = QtWidgets.QComboBox()
        self.ollama_mode_combo.addItem("Local", "local")
        self.ollama_mode_combo.addItem("Network host", "network")
        self.ollama_mode_combo.setCurrentIndex(0 if self.state.ollama_mode == "local" else 1)
        self.ollama_mode_combo.currentIndexChanged.connect(self.on_ollama_mode_changed)
        form.addRow("Connection mode", self.ollama_mode_combo)

        self.ollama_url_input = QtWidgets.QLineEdit(self.state.ollama_base_url)
        self.ollama_url_input.setPlaceholderText("http://host:11434/api")
        self.ollama_url_input.textChanged.connect(self.on_ollama_url_changed)
        form.addRow("API base URL", self.ollama_url_input)

        self.ollama_token_input = QtWidgets.QLineEdit()
        self.ollama_token_input.setEchoMode(QtWidgets.QLineEdit.EchoMode.Password)
        self.ollama_token_input.setPlaceholderText("Optional bearer token; not saved")
        form.addRow("Bearer token", self.ollama_token_input)

        self.ollama_verify_tls_checkbox = QtWidgets.QCheckBox("Verify HTTPS certificate")
        self.ollama_verify_tls_checkbox.setChecked(self.state.ollama_verify_tls)
        self.ollama_verify_tls_checkbox.toggled.connect(self.on_ollama_verify_tls_changed)
        form.addRow("TLS", self.ollama_verify_tls_checkbox)

        self.ollama_ca_bundle_input = QtWidgets.QLineEdit(self.state.ollama_ca_bundle_path)
        self.ollama_ca_bundle_input.setPlaceholderText("Optional CA bundle path")
        self.ollama_ca_bundle_input.textChanged.connect(self.on_ollama_ca_bundle_changed)
        form.addRow("CA bundle", self.ollama_ca_bundle_input)

        self.ollama_keep_alive_input = QtWidgets.QLineEdit(self.state.ollama_keep_alive)
        self.ollama_keep_alive_input.textChanged.connect(self.on_ollama_keep_alive_changed)
        form.addRow("Keep alive", self.ollama_keep_alive_input)

        self.ollama_timeout_spin = QtWidgets.QSpinBox()
        self.ollama_timeout_spin.setRange(10, 3600)
        self.ollama_timeout_spin.setValue(self.state.ollama_request_timeout_seconds)
        self.ollama_timeout_spin.setSuffix(" seconds")
        self.ollama_timeout_spin.valueChanged.connect(self.on_ollama_timeout_changed)
        form.addRow("Request timeout", self.ollama_timeout_spin)

        self.ollama_command_timeout_spin = QtWidgets.QSpinBox()
        self.ollama_command_timeout_spin.setRange(10, 1800)
        self.ollama_command_timeout_spin.setValue(self.state.ollama_command_timeout_seconds)
        self.ollama_command_timeout_spin.setSuffix(" seconds")
        self.ollama_command_timeout_spin.setToolTip("Maximum time for one model-requested workspace command.")
        self.ollama_command_timeout_spin.valueChanged.connect(self.on_ollama_command_timeout_changed)
        form.addRow("Tool command timeout", self.ollama_command_timeout_spin)

        self.ollama_context_spin = QtWidgets.QSpinBox()
        self.ollama_context_spin.setRange(0, 1_048_576)
        self.ollama_context_spin.setSingleStep(1024)
        self.ollama_context_spin.setValue(self.state.ollama_context_length)
        self.ollama_context_spin.setSpecialValueText("Automatic")
        self.ollama_context_spin.setToolTip("Tokens available to the Ollama model. Automatic uses the server/model default.")
        self.ollama_context_spin.valueChanged.connect(self.on_ollama_context_changed)
        form.addRow("Context window", self.ollama_context_spin)
        layout.addWidget(connection)

        models = QtWidgets.QGroupBox("Models")
        model_layout = QtWidgets.QHBoxLayout(models)
        self.ollama_models_combo = QtWidgets.QComboBox()
        self.ollama_models_combo.setEditable(True)
        self.ollama_models_combo.setCurrentText(self.state.model)
        self.ollama_models_combo.currentTextChanged.connect(self.on_model_changed)
        model_layout.addWidget(self.ollama_models_combo, 1)
        test_button = QtWidgets.QPushButton("Test Connection")
        test_button.clicked.connect(self.test_ollama_connection)
        model_layout.addWidget(test_button)
        refresh_button = QtWidgets.QPushButton("Refresh Models")
        refresh_button.clicked.connect(self.refresh_ollama_models)
        model_layout.addWidget(refresh_button)
        layout.addWidget(models)

        self.ollama_status_label = QtWidgets.QLabel("Configure an Ollama endpoint, then test the connection.")
        self.ollama_status_label.setObjectName("status")
        self.ollama_status_label.setWordWrap(True)
        layout.addWidget(self.ollama_status_label)
        layout.addStretch(1)
        return tab

    def _build_agents_navigation_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setMinimumWidth(290)
        panel.setMaximumWidth(390)
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(self._build_presets_box())
        layout.addWidget(self._build_role_list_box(), 1)
        return panel

    def _build_role_list_box(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Pipeline Roles")
        layout = QtWidgets.QVBoxLayout(box)

        self.role_list = QtWidgets.QListWidget()
        self.role_list.setMinimumHeight(180)
        self.role_list.currentRowChanged.connect(self.on_role_list_selection)
        layout.addWidget(self.role_list, 1)

        role_btn_grid = QtWidgets.QGridLayout()
        add_role_btn = QtWidgets.QPushButton("Add Role")
        add_role_btn.clicked.connect(self.add_role)
        role_btn_grid.addWidget(add_role_btn, 0, 0)
        import_role_btn = QtWidgets.QPushButton("Import Saved")
        import_role_btn.clicked.connect(self.import_role)
        role_btn_grid.addWidget(import_role_btn, 0, 1)
        remove_role_btn = QtWidgets.QPushButton("Remove")
        remove_role_btn.clicked.connect(self.remove_role)
        role_btn_grid.addWidget(remove_role_btn, 1, 0)
        move_up_btn = QtWidgets.QPushButton("Move Up")
        move_up_btn.clicked.connect(lambda: self.move_role(-1))
        role_btn_grid.addWidget(move_up_btn, 1, 1)
        move_down_btn = QtWidgets.QPushButton("Move Down")
        move_down_btn.clicked.connect(lambda: self.move_role(1))
        role_btn_grid.addWidget(move_down_btn, 2, 0, 1, 2)
        layout.addLayout(role_btn_grid)

        return box

    def _build_menu(self) -> None:
        menubar = self.menuBar()
        workflow_menu = menubar.addMenu("Workflow")
        new_workflow_action = workflow_menu.addAction("New Workflow")
        new_workflow_action.triggered.connect(self.new_workflow)
        workflow_menu.addSeparator()
        save_session_action = workflow_menu.addAction("Save Session")
        save_session_action.triggered.connect(self.save_session)
        save_session_as_action = workflow_menu.addAction("Save Session As...")
        save_session_as_action.triggered.connect(self.save_session_as)
        load_session_action = workflow_menu.addAction("Load Session...")
        load_session_action.triggered.connect(self.load_session)
        workflow_menu.addSeparator()
        workflow_menu.addAction("Open Artifacts Folder")
        workflow_menu.addAction("Export Transcript")

        agents_menu = menubar.addMenu("Agents")
        agents_menu.addAction("Edit Role Profiles")
        duplicate_workflow_action = agents_menu.addAction("Duplicate Workflow")
        duplicate_workflow_action.triggered.connect(self.duplicate_workflow)

        debug_menu = menubar.addMenu("Debug")
        debug_menu.addAction("Show JSONL")
        debug_menu.addAction("Validate Handoffs")

    def _build_sidebar(self) -> QtWidgets.QWidget:
        sidebar = QtWidgets.QWidget()
        sidebar.setFixedWidth(310)
        layout = QtWidgets.QVBoxLayout(sidebar)
        layout.setContentsMargins(0, 0, 0, 0)

        layout.addWidget(self._build_presets_box())

        pipeline_label = QtWidgets.QLabel("Agent Pipeline")
        pipeline_label.setObjectName("section")
        layout.addWidget(pipeline_label)
        self.pipeline_output = mono_textedit(wrap=False)
        self.pipeline_output.setFixedHeight(150)
        self.pipeline_output.setPlainText(self.pipeline_text())
        layout.addWidget(self.pipeline_output)

        handoff_label = QtWidgets.QLabel("Handoff")
        handoff_label.setObjectName("section")
        layout.addWidget(handoff_label)
        self.handoff_output = mono_textedit()
        self.handoff_output.setFixedHeight(140)
        self.handoff_output.setPlainText(self.handoff_status_text())
        layout.addWidget(self.handoff_output)

        artifacts_label = QtWidgets.QLabel("Artifacts")
        artifacts_label.setObjectName("section")
        layout.addWidget(artifacts_label)
        self.artifact_status_output = mono_textedit(wrap=False)
        self.artifact_status_output.setFixedHeight(170)
        layout.addWidget(self.artifact_status_output)

        layout.addStretch(1)
        return sidebar

    def _build_sessions_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(135)
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 0, 0)

        label = QtWidgets.QLabel("Sessions")
        label.setObjectName("section")
        layout.addWidget(label)

        self.session_list = QtWidgets.QListWidget()
        self.session_list.itemClicked.connect(self.on_session_item_clicked)
        layout.addWidget(self.session_list, 1)

        btn_row = QtWidgets.QHBoxLayout()
        delete_btn = QtWidgets.QPushButton("Delete")
        delete_btn.clicked.connect(self.delete_selected_session)
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

        return panel

    def _build_presets_box(self) -> QtWidgets.QWidget:
        """Builds a Presets picker. Called once per tab that needs one (Run
        sidebar, Agents tab); each call gets its own dropdown, kept in sync
        via self._preset_dropdowns / _sync_preset_dropdowns."""
        box = QtWidgets.QGroupBox("Presets")
        layout = QtWidgets.QVBoxLayout(box)

        dropdown = QtWidgets.QComboBox()
        dropdown.addItems(sorted(self.presets.keys()))
        if self.current_preset_name:
            dropdown.setCurrentText(self.current_preset_name)
        if not self._preset_dropdowns:
            self.preset_dropdown = dropdown
        self._preset_dropdowns.append(dropdown)
        layout.addWidget(dropdown)

        btn_row = QtWidgets.QHBoxLayout()
        load_btn = QtWidgets.QPushButton("Load")
        load_btn.clicked.connect(lambda: self.load_selected_preset(dropdown))
        btn_row.addWidget(load_btn)
        save_btn = QtWidgets.QPushButton("Save As...")
        save_btn.clicked.connect(lambda: self.save_preset_as(dropdown))
        btn_row.addWidget(save_btn)
        delete_btn = QtWidgets.QPushButton("Delete")
        delete_btn.clicked.connect(lambda: self.delete_selected_preset(dropdown))
        btn_row.addWidget(delete_btn)
        layout.addLayout(btn_row)

        return box

    def _build_main_column(self) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        layout.addWidget(self._build_command_center())
        layout.addWidget(self._build_execution_settings_box())

        splitter = QtWidgets.QSplitter(QtCore.Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_conversation_panel())
        splitter.addWidget(self._build_tabs())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

        return container

    def _build_command_center(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Command Center")
        layout = QtWidgets.QVBoxLayout(box)

        self.goal_input = QtWidgets.QPlainTextEdit(self.state.goal)
        self.goal_input.setFont(QtGui.QFont(MONO_FONT_FAMILY, 10))
        self.goal_input.setFixedHeight(60)
        self.goal_input.textChanged.connect(self.on_goal_changed)
        layout.addWidget(self.goal_input)

        row = QtWidgets.QHBoxLayout()
        row.addWidget(self._fixed_label("Workspace", 90))
        self.workspace_input = QtWidgets.QLineEdit(self.state.cwd)
        self.workspace_input.textChanged.connect(self.on_cwd_changed)
        row.addWidget(self.workspace_input, 1)
        use_app_folder_btn = QtWidgets.QPushButton("Use App Folder")
        use_app_folder_btn.clicked.connect(self.use_app_folder)
        row.addWidget(use_app_folder_btn)
        layout.addLayout(row)

        preflight_row = QtWidgets.QHBoxLayout()
        preflight_row.addWidget(self._fixed_label("Preflight", 90))
        preflight_label = QtWidgets.QLabel(self.dependency_status.message)
        preflight_label.setObjectName("status")
        preflight_label.setWordWrap(True)
        preflight_row.addWidget(preflight_label, 1)
        layout.addLayout(preflight_row)

        status_row = QtWidgets.QHBoxLayout()
        self.status_indicator = StatusIndicator()
        status_row.addWidget(self.status_indicator)
        self.phase_label = self._fixed_label("Idle", 120)
        status_row.addWidget(self.phase_label)
        self.agent_label = self._fixed_label("Agent: none", 160)
        status_row.addWidget(self.agent_label)
        self.detail_label = QtWidgets.QLabel("No agent is running.")
        self.detail_label.setObjectName("status")
        self.detail_label.setWordWrap(True)
        status_row.addWidget(self.detail_label, 1)
        layout.addLayout(status_row)

        toolbar = QtWidgets.QHBoxLayout()
        start_btn = QtWidgets.QPushButton("Start")
        start_btn.setObjectName("primary")
        start_btn.clicked.connect(self.start_workflow)
        toolbar.addWidget(start_btn)

        continue_btn = QtWidgets.QPushButton("Continue")
        continue_btn.clicked.connect(self.continue_workflow)
        toolbar.addWidget(continue_btn)

        pause_btn = QtWidgets.QPushButton("Pause")
        pause_btn.clicked.connect(self.pause_workflow)
        toolbar.addWidget(pause_btn)

        step_btn = QtWidgets.QPushButton("Step Once")
        step_btn.clicked.connect(self.start_current_role)
        toolbar.addWidget(step_btn)

        stop_btn = QtWidgets.QPushButton("Stop")
        stop_btn.clicked.connect(self.stop_run)
        toolbar.addWidget(stop_btn)

        toolbar.addWidget(self._vline())

        relay_btn = QtWidgets.QPushButton("Relay Pending")
        relay_btn.clicked.connect(self.relay_pending_handoff)
        toolbar.addWidget(relay_btn)

        refresh_btn = QtWidgets.QPushButton("Refresh Artifacts")
        refresh_btn.clicked.connect(self.refresh_artifacts)
        toolbar.addWidget(refresh_btn)

        clear_btn = QtWidgets.QPushButton("Clear")
        clear_btn.clicked.connect(self.clear_outputs)
        toolbar.addWidget(clear_btn)

        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        return box

    def _build_conversation_panel(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Conversation")
        layout = QtWidgets.QVBoxLayout(box)
        self.conversation_output = mono_textedit()
        layout.addWidget(self.conversation_output)
        return box

    def _build_tabs(self) -> QtWidgets.QWidget:
        self.tabs = QtWidgets.QTabWidget()

        current_tab = QtWidgets.QWidget()
        current_layout = QtWidgets.QVBoxLayout(current_tab)
        self.current_output = mono_textedit()
        self.current_output.setPlainText(self.current_step_text())
        current_layout.addWidget(self.current_output)
        self.tabs.addTab(current_tab, "Current Step")

        self.tabs.addTab(self._build_artifacts_tab(), "Artifacts")

        activity_tab = QtWidgets.QWidget()
        activity_layout = QtWidgets.QVBoxLayout(activity_tab)
        self.activity_output = mono_textedit(wrap=False)
        activity_layout.addWidget(self.activity_output)
        self.tabs.addTab(activity_tab, "Activity")

        self.tabs.addTab(self._build_debug_tab(), "Debug")

        return self.tabs

    def _build_artifacts_tab(self) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        artifact_tabs = QtWidgets.QTabWidget()

        specs = [
            ("PLAN.md", "PLAN.md"),
            ("REVIEW.md", "REVIEW.md"),
            ("IMPLEMENTATION_NOTES.md", "IMPLEMENTATION"),
            ("TEST_RESULTS.md", "TESTS"),
            ("SUMMARY.md", "SUMMARY.md"),
        ]
        for artifact_name, label in specs:
            tab = QtWidgets.QWidget()
            tab_layout = QtWidgets.QVBoxLayout(tab)
            output = mono_textedit()
            self.artifact_outputs[artifact_name] = output
            tab_layout.addWidget(output)
            artifact_tabs.addTab(tab, label)

        layout.addWidget(artifact_tabs)
        return container

    def _build_role_prompt_tab(self) -> QtWidgets.QWidget:
        container = QtWidgets.QGroupBox("Role Editor")
        layout = QtWidgets.QVBoxLayout(container)
        name_row = QtWidgets.QHBoxLayout()
        name_row.addWidget(self._fixed_label("Role name", 90))
        self.role_name_input = QtWidgets.QLineEdit(self.current_role().name)
        self.role_name_input.textChanged.connect(self.on_role_name_changed)
        name_row.addWidget(self.role_name_input, 1)
        layout.addLayout(name_row)

        artifact_row = QtWidgets.QHBoxLayout()
        artifact_row.addWidget(self._fixed_label("Artifact file", 90))
        self.role_artifact_input = QtWidgets.QLineEdit(self.current_role().artifact_name)
        self.role_artifact_input.textChanged.connect(self.on_role_artifact_changed)
        artifact_row.addWidget(self.role_artifact_input, 1)
        layout.addLayout(artifact_row)

        role_model_row = QtWidgets.QHBoxLayout()
        role_model_row.addWidget(self._fixed_label("Role model", 90))
        self.role_model_input = QtWidgets.QLineEdit(self.current_role().model)
        self.role_model_input.setPlaceholderText("Use global model")
        self.role_model_input.textChanged.connect(self.on_role_model_changed)
        role_model_row.addWidget(self.role_model_input, 1)
        layout.addLayout(role_model_row)

        role_provider_row = QtWidgets.QHBoxLayout()
        role_provider_row.addWidget(self._fixed_label("Role provider", 90))
        self.role_provider_combo = QtWidgets.QComboBox()
        self.role_provider_combo.addItems([ROLE_PROVIDER_DEFAULT_LABEL, *PROVIDERS])
        self.role_provider_combo.setCurrentText(self.current_role().provider or ROLE_PROVIDER_DEFAULT_LABEL)
        self.role_provider_combo.currentTextChanged.connect(self.on_role_provider_changed)
        role_provider_row.addWidget(self.role_provider_combo, 1)
        layout.addLayout(role_provider_row)

        effort_row = QtWidgets.QHBoxLayout()
        effort_row.addWidget(self._fixed_label("Effort", 90))
        self.role_effort_combo = QtWidgets.QComboBox()
        self.role_effort_combo.setEditable(False)
        self.role_effort_combo.addItems(["", "minimal", "low", "medium", "high"])
        self.role_effort_combo.setCurrentText(self.current_role().model_reasoning_effort)
        self.role_effort_combo.currentTextChanged.connect(self.on_role_effort_changed)
        effort_row.addWidget(self.role_effort_combo, 1)
        layout.addLayout(effort_row)
        self.role_prompt_input = QtWidgets.QPlainTextEdit(self.current_role().prompt)
        self.role_prompt_input.setFont(QtGui.QFont(MONO_FONT_FAMILY, 10))
        self.role_prompt_input.textChanged.connect(self.on_role_prompt_changed)
        self.role_prompt_input.setMinimumHeight(260)
        layout.addWidget(self.role_prompt_input, 1)

        handoff_targets_label = QtWidgets.QLabel("Allowed handoff targets")
        handoff_targets_label.setObjectName("section")
        layout.addWidget(handoff_targets_label)
        self.handoff_targets_list = QtWidgets.QListWidget()
        self.handoff_targets_list.setMinimumHeight(90)
        self.handoff_targets_list.setMaximumHeight(150)
        self.handoff_targets_list.itemChanged.connect(self.on_handoff_targets_changed)
        layout.addWidget(self.handoff_targets_list)
        role_lib_row = QtWidgets.QHBoxLayout()
        save_role_btn = QtWidgets.QPushButton("Save Role to Library")
        save_role_btn.clicked.connect(self.save_role)
        role_lib_row.addWidget(save_role_btn)
        delete_role_lib_btn = QtWidgets.QPushButton("Delete Role from Library")
        delete_role_lib_btn.clicked.connect(self.delete_role_from_library)
        role_lib_row.addWidget(delete_role_lib_btn)
        role_lib_row.addStretch(1)
        layout.addLayout(role_lib_row)
        self._sync_role_editor()
        return container

    def _build_execution_settings_box(self) -> QtWidgets.QWidget:
        box = QtWidgets.QGroupBox("Execution Settings")
        layout = QtWidgets.QVBoxLayout(box)

        provider_row = QtWidgets.QHBoxLayout()
        provider_row.addWidget(self._fixed_label("Provider", 90))
        self.provider_combo = QtWidgets.QComboBox()
        self.provider_combo.addItems(PROVIDERS)
        self.provider_combo.setCurrentText(self.state.provider)
        self.provider_combo.currentTextChanged.connect(self.on_provider_changed)
        provider_row.addWidget(self.provider_combo)
        provider_row.addStretch(1)
        layout.addLayout(provider_row)

        model_row = QtWidgets.QHBoxLayout()
        model_row.addWidget(self._fixed_label("Model", 90))
        self.model_input = QtWidgets.QLineEdit()
        self.model_input.setPlaceholderText("Codex config default")
        self.model_input.textChanged.connect(self.on_model_changed)
        model_row.addWidget(self.model_input, 1)
        layout.addLayout(model_row)

        codex_row = QtWidgets.QHBoxLayout()
        codex_row.addWidget(self._fixed_label("Codex", 90))
        self.codex_cmd_input = QtWidgets.QLineEdit(self.state.codex_cmd)
        self.codex_cmd_input.textChanged.connect(self.on_codex_cmd_changed)
        codex_row.addWidget(self.codex_cmd_input, 1)
        detect_btn = QtWidgets.QPushButton("Detect")
        detect_btn.clicked.connect(self.detect_codex_command)
        codex_row.addWidget(detect_btn)
        layout.addLayout(codex_row)

        claude_row = QtWidgets.QHBoxLayout()
        claude_row.addWidget(self._fixed_label("Claude", 90))
        self.claude_cmd_input = QtWidgets.QLineEdit(self.state.claude_cmd)
        self.claude_cmd_input.setPlaceholderText("claude")
        self.claude_cmd_input.textChanged.connect(self.on_claude_cmd_changed)
        claude_row.addWidget(self.claude_cmd_input, 1)
        layout.addLayout(claude_row)

        sandbox_row = QtWidgets.QHBoxLayout()
        sandbox_row.addWidget(self._fixed_label("Sandbox", 90))
        self.sandbox_dropdown = QtWidgets.QComboBox()
        self.sandbox_dropdown.addItems(["workspace-write", "read-only", "danger-full-access"])
        self.sandbox_dropdown.setCurrentText(self.state.sandbox)
        self.sandbox_dropdown.setFixedWidth(190)
        self.sandbox_dropdown.currentTextChanged.connect(self.on_sandbox_changed)
        sandbox_row.addWidget(self.sandbox_dropdown)
        self.bypass_checkbox = QtWidgets.QCheckBox("Normal local shell mode")
        self.bypass_checkbox.setChecked(self.state.bypass_approvals_and_sandbox)
        self.bypass_checkbox.toggled.connect(self.on_bypass_changed)
        sandbox_row.addWidget(self.bypass_checkbox)
        sandbox_row.addStretch(1)
        layout.addLayout(sandbox_row)

        flags_row = QtWidgets.QHBoxLayout()
        self.skip_git_checkbox = QtWidgets.QCheckBox("Skip git repo check")
        self.skip_git_checkbox.setChecked(self.state.skip_git_check)
        self.skip_git_checkbox.toggled.connect(self.on_skip_git_changed)
        flags_row.addWidget(self.skip_git_checkbox)

        self.ephemeral_checkbox = QtWidgets.QCheckBox("Ephemeral")
        self.ephemeral_checkbox.setChecked(self.state.ephemeral)
        self.ephemeral_checkbox.toggled.connect(self.on_ephemeral_changed)
        flags_row.addWidget(self.ephemeral_checkbox)

        self.auto_advance_checkbox = QtWidgets.QCheckBox("Auto-relay handoffs")
        self.auto_advance_checkbox.setChecked(self.state.auto_advance)
        self.auto_advance_checkbox.toggled.connect(self.on_auto_advance_changed)
        flags_row.addWidget(self.auto_advance_checkbox)
        flags_row.addStretch(1)
        layout.addLayout(flags_row)

        extra_row = QtWidgets.QHBoxLayout()
        extra_row.addWidget(self._fixed_label("Extra args", 90))
        self.extra_args_input = QtWidgets.QLineEdit()
        self.extra_args_input.setPlaceholderText("--profile name --config key=value")
        self.extra_args_input.textChanged.connect(self.on_extra_args_changed)
        extra_row.addWidget(self.extra_args_input, 1)
        layout.addLayout(extra_row)

        return box

    def _build_debug_tab(self) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(container)
        debug_tabs = QtWidgets.QTabWidget()

        events_tab = QtWidgets.QWidget()
        events_layout = QtWidgets.QVBoxLayout(events_tab)
        self.events_output = mono_textedit(wrap=False)
        events_layout.addWidget(self.events_output)
        debug_tabs.addTab(events_tab, "Events")

        raw_tab = QtWidgets.QWidget()
        raw_layout = QtWidgets.QVBoxLayout(raw_tab)
        self.raw_output = mono_textedit(wrap=False)
        raw_layout.addWidget(self.raw_output)
        debug_tabs.addTab(raw_tab, "Raw JSONL")

        layout.addWidget(debug_tabs)
        return container

    def _build_status_bar(self) -> None:
        status_bar = QtWidgets.QStatusBar()
        self.setStatusBar(status_bar)
        self.status_label = QtWidgets.QLabel("Ready.")
        self.status_label.setObjectName("status")
        status_bar.addWidget(self.status_label, 1)
        self._rebuild_role_status_buttons()

    def _rebuild_role_status_buttons(self) -> None:
        status_bar = self.statusBar()
        for btn in self._role_button_widgets:
            status_bar.removeWidget(btn)
            btn.deleteLater()
        self._role_button_widgets = []
        for index, role in enumerate(self.roles):
            btn = QtWidgets.QPushButton(role.name)
            btn.setFlat(True)
            btn.clicked.connect(lambda _checked=False, i=index: self.select_role(i))
            status_bar.addPermanentWidget(btn)
            self._role_button_widgets.append(btn)

    @staticmethod
    def _fixed_label(text: str, width: int) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setFixedWidth(width)
        return label

    @staticmethod
    def _vline() -> QtWidgets.QFrame:
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        line.setFrameShadow(QtWidgets.QFrame.Shadow.Sunken)
        return line

    def set_status(self, text: str) -> None:
        self.status_label.setText(text)

    # ------------------------------------------------------------ lifecycle

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802 (Qt override)
        self.stop_all_agents()
        super().closeEvent(event)

    def current_role(self) -> AgentRole:
        return self.roles[self.state.current_role_index]

    def artifacts_dir(self) -> Path:
        return self.current_workspace() / "artifacts"

    def current_workspace(self) -> Path:
        return workspace_path(self.state.cwd)

    def is_run_active(self) -> bool:
        return self.active_run_id is not None and self.run_lifecycle in {"launching", "running", "stopping"}

    def reject_if_run_active(self, action: str) -> bool:
        if not self.is_run_active():
            return False
        role_name = self.active_role.name if self.active_role else self.current_role().name
        self.set_status(f"Cannot {action}; {role_name} is {self.run_lifecycle}.")
        self.set_phase("Waiting", role_name, "Stop the active Codex run before starting another action.")
        return True

    def default_session_path(self) -> Path:
        return session_dir_for_workspace(self.current_workspace()) / f"workbench-session{SESSION_FILE_SUFFIX}"

    def automatic_session_path(self) -> Path:
        session_dir = session_dir_for_workspace(self.current_workspace())
        timestamp = time.strftime("%Y%m%d-%H%M%S")
        stem = f"{timestamp}-{session_slug(self.state.goal)}"
        path = session_dir / f"{stem}{SESSION_FILE_SUFFIX}"
        suffix = 2
        while path.exists():
            path = session_dir / f"{stem}-{suffix}{SESSION_FILE_SUFFIX}"
            suffix += 1
        return path

    def ensure_session_file(self) -> None:
        if self.session_file is None:
            self.write_session_file(self.automatic_session_path(), announce=False)

    def register_session_path(self, path: Path) -> None:
        normalized = contained_session_path(self.current_workspace(), path)
        if normalized is None:
            self.append_event(f"Session path rejected outside workspace session directory: {path}", "warning")
            return
        existing = [item for item in self.saved_session_paths if str(item).lower() != str(normalized).lower()]
        self.saved_session_paths = [normalized, *existing]
        try:
            save_session_index(self.current_workspace(), self.saved_session_paths)
        except OSError as exc:
            self.append_event(f"Could not update session index: {exc}", "warning")
        self.refresh_session_list()

    def refresh_session_list(self) -> None:
        indexed_paths = load_session_index(
            self.current_workspace(),
            warn=lambda message: self.append_event(message, "warning"),
        )
        if self.saved_session_paths:
            indexed_keys = {path_key(path) for path in indexed_paths}
            indexed_paths.extend(path for path in self.saved_session_paths if path_key(path) not in indexed_keys)
        existing_paths: list[Path] = []
        for path in indexed_paths:
            safe_path = contained_session_path(self.current_workspace(), path)
            if safe_path and safe_path.exists():
                existing_paths.append(safe_path)
        self.saved_session_paths = existing_paths

        if self.session_list is None:
            return
        self.session_list.blockSignals(True)
        self.session_list.clear()
        for path in self.saved_session_paths:
            item = QtWidgets.QListWidgetItem(self.session_label(path))
            item.setData(QtCore.Qt.ItemDataRole.UserRole, str(path))
            item.setToolTip(str(path))
            if self.session_file and str(path).lower() == str(self.session_file).lower():
                font = item.font()
                font.setBold(True)
                item.setFont(font)
            self.session_list.addItem(item)
        if not self.saved_session_paths:
            item = QtWidgets.QListWidgetItem("No saved sessions")
            item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEnabled)
            self.session_list.addItem(item)
        self.session_list.blockSignals(False)

    def session_label(self, path: Path) -> str:
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return path.stem
        if not isinstance(loaded, dict):
            return path.stem
        state = loaded.get("state") if isinstance(loaded.get("state"), dict) else {}
        goal = str(state.get("goal") or "").strip().splitlines()
        title = goal[0][:24] if goal else path.stem[:24]
        role_count = len(loaded.get("roles")) if isinstance(loaded.get("roles"), list) else 0
        return f"{title}\n{role_count} roles"

    def on_session_item_clicked(self, item: QtWidgets.QListWidgetItem) -> None:
        path_text = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not path_text:
            return
        self.load_session_file(Path(str(path_text)))

    def delete_selected_session(self) -> None:
        if self.session_list is None:
            return
        item = self.session_list.currentItem()
        if item is None:
            self.set_status("Select a session to delete.")
            return
        path_text = item.data(QtCore.Qt.ItemDataRole.UserRole)
        if not path_text:
            self.set_status("Select a saved session to delete.")
            return
        path = contained_session_path(self.current_workspace(), Path(str(path_text)))
        if path is None:
            self.set_status("Refusing to delete a session outside the workspace session directory.")
            self.append_event(f"Unsafe session delete rejected: {path_text}", "warning")
            self.refresh_session_list()
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete Session",
            f"Delete this saved session?\n{path}",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            QtWidgets.QMessageBox.critical(self, "Delete Session", f"Could not delete session:\n{exc}")
            self.set_status(f"Could not delete session: {exc}")
            return

        self.saved_session_paths = [item_path for item_path in self.saved_session_paths if str(item_path).lower() != str(path).lower()]
        try:
            save_session_index(self.current_workspace(), self.saved_session_paths)
        except OSError as exc:
            self.append_event(f"Could not update session index: {exc}", "warning")
        if self.session_file and str(self.session_file).lower() == str(path).lower():
            self.session_file = None
        self.refresh_session_list()
        self.set_status(f"Deleted session: {path}")

    def session_payload(self) -> dict[str, Any]:
        return {
            "version": 1,
            "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "state": workflow_state_to_dict(self.state),
            "roles": [role_to_dict(role) for role in self.roles],
            "conversation_lines": list(self.conversation_lines),
            "activity_lines": list(self.activity_lines),
            "event_lines": list(self.event_lines),
            "raw_lines": list(self.raw_lines),
            "final_text_by_role": dict(self.final_text_by_role),
            "agent_buffers": dict(self.agent_buffers),
            "seen_handoffs": sorted(self.seen_handoffs),
            "pending_handoff": handoff_to_dict(self.pending_handoff),
            "pending_relay_message": self.pending_relay_message,
            "phase": self.phase,
            "phase_detail": self.phase_detail,
        }

    def choose_session_save_path(self) -> Path | None:
        start_path = str(self.session_file or self.default_session_path())
        path, _selected_filter = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save Workbench Session",
            start_path,
            SESSION_FILE_FILTER,
        )
        if not path:
            return None
        session_path = Path(path)
        if session_path.suffix.lower() != ".json":
            session_path = session_path.with_name(session_path.name + SESSION_FILE_SUFFIX)
        return session_path

    def save_session(self) -> None:
        if self.session_file is None:
            self.save_session_as()
            return
        self.write_session_file(self.session_file)

    def save_session_as(self) -> None:
        path = self.choose_session_save_path()
        if path is None:
            return
        self.write_session_file(path)

    def write_session_file(self, path: Path, announce: bool = True) -> None:
        safe_path = contained_session_path(self.current_workspace(), path)
        if safe_path is None:
            message = f"Session path must stay under {session_dir_for_workspace(self.current_workspace())}"
            if announce:
                QtWidgets.QMessageBox.warning(self, "Save Session", message)
            self.set_status(message)
            self.append_event(f"Unsafe session save rejected: {path}", "warning")
            return
        try:
            atomic_write_json(safe_path, self.session_payload())
        except OSError as exc:
            if announce:
                QtWidgets.QMessageBox.critical(self, "Save Session", f"Could not save session:\n{exc}")
            self.set_status(f"Could not save session: {exc}")
            return
        self.session_file = safe_path
        self.register_session_path(safe_path)
        if announce:
            self.set_status(f"Saved session: {safe_path}")
            self.append_event(f"Session saved: {safe_path}")

    def auto_save_session(self) -> None:
        if self.session_file is not None:
            self.write_session_file(self.session_file, announce=False)

    def load_session(self) -> None:
        if self.reject_if_run_active("load a session"):
            return

        start_path = str(self.session_file or self.default_session_path())
        path, _selected_filter = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Load Workbench Session",
            start_path,
            SESSION_FILE_FILTER,
        )
        if not path:
            return
        self.load_session_file(Path(path))

    def load_session_file(self, path: Path) -> None:
        if self.reject_if_run_active("load a session"):
            return
        safe_path = contained_session_path(self.current_workspace(), path)
        if safe_path is None:
            self.set_status("Refusing to load a session outside the workspace session directory.")
            self.append_event(f"Unsafe session load rejected: {path}", "warning")
            return
        try:
            loaded = json.loads(safe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            QtWidgets.QMessageBox.critical(self, "Load Session", f"Could not load session:\n{exc}")
            self.set_status(f"Could not load session: {exc}")
            return
        if not isinstance(loaded, dict):
            QtWidgets.QMessageBox.critical(self, "Load Session", "Session file does not contain a JSON object.")
            return

        state_data = loaded.get("state") if isinstance(loaded.get("state"), dict) else {}
        roles_data = loaded.get("roles") if isinstance(loaded.get("roles"), list) else []
        roles = [role for item in roles_data if isinstance(item, dict) for role in [role_from_dict(item)] if role]
        if not roles:
            QtWidgets.QMessageBox.critical(self, "Load Session", "Session file does not contain any roles.")
            return

        self.state = workflow_state_from_dict(state_data)
        self.roles = roles
        self.saved_session_paths = []
        self.state.current_role_index = max(0, min(self.state.current_role_index, len(self.roles) - 1))
        self.conversation_lines = self._string_list(loaded.get("conversation_lines"))[-120:]
        self.activity_lines = self._string_list(loaded.get("activity_lines"))[-MAX_LOG_LINES:]
        self.event_lines = self._string_list(loaded.get("event_lines"))[-MAX_LOG_LINES:]
        self.raw_lines = self._string_list(loaded.get("raw_lines"))[-MAX_RAW_LINES:]
        self.final_text_by_role = self._string_dict(loaded.get("final_text_by_role"))
        self.agent_buffers = self._string_dict(loaded.get("agent_buffers"))
        self.seen_handoffs = set(self._string_list(loaded.get("seen_handoffs")))
        self.pending_handoff = handoff_from_dict(loaded.get("pending_handoff"))
        self.pending_relay_message = str(loaded.get("pending_relay_message") or "")
        self.active_role = None
        self.run_started_at = 0.0
        self.active_run_id = None
        self.run_lifecycle = "idle"
        self.session_file = safe_path
        self.register_session_path(safe_path)

        self.sync_controls_from_state()
        self._rebuild_role_status_buttons()
        self._sync_role_editor()
        self.set_phase(str(loaded.get("phase") or "Idle"), None, str(loaded.get("phase_detail") or "Session loaded."))
        self.refresh_all_outputs()
        self.refresh_artifacts()
        self.set_status(f"Loaded session: {safe_path}")

    @staticmethod
    def _string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item) for item in value]

    @staticmethod
    def _string_dict(value: Any) -> dict[str, str]:
        if not isinstance(value, dict):
            return {}
        return {str(key): str(item) for key, item in value.items()}

    def sync_controls_from_state(self) -> None:
        self.goal_input.blockSignals(True)
        self.goal_input.setPlainText(self.state.goal)
        self.goal_input.blockSignals(False)

        self.workspace_input.blockSignals(True)
        self.workspace_input.setText(self.state.cwd)
        self.workspace_input.blockSignals(False)

        self.model_input.blockSignals(True)
        self.model_input.setText(self.state.model)
        self.model_input.blockSignals(False)

        self.provider_combo.blockSignals(True)
        self.provider_combo.setCurrentText(self.state.provider)
        self.provider_combo.blockSignals(False)

        self.codex_cmd_input.blockSignals(True)
        self.codex_cmd_input.setText(self.state.codex_cmd)
        self.codex_cmd_input.blockSignals(False)

        self.claude_cmd_input.blockSignals(True)
        self.claude_cmd_input.setText(self.state.claude_cmd)
        self.claude_cmd_input.blockSignals(False)

        self.sandbox_dropdown.blockSignals(True)
        if self.sandbox_dropdown.findText(self.state.sandbox) == -1:
            self.sandbox_dropdown.addItem(self.state.sandbox)
        self.sandbox_dropdown.setCurrentText(self.state.sandbox)
        self.sandbox_dropdown.blockSignals(False)

        self.bypass_checkbox.blockSignals(True)
        self.bypass_checkbox.setChecked(self.state.bypass_approvals_and_sandbox)
        self.bypass_checkbox.blockSignals(False)

        self.skip_git_checkbox.blockSignals(True)
        self.skip_git_checkbox.setChecked(self.state.skip_git_check)
        self.skip_git_checkbox.blockSignals(False)

        self.ephemeral_checkbox.blockSignals(True)
        self.ephemeral_checkbox.setChecked(self.state.ephemeral)
        self.ephemeral_checkbox.blockSignals(False)

        self.auto_advance_checkbox.blockSignals(True)
        self.auto_advance_checkbox.setChecked(self.state.auto_advance)
        self.auto_advance_checkbox.blockSignals(False)

        self.extra_args_input.blockSignals(True)
        self.extra_args_input.setText(self.state.extra_args)
        self.extra_args_input.blockSignals(False)

        self.ollama_mode_combo.blockSignals(True)
        self.ollama_mode_combo.setCurrentIndex(0 if self.state.ollama_mode == "local" else 1)
        self.ollama_mode_combo.blockSignals(False)
        self.ollama_url_input.blockSignals(True)
        self.ollama_url_input.setText(self.state.ollama_base_url)
        self.ollama_url_input.blockSignals(False)
        self.ollama_keep_alive_input.blockSignals(True)
        self.ollama_keep_alive_input.setText(self.state.ollama_keep_alive)
        self.ollama_keep_alive_input.blockSignals(False)
        self.ollama_timeout_spin.blockSignals(True)
        self.ollama_timeout_spin.setValue(self.state.ollama_request_timeout_seconds)
        self.ollama_timeout_spin.blockSignals(False)
        self.ollama_command_timeout_spin.blockSignals(True)
        self.ollama_command_timeout_spin.setValue(self.state.ollama_command_timeout_seconds)
        self.ollama_command_timeout_spin.blockSignals(False)
        self.ollama_verify_tls_checkbox.blockSignals(True)
        self.ollama_verify_tls_checkbox.setChecked(self.state.ollama_verify_tls)
        self.ollama_verify_tls_checkbox.blockSignals(False)
        self.ollama_ca_bundle_input.blockSignals(True)
        self.ollama_ca_bundle_input.setText(self.state.ollama_ca_bundle_path)
        self.ollama_ca_bundle_input.blockSignals(False)
        self.ollama_context_spin.blockSignals(True)
        self.ollama_context_spin.setValue(self.state.ollama_context_length)
        self.ollama_context_spin.blockSignals(False)
        self.ollama_models_combo.setCurrentText(self.state.model)

    # ----------------------------------------------------------- workflow

    def start_workflow(self) -> None:
        if self.reject_if_run_active("start a workflow"):
            return
        self.clear_outputs()
        for role in self.roles:
            role.status = "queued"
        self.state.current_role_index = 0
        self.pending_relay_message = ""
        self.pending_handoff = None
        self.start_current_role()

    def continue_workflow(self) -> None:
        if self.reject_if_run_active("continue the workflow"):
            return
        if self.current_role().name == "Section Planner" and self.current_role().status == "complete":
            manager_index = self.role_index("Section Manager")
            if manager_index is not None:
                self.state.current_role_index = manager_index
                self.current_role().status = "waiting"
                self._sync_role_editor()
        self.start_current_role()

    def pause_workflow(self) -> None:
        self.state.auto_advance = False
        self.auto_advance_checkbox.setChecked(False)
        self.set_status("Auto-advance paused.")

    def start_current_role(self) -> None:
        role = self.current_role()
        cwd = self.current_workspace()
        if not cwd.exists() or not cwd.is_dir():
            self.set_status(f"Workspace folder does not exist: {cwd}")
            return

        if self.reject_if_run_active("start another agent"):
            return

        safe_artifact, warning = safe_artifact_name(role.artifact_name, role.name)
        if warning:
            role.artifact_name = safe_artifact
            self.append_event(warning, "warning")
            self._sync_role_editor()

        self.ensure_session_file()
        self.artifacts_dir().mkdir(exist_ok=True)
        self._next_run_id += 1
        run_id = self._next_run_id
        self.active_run_id = run_id
        self.run_lifecycle = "launching"
        role.status = "running"
        self.active_role = role
        self.run_started_at = time.time()
        self.last_run_error = ""
        provider = role.provider or self.state.provider
        if provider == "Ollama" and not self.confirm_ollama_connection():
            return
        self.set_phase("Starting", role.name, f"Launching {provider} process.")
        if self.pending_relay_message:
            self.append_conversation("Relay", self.pending_relay_message)
        else:
            self.append_conversation("User", self.state.goal)
        self.append_conversation(role.name, f"Starting {provider} run.")
        self.append_activity(f"[{role.name.lower()} running] {role.artifact_name}")
        self.set_status(f"{role.name} is running...")
        self.refresh_all_outputs()

        prompt = self.build_agent_prompt(role, self.pending_relay_message)
        self.pending_relay_message = ""
        common_callbacks = {
            "on_event": lambda event, run_id=run_id: self.signals.event_ready.emit(run_id, event),
            "on_log": lambda summary, run_id=run_id: self.signals.log_ready.emit(run_id, summary.text, summary.level),
            "on_done": lambda code, stderr, run_id=run_id: self.signals.done_ready.emit(run_id, code, stderr),
        }
        if provider == "Claude":
            run_state = ClaudeRunState(
                prompt=prompt, claude_cmd=self.state.claude_cmd, cwd=str(cwd),
                resume_session_id=(role.session_id if role.session_provider == provider and not self.state.ephemeral else ""),
                model=role.model.strip() or self.state.model,
                extra_args=self.state.extra_args,
                accept_edits=self.state.bypass_approvals_and_sandbox,
            )
            self.runner = ClaudeExecRunner(run_state, **common_callbacks)
        elif provider == "Ollama":
            run_state = OllamaRunState(
                prompt=prompt,
                base_url=self.ollama_api_base_url(),
                model=role.model.strip() or self.state.model,
                keep_alive=self.state.ollama_keep_alive,
                request_timeout_seconds=self.state.ollama_request_timeout_seconds,
                command_timeout_seconds=self.state.ollama_command_timeout_seconds,
                verify_tls=self.state.ollama_verify_tls,
                ca_bundle_path=self.state.ollama_ca_bundle_path,
                context_length=self.state.ollama_context_length,
                expected_artifact_path=str(self.artifacts_dir() / role.artifact_name),
                expected_artifact_fingerprint=OllamaExecRunner._file_fingerprint(self.artifacts_dir() / role.artifact_name),
            )
            self.runner = OllamaExecRunner(
                run_state,
                execute_tool=self.execute_ollama_tool,
                bearer_token=self.ollama_token_input.text().strip(),
                **common_callbacks,
            )
        else:
            run_state = CodexRunState(
                prompt=prompt, codex_cmd=self.state.codex_cmd, cwd=str(cwd),
                resume_session_id=(role.session_id if role.session_provider == provider and not self.state.ephemeral else ""),
                model=role.model.strip() or self.state.model,
                model_reasoning_effort=role.model_reasoning_effort.strip(), sandbox=self.state.sandbox,
                extra_args=self.state.extra_args,
                bypass_approvals_and_sandbox=self.state.bypass_approvals_and_sandbox,
                skip_git_check=self.state.skip_git_check, ephemeral=self.state.ephemeral,
            )
            self.runner = CodexExecRunner(run_state, **common_callbacks)
        self.runner.start()

    def stop_run(self) -> None:
        if not self.is_run_active() and not self.runner:
            self.set_status("No agent process is running.")
            return
        self.run_lifecycle = "stopping"
        if self.runner:
            self.runner.stop()
        self.stop_ollama_tool_process()
        self.set_status("Stopping agent...")
        self.set_phase("Stopping", None, "Stopping active agent process.")

    def stop_all_agents(self) -> None:
        if self.runner:
            self.run_lifecycle = "stopping"
            self.runner.stop()
        self.stop_ollama_tool_process()

    @QtCore.pyqtSlot(int, int, str)
    def finish_run(self, run_id: int, returncode: int, stderr: str) -> None:
        if run_id != self.active_run_id:
            return
        role = self.active_role or self.current_role()
        elapsed = time.time() - self.run_started_at if self.run_started_at else 0.0
        stderr = stderr.strip()
        self.refresh_artifacts()
        self.clear_finished_runner(force=True)
        self.active_run_id = None
        self.run_lifecycle = "idle"

        if returncode == 0:
            role.status = "complete"
            self.append_activity(f"[{role.name.lower()} completed] exit=0 in {elapsed:.1f}s")
            self.set_status(f"{role.name} completed in {elapsed:.1f}s.")
            self.set_phase("Complete", role.name, f"Finished in {elapsed:.1f}s.")

            if self.pending_handoff is None:
                fallback_handoff = self.section_planner_fallback_handoff(role)
                if fallback_handoff:
                    self.pending_handoff = fallback_handoff
                    self.append_event("Created Section Planner fallback handoff from completed planning artifacts.", "warning")

            if self.pending_handoff:
                handoff = self.pending_handoff
                self.append_activity(
                    f"[handoff] {handoff.source_role} -> {handoff.target_role} ({len(handoff.body):,} chars)"
                )
                self.set_phase("Handoff Ready", handoff.source_role, f"Ready to relay to {handoff.target_role}.")
                if self.state.auto_advance:
                    self.pending_handoff = None
                    self.queue_handoff_relay(handoff)
                else:
                    self.set_status(
                        f"Handoff ready: {handoff.source_role} -> {handoff.target_role}. Click Relay Pending."
                    )
            elif role.allowed_handoffs is None and self.state.current_role_index < len(self.roles) - 1:
                self.state.current_role_index += 1
                self.current_role().status = "waiting"
                self._sync_role_editor()
                if self.state.auto_advance:
                    self.start_current_role()
        else:
            role.status = "blocked"
            if stderr:
                if "CreateProcessWithLogonW failed: 1327" in stderr:
                    self.append_activity(
                        "[diagnosis] Windows sandbox process launch failed. "
                        "Use Normal local shell mode for a normal-terminal style run."
                    )
                self.append_activity("[stderr]\n" + stderr)
            if stderr:
                first_line = stderr.splitlines()[0]
            elif self.last_run_error:
                first_line = self.last_run_error
            else:
                first_line = "no stderr output"
            self.append_activity(f"[{role.name.lower()} blocked] exit={returncode}: {first_line}")
            self.append_event(f"{role.name} exited with code {returncode}: {first_line}", "error")
            self.set_status(f"{role.name} exited with code {returncode} after {elapsed:.1f}s.")
            self.set_phase("Blocked", role.name, first_line)

        self.refresh_all_outputs()
        self.auto_save_session()

    @QtCore.pyqtSlot(int, dict)
    def handle_event(self, run_id: int, event: dict[str, Any]) -> None:
        if run_id != self.active_run_id:
            return
        if self.run_lifecycle == "launching":
            self.run_lifecycle = "running"
        role_name = self.active_role.name if self.active_role else self.current_role().name
        event_type = str(event.get("type", "unknown"))
        self.raw_lines.append(json.dumps({"role": role_name, **event}, ensure_ascii=False))
        self.raw_lines = self.raw_lines[-MAX_RAW_LINES:]
        self.raw_output.setPlainText(self.log_text(self.raw_lines))

        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or event.get("id") or "")
            role = self.role_by_name(role_name)
            provider = (role.provider if role and role.provider else self.state.provider)
            had_session = bool(role and role.session_id and role.session_provider == provider and provider != "Ollama")
            if role and thread_id and provider != "Ollama":
                role.session_id = thread_id
                role.session_provider = provider
                self.auto_save_session()
            self.append_event(f"{role_name}: thread started {thread_id}".strip())
            mode = "resumed" if had_session else "started"
            detail = f"{provider} run {mode}." if provider == "Ollama" else f"{provider} thread {mode}."
            self.set_phase("Connected", role_name, detail)
            return
        if event_type == "turn.started":
            self.append_event(f"{role_name}: turn started")
            self.set_phase("Thinking", role_name, "Codex is reasoning.")
            return
        if event_type == "turn.completed":
            usage = event.get("usage")
            suffix = " | usage " + json.dumps(usage, ensure_ascii=False) if usage else ""
            self.append_event(f"{role_name}: turn completed{suffix}")
            if self.pending_handoff and self.state.auto_advance:
                self.set_status(
                    f"{role_name} completed; waiting for Codex process exit before relaying handoff."
                )
                self.set_phase("Finalizing", role_name, "Waiting for Codex process exit before relay.")
            else:
                self.set_phase("Finalizing", role_name, "Codex turn completed.")
            return
        if event_type in {"turn.failed", "thread.error", "error"}:
            detail = self.extract_error_detail(event) or self.compact_json(event)
            self.last_run_error = detail
            self.append_activity(f"[{role_name.lower()} {event_type}] {detail}")
            self.append_event(f"{role_name}: {event_type}: {self.compact_json(event)}", "error")
            self.set_phase("Error", role_name, detail)
            return
        if event_type.startswith("item."):
            self.handle_item_event(role_name, event_type, event)
            return
        self.append_event(f"{role_name}: {event_type}: {self.compact_json(event)}")

    def handle_item_event(self, role_name: str, event_type: str, event: dict[str, Any]) -> None:
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        item_type = str(item.get("type") or item.get("item_type") or item.get("kind") or "item")
        status = str(item.get("status") or event_type.rsplit(".", 1)[-1])
        is_start_event = event_type == "item.started" or status in {"started", "in_progress", "running"}

        if item_type == "agent_message":
            text = self.extract_text(item)
            if text:
                self.final_text_by_role[role_name] = text
                self.append_conversation(role_name, text)
                self.capture_handoff_text(role_name, text)
                self.set_phase("Responding", role_name, f"Received agent message ({len(text):,} chars).")
                self.append_event(f"{role_name}: agent message ({len(text):,} chars)")
            else:
                self.append_event(f"{role_name}: agent message")
            return

        if item_type == "command_execution":
            command = item.get("command") or item.get("cmd") or item.get("display_command") or "command"
            exit_code = item.get("exit_code")
            if is_start_event:
                self.set_phase("Working", role_name, str(command))
                self.append_event(f"{role_name}: command started")
                return
            self.append_activity(f"[{role_name.lower()} {status} exit={exit_code}] {command}")
            self.set_phase("Working", role_name, str(command))
            output = item.get("aggregated_output") or item.get("output") or item.get("stdout")
            if output:
                self.append_command_output(role_name, str(output).rstrip())
            if self.command_failed(status, exit_code):
                self.record_item_warning(role_name, f"command {status} exit={exit_code}: {command}")
            self.append_event(f"{role_name}: command_execution {status}")
            return

        if item_type == "file_change":
            if is_start_event:
                self.set_phase("Editing", role_name, "File change in progress.")
                self.append_event(f"{role_name}: file_change started")
                return
            changes = item.get("changes")
            if isinstance(changes, list) and changes:
                for change in changes:
                    if isinstance(change, dict):
                        kind = change.get("kind") or change.get("type") or "change"
                        path = change.get("path") or change.get("file") or ""
                        self.append_activity(f"[{role_name.lower()} file {kind}] {path}".rstrip())
            else:
                self.append_activity(f"[{role_name.lower()} file_change] {self.compact_json(item)}")
            self.append_event(f"{role_name}: file_change {status}")
            self.set_phase("Editing", role_name, "File changes detected.")
            return

        if item_type in {"error", "failure"}:
            self.record_item_warning(role_name, self.compact_json(item))
            return

        if item_type in {"mcp_tool_call", "web_search", "todo_list"}:
            self.append_activity(f"[{role_name.lower()} {item_type}] {self.compact_json(item)}")
            self.append_event(f"{role_name}: {item_type}")
            self.set_phase("Using Tool", role_name, item_type)
            return

        self.append_event(f"{role_name}: {event_type}: {item_type}")

    def build_agent_prompt(self, role: AgentRole, relay_message: str = "") -> str:
        artifact_name, warning = safe_artifact_name(role.artifact_name, role.name)
        if warning:
            role.artifact_name = artifact_name
            self.append_event(warning, "warning")
        artifacts = self.read_artifact_context()
        handoff_rules = self.handoff_instructions(role)
        relay_block = ""
        if relay_message.strip():
            relay_block = f"\nDirect message relayed to you:\n{relay_message.strip()}\n"
        return (
            f"You are the {role.name} agent in a file-backed multi-agent Codex workflow.\n\n"
            f"User goal:\n{self.state.goal.strip()}\n\n"
            f"{relay_block}"
            f"Your role instruction:\n{role.prompt.strip()}\n\n"
            "Use the workspace as the source of truth. Store handoff markdown in the artifacts folder.\n"
            "Read any needed artifact files directly from disk; artifact contents are not pasted here.\n"
            "Workspace commands run through Windows PowerShell. Use PowerShell syntax and `python`, not bash commands such as grep, wc, cat, or python3.\n"
            "For source, HTML, CSS, or configuration edits, use apply_workspace_patch after reading the file. "
            "Use write_workspace_file only to create a new file or replace an entire file. Do not use run_workspace_command "
            "for multi-line scripts or nested PowerShell commands to edit files.\n"
            f"Expected artifact: artifacts/{artifact_name}\n\n"
            "Completion contract: create the expected artifact with the available workspace tools before finishing. "
            "Do not claim completion or return a text-only answer until artifacts/"
            f"{artifact_name} exists.\n\n"
            f"{handoff_rules}\n\n"
            f"Existing artifacts:\n{artifacts if artifacts else '(none yet)'}\n"
        )

    def allowed_handoff_targets(self, role: AgentRole) -> list[AgentRole]:
        targets = [target for target in self.roles if target.name != role.name]
        if role.allowed_handoffs is None:
            return targets
        allowed = set(role.allowed_handoffs)
        return [target for target in targets if target.name in allowed]

    def section_planner_fallback_handoff(self, role: AgentRole) -> Handoff | None:
        """Bridge older planner outputs that wrote a handoff file but omitted a marker."""
        if role.name != "Section Planner" or self.role_index("Section Manager") is None:
            return None
        index_path = self.artifacts_dir() / "sections" / "INDEX.md"
        if not index_path.is_file():
            return None
        handoff_path = self.artifacts_dir() / "SECTION_PLANNER_HANDOFF.md"
        try:
            body = handoff_path.read_text(encoding="utf-8", errors="replace").strip() if handoff_path.is_file() else ""
            if not body:
                body = (
                    "Planning artifacts are ready. Read artifacts/sections/INDEX.md, start the earliest eligible pending section, "
                    "and dispatch it to Section Implementer."
                )
        except OSError as exc:
            self.append_event(f"Could not read Section Planner fallback handoff: {exc}", "warning")
            return None
        return Handoff("Section Planner", "Section Manager", handoff_marker("Section Manager"), body[:HANDOFF_MAX_LENGTH])

    def handoff_instructions(self, role: AgentRole) -> str:
        targets = self.allowed_handoff_targets(role)
        markers = ", ".join(f"{target.name}: {handoff_marker(target.name)}" for target in targets)
        section_rule = (
            "- Sectioned workflow roles must put a valid marker in their final response; a Markdown handoff file alone is not a handoff.\n"
            if role.name.startswith("Section ")
            else ""
        )
        return (
            "Agent handoff protocol:\n"
            f"- To pass work to another agent, write that agent's marker, then the direct message body, then {HANDOFF_END}, then {HANDOFF_DONE}.\n"
            f"- Available target markers: {markers if markers else '(none enabled for this role)'}.\n"
            f"- To end an approved review loop, write {APPROVED} then {HANDOFF_DONE}.\n"
            f"{section_rule}"
            "- The orchestrator relays only the body inside the handoff block."
        )

    def relay_handoff(self, handoff: Handoff) -> None:
        self.queue_handoff_relay(handoff)

    def queue_handoff_relay(self, handoff: Handoff) -> None:
        target_index = self.role_index(handoff.target_role)
        if target_index is None:
            self.append_event(f"Unknown handoff target: {handoff.target_role}", "error")
            return
        self.state.current_role_index = target_index
        target = self.current_role()
        target.status = "waiting"
        self._sync_role_editor()
        self.pending_relay_message = handoff.body
        self.set_status(f"Relaying {handoff.source_role} message to {handoff.target_role}...")
        self.set_phase("Relaying", handoff.target_role, f"Message from {handoff.source_role}.")
        self.append_event(f"Relay queued: {handoff.source_role} -> {handoff.target_role}")
        self.launch_queued_relay()

    def launch_queued_relay(self, attempt: int = 0) -> None:
        self.clear_finished_runner()
        if self.is_run_active():
            if attempt >= 20:
                self.set_status("Relay is still waiting for the previous agent to exit.")
                self.set_phase("Relay Waiting", self.current_role().name, "Previous Codex process has not exited.")
                return
            self.set_status(f"Relay queued; waiting for previous agent to exit ({attempt + 1}).")
            self.set_phase("Relay Queued", self.current_role().name, "Waiting for previous process shutdown.")
            QtCore.QTimer.singleShot(250, lambda: self.launch_queued_relay(attempt + 1))
            return

        self.append_event(f"Launching relayed agent: {self.current_role().name}")
        self.start_current_role()

    @QtCore.pyqtSlot(int, str, str)
    def handle_runner_log(self, run_id: int, text: str, level: str) -> None:
        if run_id != self.active_run_id:
            return
        self.append_event(text, level)

    def clear_finished_runner(self, force: bool = False) -> None:
        if force or (self.runner and self.runner.process and self.runner.process.poll() is not None):
            self.runner = None

    def relay_pending_handoff(self) -> None:
        if not self.pending_handoff:
            self.set_status("No pending handoff to relay.")
            return

        if self.is_run_active():
            self.set_status("Current agent is still running; pending handoff will relay when it exits.")
            return

        handoff = self.pending_handoff
        self.pending_handoff = None
        self.append_activity(
            f"[handoff] {handoff.source_role} -> {handoff.target_role} ({len(handoff.body):,} chars)"
        )
        self.relay_handoff(handoff)

    def capture_handoff_text(self, role_name: str, text: str) -> None:
        buffer = self.agent_buffers.get(role_name, "")
        buffer = (buffer + "\n" + text)[-60000:]
        self.agent_buffers[role_name] = buffer

        if role_name.lower() == "reviewer" and self.has_approved_done(buffer):
            self.pending_handoff = None
            self.append_activity("[approved] Reviewer approved the workflow.")
            self.set_status("Reviewer approved the workflow.")
            return

        handoff = self.extract_best_handoff(role_name, buffer)
        if not handoff:
            return

        key = self.handoff_key(handoff)
        if key in self.seen_handoffs:
            self.append_event(f"Duplicate handoff ignored: {handoff.source_role} -> {handoff.target_role}")
            return
        self.seen_handoffs.add(key)
        if len(self.seen_handoffs) > 200:
            self.seen_handoffs = set(list(self.seen_handoffs)[-100:])

        self.pending_handoff = handoff
        self.agent_buffers[role_name] = ""
        self.append_event(f"Handoff detected: {handoff.source_role} -> {handoff.target_role}")
        self.set_phase("Handoff Detected", role_name, f"Target: {handoff.target_role}.")

    def extract_best_handoff(self, source_role: str, buffer: str) -> Handoff | None:
        candidates: list[tuple[int, Handoff]] = []
        source = self.role_by_name(source_role)
        targets = self.allowed_handoff_targets(source) if source else [target for target in self.roles if target.name != source_role]
        for target in targets:
            marker = handoff_marker(target.name)
            for start, body, between_end_and_done in extract_handoff_candidates(buffer, marker):
                if len(between_end_and_done) > 80:
                    continue
                if body and self.validate_handoff_body(source_role, marker, body):
                    candidates.append((start, Handoff(source_role, target.name, marker, body)))
        if not candidates:
            return None
        candidates.sort(key=lambda item: item[0])
        return candidates[-1][1]

    def extract_latest_valid_handoff(self, buffer: str, marker: str) -> str:
        return extract_latest_valid_handoff(buffer, marker)

    def validate_handoff_body(self, source_role: str, marker: str, body: str) -> bool:
        reason = handoff_rejection_reason(body)
        if reason:
            self.append_event(f"Handoff rejected from {source_role} via {marker}: {reason}", "warning")
            return False
        return True

    def role_index(self, role_name: str) -> int | None:
        for index, role in enumerate(self.roles):
            if role.name.lower() == role_name.lower():
                return index
        return None

    def role_by_name(self, role_name: str) -> AgentRole | None:
        index = self.role_index(role_name)
        return self.roles[index] if index is not None else None

    @staticmethod
    def handoff_marker(role_name: str) -> str:
        return handoff_marker(role_name)

    @staticmethod
    def has_approved_done(buffer: str) -> bool:
        return has_approved_done(buffer)

    @staticmethod
    def handoff_key(handoff: Handoff) -> str:
        body = " ".join(handoff.body.lower().split())
        return f"{handoff.source_role}|{handoff.marker}|{body}"

    @staticmethod
    def extract_error_detail(event: dict[str, Any]) -> str:
        error = event.get("error")
        if isinstance(error, dict):
            message = str(error.get("message") or "").strip()
            if message:
                return message
        elif isinstance(error, str) and error.strip():
            return error.strip()
        return str(event.get("message") or "").strip()

    @staticmethod
    def command_failed(status: str, exit_code: Any) -> bool:
        normalized = status.lower()
        if normalized in {"failed", "failure", "error", "errored", "cancelled"}:
            return True
        if exit_code in (None, "", 0, "0"):
            return False
        return True

    def record_item_warning(self, role_name: str, detail: str) -> None:
        text = detail.strip() or "item warning"
        self.append_activity(f"[{role_name.lower()} warning] {text}")
        self.append_event(f"{role_name}: {text}", "warning")
        self.set_phase("Warning", role_name, text)

    def read_artifact_context(self) -> str:
        parts = []
        artifact_dir = self.artifacts_dir()
        for role in self.roles:
            artifact_name, warning = safe_artifact_name(role.artifact_name, role.name)
            if warning:
                role.artifact_name = artifact_name
                self.append_event(warning, "warning")
            path = artifact_dir / artifact_name
            artifact_ref = f"artifacts/{artifact_name}"
            if not path.exists():
                parts.append(f"missing  {artifact_ref}")
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                parts.append(f"error    {artifact_ref} ({exc})")
                continue
            parts.append(f"ok       {artifact_ref} ({size:,} bytes)")
        return "\n\n".join(parts)

    def append_command_output(self, role_name: str, output: str) -> None:
        if len(output) <= MAX_INLINE_ACTIVITY_OUTPUT_CHARS:
            self.append_activity(output)
            return
        preview = output[:INLINE_ACTIVITY_OUTPUT_PREVIEW_CHARS].rstrip()
        saved, truncated_save = bounded_command_output(output)
        try:
            output_dir = self.artifacts_dir() / ".workbench-command-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            slug = session_slug(role_name)
            path = output_dir / f"{timestamp}-{slug}.txt"
            suffix = 2
            while path.exists():
                path = output_dir / f"{timestamp}-{slug}-{suffix}.txt"
                suffix += 1
            path.write_text(saved, encoding="utf-8", errors="replace")
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            self.prune_command_output_files(output_dir)
        except OSError as exc:
            self.append_activity(preview)
            self.append_activity(
                f"[{role_name.lower()} output save failed] {exc}; showing first "
                f"{len(preview):,} of {len(output):,} chars only."
            )
            return
        relative = path.relative_to(self.current_workspace())
        self.append_activity(preview)
        if truncated_save:
            saved_note = f"head and tail ({len(saved):,} chars)"
        else:
            saved_note = f"all {len(saved):,} chars"
        self.append_activity(
            f"[{role_name.lower()} output truncated] showing first "
            f"{INLINE_ACTIVITY_OUTPUT_PREVIEW_CHARS:,} of {len(output):,} chars; "
            f"{saved_note} saved to {relative}; keeping newest {MAX_COMMAND_OUTPUT_FILES} output files"
        )

    @staticmethod
    def prune_command_output_files(output_dir: Path) -> None:
        try:
            files = sorted(
                [path for path in output_dir.glob("*.txt") if path.is_file()],
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        total_bytes = 0
        kept = 0
        for path in files:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            kept += 1
            total_bytes += size
            if kept <= MAX_COMMAND_OUTPUT_FILES and total_bytes <= MAX_COMMAND_OUTPUT_TOTAL_BYTES:
                continue
            try:
                path.unlink()
            except OSError:
                pass

    def refresh_artifacts(self) -> None:
        artifact_dir = self.artifacts_dir()
        self._last_artifact_refresh = time.monotonic()
        status_lines = []
        for spec in ARTIFACT_SPECS:
            artifact_name = spec.name
            output = self.artifact_outputs.get(artifact_name)
            path = artifact_dir / artifact_name
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                if output:
                    output.setPlainText(text)
                status_lines.append(f"ok       artifacts/{artifact_name} ({len(text):,} chars)")
            else:
                if output:
                    output.setPlainText(f"artifacts/{artifact_name} has not been created yet.")
                status_lines.append(f"missing  artifacts/{artifact_name}")
        self.artifact_status_output.setPlainText("\n".join(status_lines))

    def refresh_all_outputs(self) -> None:
        self._refresh_role_list()
        self.current_output.setPlainText(self.current_step_text())
        self.pipeline_output.setPlainText(self.pipeline_text())
        self.handoff_output.setPlainText(self.handoff_status_text())
        self.conversation_output.setPlainText(self.conversation_text())
        self.activity_output.setPlainText(self.log_text(self.activity_lines))
        self.events_output.setPlainText(self.log_text(self.event_lines))
        self.raw_output.setPlainText(self.log_text(self.raw_lines))
        if time.monotonic() - self._last_artifact_refresh > 1.0:
            self.refresh_artifacts()

    def current_step_text(self) -> str:
        lines = []
        for index, role in enumerate(self.roles, start=1):
            marker = ">" if index - 1 == self.state.current_role_index else " "
            memory = "session" if role.session_id else "new"
            lines.append(f"{marker} {index}. {role.name:<12} {role.status:<9} {memory}")
        role = self.current_role()
        effective_model = role.model.strip() or self.state.model.strip() or "global/default"
        effective_effort = role.model_reasoning_effort.strip() or "global/default"
        effective_provider = role.provider or self.state.provider
        lines.append("")
        lines.append("Current role")
        lines.append(role.name)
        lines.append("")
        lines.append("Model")
        lines.append(effective_model)
        lines.append("")
        lines.append("Provider")
        lines.append(effective_provider)
        lines.append("")
        lines.append("Effort")
        lines.append(effective_effort)
        lines.append("")
        lines.append("Role instruction")
        lines.append(role.prompt)
        return "\n".join(lines)

    def pipeline_text(self) -> str:
        lines = []
        for index, role in enumerate(self.roles, start=1):
            active = ">" if index - 1 == self.state.current_role_index else " "
            session = "session" if role.session_id else "new"
            lines.append(f"{active} {index}. {role.name:<12} {role.status:<9} {session}")
        return "\n".join(lines)

    def handoff_status_text(self) -> str:
        lines = [
            f"auto-relay: {'on' if self.state.auto_advance else 'off'}",
            f"phase: {self.phase}",
        ]
        if self.pending_handoff:
            handoff = self.pending_handoff
            lines.extend(
                [
                    f"source: {handoff.source_role}",
                    f"target: {handoff.target_role}",
                    f"body: {len(handoff.body):,} chars",
                    "status: ready",
                ]
            )
        elif self.pending_relay_message:
            lines.extend(
                [
                    f"target: {self.current_role().name}",
                    f"body: {len(self.pending_relay_message):,} chars",
                    "status: queued",
                ]
            )
        else:
            lines.append("status: none")
        return "\n".join(lines)

    def clear_outputs(self) -> None:
        self.conversation_lines.clear()
        self.activity_lines.clear()
        self.event_lines.clear()
        self.raw_lines.clear()
        self.final_text_by_role.clear()
        self.agent_buffers.clear()
        self.seen_handoffs.clear()
        self.pending_handoff = None
        self.pending_relay_message = ""
        self.set_status("Cleared.")
        self.set_phase("Idle", None, "No agent is running.")
        self.refresh_all_outputs()

    def append_conversation(self, speaker: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.conversation_lines.append(f"{speaker}:\n{text}")
        self.conversation_lines = self.conversation_lines[-120:]
        self.conversation_output.setPlainText(self.conversation_text())

    def append_activity(self, text: str) -> None:
        self.activity_lines.append(text)
        self.activity_lines = self.activity_lines[-MAX_LOG_LINES:]
        self.activity_output.setPlainText(self.log_text(self.activity_lines))

    @QtCore.pyqtSlot(str, str)
    def append_event(self, text: str, level: str = "info") -> None:
        prefix = time.strftime("%H:%M:%S")
        if level != "info":
            prefix = f"{prefix} {level.upper()}"
        self.event_lines.append(f"{prefix}  {text}")
        self.event_lines = self.event_lines[-MAX_LOG_LINES:]
        self.events_output.setPlainText(self.log_text(self.event_lines))

    PHASE_DONE = {"Complete"}
    PHASE_ERROR = {"Blocked", "Error", "Warning"}
    PHASE_WAITING = {"Handoff Ready"}

    def set_phase(self, phase: str, agent_name: str | None, detail: str) -> None:
        self.phase = phase
        self.phase_detail = detail
        self.phase_label.setText(phase)
        self.agent_label.setText(f"Agent: {agent_name or 'none'}")
        self.detail_label.setText(detail)
        if phase in self.PHASE_DONE:
            self.status_indicator.set_done()
        elif phase in self.PHASE_ERROR:
            self.status_indicator.set_error()
        elif phase == "Idle":
            self.status_indicator.set_idle()
        elif phase in self.PHASE_WAITING:
            self.status_indicator.set_waiting()
        else:
            self.status_indicator.set_busy()
        if hasattr(self, "pipeline_output"):
            self.pipeline_output.setPlainText(self.pipeline_text())
        if hasattr(self, "handoff_output"):
            self.handoff_output.setPlainText(self.handoff_status_text())

    @staticmethod
    def log_text(lines: list[str]) -> str:
        return "\n".join(reversed(lines))

    def conversation_text(self) -> str:
        return "\n\n".join(reversed(self.conversation_lines))

    def select_role(self, index: int) -> None:
        self.state.current_role_index = index
        self._sync_role_editor()
        self.refresh_all_outputs()
        self.set_status(f"Selected {self.current_role().name}.")

    def _sync_role_editor(self) -> None:
        role = self.current_role()
        self.role_prompt_input.blockSignals(True)
        self.role_prompt_input.setPlainText(role.prompt)
        self.role_prompt_input.blockSignals(False)
        self.role_name_input.blockSignals(True)
        self.role_name_input.setText(role.name)
        self.role_name_input.blockSignals(False)
        self.role_artifact_input.blockSignals(True)
        self.role_artifact_input.setText(role.artifact_name)
        self.role_artifact_input.blockSignals(False)
        self.role_model_input.blockSignals(True)
        self.role_model_input.setText(role.model)
        self.role_model_input.blockSignals(False)
        self.role_provider_combo.blockSignals(True)
        self.role_provider_combo.setCurrentText(role.provider or ROLE_PROVIDER_DEFAULT_LABEL)
        self.role_provider_combo.blockSignals(False)
        self.role_effort_combo.blockSignals(True)
        self.role_effort_combo.setCurrentText(role.model_reasoning_effort)
        self.role_effort_combo.blockSignals(False)
        self.role_list.blockSignals(True)
        self.role_list.setCurrentRow(self.state.current_role_index)
        self.role_list.blockSignals(False)
        self._sync_handoff_targets_editor()

    def _sync_handoff_targets_editor(self) -> None:
        if not hasattr(self, "handoff_targets_list"):
            return
        role = self.current_role()
        allowed = role.allowed_handoffs
        allowed_set = set(allowed or [])
        self.handoff_targets_list.blockSignals(True)
        self.handoff_targets_list.clear()
        for target in self.roles:
            if target.name == role.name:
                continue
            item = QtWidgets.QListWidgetItem(target.name)
            item.setFlags(item.flags() | QtCore.Qt.ItemFlag.ItemIsUserCheckable)
            checked = allowed is None or target.name in allowed_set
            item.setCheckState(QtCore.Qt.CheckState.Checked if checked else QtCore.Qt.CheckState.Unchecked)
            self.handoff_targets_list.addItem(item)
        self.handoff_targets_list.blockSignals(False)

    def _selected_handoff_targets(self) -> list[str]:
        selected: list[str] = []
        if not hasattr(self, "handoff_targets_list"):
            return selected
        for index in range(self.handoff_targets_list.count()):
            item = self.handoff_targets_list.item(index)
            if item.checkState() == QtCore.Qt.CheckState.Checked:
                selected.append(item.text())
        return selected

    def _refresh_role_list(self) -> None:
        self.role_list.blockSignals(True)
        self.role_list.clear()
        for index, role in enumerate(self.roles, start=1):
            self.role_list.addItem(f"{index}. {role.name} ({role.status})")
        self.role_list.setCurrentRow(self.state.current_role_index)
        self.role_list.blockSignals(False)

    def on_role_list_selection(self, index: int) -> None:
        if index < 0 or index >= len(self.roles):
            return
        self.select_role(index)

    def add_role(self) -> None:
        name, ok = QtWidgets.QInputDialog.getText(self, "Add Role", "Role name:")
        if not ok or not name.strip():
            return
        name = name.strip()
        if self.role_index(name) is not None:
            QtWidgets.QMessageBox.warning(self, "Add Role", f"A role named '{name}' already exists.")
            return
        slug = "".join(ch if ch.isalnum() else "_" for ch in name.upper()).strip("_") or "ROLE"
        new_role = AgentRole(name, f"{slug}.md", f"Describe what the {name} agent should do.")
        insert_at = self.state.current_role_index + 1
        self.roles.insert(insert_at, new_role)
        self.state.current_role_index = insert_at
        self._refresh_role_list()
        self._rebuild_role_status_buttons()
        self._sync_role_editor()
        self.refresh_all_outputs()
        self.set_status(f"Added role '{name}'.")

    def import_role(self) -> None:
        if not self.role_library:
            self.set_status("No saved roles yet. Use 'Save Role to Library' first.")
            return

        names = sorted(self.role_library.keys())
        picked, ok = QtWidgets.QInputDialog.getItem(
            self, "Import Saved Role", "Choose a saved role:", names, editable=False
        )
        if not ok or not picked:
            return

        entry = self.role_library[picked]
        name = picked
        if self.role_index(name) is not None:
            base_name, suffix = name, 2
            while self.role_index(name) is not None:
                name = f"{base_name} ({suffix})"
                suffix += 1

        new_role = AgentRole(
            name,
            entry["artifact_name"],
            entry["prompt"],
            allowed_handoffs=optional_string_list(entry, "allowed_handoffs"),
            model=str(entry.get("model") or ""),
            model_reasoning_effort=str(entry.get("model_reasoning_effort") or ""),
            provider=str(entry.get("provider") or "") if str(entry.get("provider") or "") in PROVIDERS else "",
        )
        insert_at = self.state.current_role_index + 1
        self.roles.insert(insert_at, new_role)
        self.state.current_role_index = insert_at
        self._refresh_role_list()
        self._rebuild_role_status_buttons()
        self._sync_role_editor()
        self.refresh_all_outputs()
        self.set_status(f"Imported saved role '{name}'.")

    def save_role(self) -> None:
        role = self.current_role()
        self.role_library[role.name] = role_library_entry(role)
        save_library(self.role_library, self.presets)
        self.set_status(f"Saved role '{role.name}' to the library.")

    def delete_role_from_library(self) -> None:
        role = self.current_role()
        name = role.name
        if name not in self.role_library:
            self.set_status(f"'{name}' is not saved in the role library.")
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete Role",
            f"Delete '{name}' from the role library? It stays in this pipeline until removed separately.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        del self.role_library[name]
        save_library(self.role_library, self.presets)
        self.set_status(f"Deleted '{name}' from the role library.")

    def remove_role(self) -> None:
        if len(self.roles) <= 1:
            self.set_status("At least one role is required.")
            return
        index = self.state.current_role_index
        removed = self.roles.pop(index)
        self.state.current_role_index = min(index, len(self.roles) - 1)
        self._refresh_role_list()
        self._rebuild_role_status_buttons()
        self._sync_role_editor()
        self.refresh_all_outputs()
        self.set_status(f"Removed role '{removed.name}'.")

    def move_role(self, delta: int) -> None:
        index = self.state.current_role_index
        new_index = index + delta
        if new_index < 0 or new_index >= len(self.roles):
            return
        self.roles[index], self.roles[new_index] = self.roles[new_index], self.roles[index]
        self.state.current_role_index = new_index
        self._refresh_role_list()
        self._rebuild_role_status_buttons()
        self._sync_role_editor()
        self.refresh_all_outputs()

    def on_role_name_changed(self, value: str) -> None:
        self.current_role().name = value
        self._refresh_role_list()
        self._rebuild_role_status_buttons()
        self._sync_handoff_targets_editor()
        self.refresh_all_outputs()

    def on_role_artifact_changed(self, value: str) -> None:
        role = self.current_role()
        artifact_name, warning = safe_artifact_name(value, role.name)
        role.artifact_name = artifact_name
        if warning:
            self.append_event(warning, "warning")
            self.role_artifact_input.blockSignals(True)
            self.role_artifact_input.setText(artifact_name)
            self.role_artifact_input.blockSignals(False)
        self.refresh_all_outputs()

    def on_role_model_changed(self, value: str) -> None:
        self.current_role().model = value.strip()
        self.current_output.setPlainText(self.current_step_text())

    def on_role_provider_changed(self, value: str) -> None:
        self.current_role().provider = value if value in PROVIDERS else ""
        self.current_output.setPlainText(self.current_step_text())

    def on_role_effort_changed(self, value: str) -> None:
        self.current_role().model_reasoning_effort = value.strip()
        self.current_output.setPlainText(self.current_step_text())

    def on_handoff_targets_changed(self, _item: QtWidgets.QListWidgetItem) -> None:
        role = self.current_role()
        target_names = [target.name for target in self.roles if target.name != role.name]
        selected = self._selected_handoff_targets()
        if set(selected) == set(target_names):
            role.allowed_handoffs = None
        else:
            role.allowed_handoffs = selected
        self.current_output.setPlainText(self.current_step_text())
        self.handoff_output.setPlainText(self.handoff_status_text())

    def _sync_preset_dropdowns(self, selected: str | None = None) -> None:
        if selected is not None:
            self.current_preset_name = selected
        elif self.current_preset_name not in self.presets:
            self.current_preset_name = next(iter(sorted(self.presets)), "")
        for dropdown in self._preset_dropdowns:
            dropdown.blockSignals(True)
            dropdown.clear()
            dropdown.addItems(sorted(self.presets.keys()))
            if self.current_preset_name:
                dropdown.setCurrentText(self.current_preset_name)
            dropdown.blockSignals(False)

    def load_selected_preset(self, dropdown: QtWidgets.QComboBox | None = None) -> None:
        dropdown = dropdown or self.preset_dropdown
        name = dropdown.currentText()
        order = self.presets.get(name)
        if order is None:
            self.set_status(f"Preset '{name}' not found.")
            return
        if self.reject_if_run_active("switch presets"):
            return

        missing = [role_name for role_name in order if role_name not in self.role_library]
        roles = [
            AgentRole(
                role_name,
                self.role_library[role_name]["artifact_name"],
                self.role_library[role_name]["prompt"],
                allowed_handoffs=optional_string_list(self.role_library[role_name], "allowed_handoffs"),
                model=str(self.role_library[role_name].get("model") or ""),
                model_reasoning_effort=str(self.role_library[role_name].get("model_reasoning_effort") or ""),
                provider=(str(self.role_library[role_name].get("provider") or "")
                          if str(self.role_library[role_name].get("provider") or "") in PROVIDERS else ""),
            )
            for role_name in order
            if role_name in self.role_library
        ]
        if not roles:
            self.set_status(f"Preset '{name}' has no roles left in the library.")
            return

        self.roles = roles
        self.state.current_role_index = 0
        self.pending_handoff = None
        self.pending_relay_message = ""
        self._sync_preset_dropdowns(name)
        self._refresh_role_list()
        self._rebuild_role_status_buttons()
        self._sync_role_editor()
        self.refresh_all_outputs()
        note = f" (missing from library: {', '.join(missing)})" if missing else ""
        self.set_status(f"Loaded preset '{name}' ({len(self.roles)} roles){note}.")

    def save_preset_as(self, dropdown: QtWidgets.QComboBox | None = None) -> None:
        dropdown = dropdown or self.preset_dropdown
        default_name = dropdown.currentText() or "My Preset"
        name, ok = QtWidgets.QInputDialog.getText(self, "Save Preset", "Preset name:", text=default_name)
        if not ok or not name.strip():
            return
        name = name.strip()
        for role in self.roles:
            self.role_library[role.name] = role_library_entry(role)
        self.presets[name] = [role.name for role in self.roles]
        save_library(self.role_library, self.presets)
        self.current_preset_name = name
        self._sync_preset_dropdowns(name)
        self.set_status(f"Saved preset '{name}' ({len(self.roles)} roles).")

    def delete_selected_preset(self, dropdown: QtWidgets.QComboBox | None = None) -> None:
        dropdown = dropdown or self.preset_dropdown
        name = dropdown.currentText()
        if name not in self.presets:
            return
        if len(self.presets) <= 1:
            self.set_status("At least one preset must remain.")
            return
        confirm = QtWidgets.QMessageBox.question(
            self,
            "Delete Preset",
            f"Delete preset '{name}'? Its roles stay in the role library.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        del self.presets[name]
        save_library(self.role_library, self.presets)
        if self.current_preset_name == name:
            self.current_preset_name = "Default Pipeline" if "Default Pipeline" in self.presets else next(iter(sorted(self.presets)), "")
        self._sync_preset_dropdowns()
        self.set_status(f"Deleted preset '{name}'.")

    def new_workflow(self) -> None:
        if self.reject_if_run_active("start a new workflow"):
            return
        self.session_file = None
        self.refresh_session_list()
        if "Default Pipeline" in self.presets:
            self._sync_preset_dropdowns("Default Pipeline")
            self.load_selected_preset()
        self.clear_agent_sessions()
        self.clear_outputs()

    def duplicate_workflow(self) -> None:
        self.save_preset_as()

    def on_goal_changed(self) -> None:
        self.state.goal = self.goal_input.toPlainText()

    def on_role_prompt_changed(self) -> None:
        self.current_role().prompt = self.role_prompt_input.toPlainText()
        self.current_output.setPlainText(self.current_step_text())

    def on_codex_cmd_changed(self, value: str) -> None:
        self.state.codex_cmd = value

    def on_claude_cmd_changed(self, value: str) -> None:
        self.state.claude_cmd = value

    def detect_codex_command(self) -> None:
        self.state.codex_cmd = resolve_codex_command()
        self.codex_cmd_input.setText(self.state.codex_cmd)
        self.set_status(f"Using Codex command: {self.state.codex_cmd}")

    def on_cwd_changed(self, value: str) -> None:
        old_cwd = self.state.cwd
        self.state.cwd = value
        if value != old_cwd:
            self.session_file = None
            self.saved_session_paths = []
            self.clear_agent_sessions()
            self.refresh_session_list()
        self.refresh_artifacts()

    def use_app_folder(self) -> None:
        self.state.cwd = str(app_dir())
        self.session_file = None
        self.saved_session_paths = []
        self.clear_agent_sessions()
        self.workspace_input.setText(self.state.cwd)
        self.refresh_session_list()
        self.refresh_artifacts()
        self.set_status(f"Workspace set to {self.state.cwd}")

    def clear_agent_sessions(self) -> None:
        for role in self.roles:
            role.session_id = ""
            role.session_provider = ""
        self.append_event("Workspace changed; cleared per-agent session ids.")

    def ollama_api_base_url(self) -> str:
        value = self.state.ollama_base_url.strip().rstrip("/")
        if not value:
            return "http://localhost:11434/api"
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("Ollama URL must include http:// or https:// and a host.")
        return value if parsed.path.rstrip("/").endswith("/api") else value + "/api"

    def confirm_ollama_connection(self) -> bool:
        try:
            base_url = self.ollama_api_base_url()
        except ValueError as exc:
            self.set_status(str(exc))
            return False
        parsed = urllib.parse.urlparse(base_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme == "http" and host not in {"localhost", "127.0.0.1", "::1"}:
            answer = QtWidgets.QMessageBox.warning(
                self,
                "Plaintext Ollama Connection",
                "This network Ollama endpoint uses HTTP. Prompts and responses can be read by other parties on the network. Continue?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            return answer == QtWidgets.QMessageBox.StandardButton.Yes
        return True

    def on_ollama_mode_changed(self, _index: int) -> None:
        self.state.ollama_mode = str(self.ollama_mode_combo.currentData() or "local")
        if self.state.ollama_mode == "local" and not self.state.ollama_base_url.strip():
            self.state.ollama_base_url = "http://localhost:11434/api"
            self.ollama_url_input.setText(self.state.ollama_base_url)

    def on_ollama_url_changed(self, value: str) -> None:
        self.state.ollama_base_url = value.strip()

    def on_ollama_keep_alive_changed(self, value: str) -> None:
        self.state.ollama_keep_alive = value.strip() or "1h"

    def on_ollama_timeout_changed(self, value: int) -> None:
        self.state.ollama_request_timeout_seconds = int(value)

    def on_ollama_command_timeout_changed(self, value: int) -> None:
        self.state.ollama_command_timeout_seconds = int(value)

    def on_ollama_verify_tls_changed(self, checked: bool) -> None:
        self.state.ollama_verify_tls = checked

    def on_ollama_ca_bundle_changed(self, value: str) -> None:
        self.state.ollama_ca_bundle_path = value.strip()

    def on_ollama_context_changed(self, value: int) -> None:
        self.state.ollama_context_length = int(value)

    def _ollama_headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        token = self.ollama_token_input.text().strip()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _ollama_ssl_context(self, base_url: str) -> ssl.SSLContext | None:
        if not base_url.lower().startswith("https://"):
            return None
        context = ssl.create_default_context(cafile=self.state.ollama_ca_bundle_path or None)
        if not self.state.ollama_verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        return context

    def test_ollama_connection(self) -> None:
        try:
            base_url = self.ollama_api_base_url()
            request = urllib.request.Request(f"{base_url}/tags", headers=self._ollama_headers())
            with urllib.request.urlopen(request, timeout=10, context=self._ollama_ssl_context(base_url)) as response:
                data = json.loads(response.read().decode("utf-8"))
            models = self._set_ollama_models(data)
            count = len(models)
            self.ollama_status_label.setText(f"Connected to {base_url}; {count} installed model(s) found.")
            self.set_status("Ollama connection succeeded.")
        except urllib.error.HTTPError as exc:
            self.ollama_status_label.setText(f"Connection failed: HTTP {exc.code} {exc.reason}")
        except urllib.error.URLError as exc:
            self.ollama_status_label.setText(f"Connection failed: {exc.reason}")
        except (ValueError, json.JSONDecodeError) as exc:
            self.ollama_status_label.setText(f"Connection failed: {exc}")

    def refresh_ollama_models(self) -> None:
        try:
            base_url = self.ollama_api_base_url()
            request = urllib.request.Request(f"{base_url}/tags", headers=self._ollama_headers())
            with urllib.request.urlopen(request, timeout=10, context=self._ollama_ssl_context(base_url)) as response:
                payload = json.loads(response.read().decode("utf-8"))
            models = self._set_ollama_models(payload)
            self.ollama_status_label.setText(f"Loaded {len(models)} model(s) from {base_url}.")
        except urllib.error.HTTPError as exc:
            self.ollama_status_label.setText(f"Could not load models: HTTP {exc.code} {exc.reason}")
        except urllib.error.URLError as exc:
            self.ollama_status_label.setText(f"Could not load models: {exc.reason}")
        except (ValueError, json.JSONDecodeError) as exc:
            self.ollama_status_label.setText(f"Could not load models: {exc}")

    def _set_ollama_models(self, payload: Any) -> list[str]:
        if not isinstance(payload, dict):
            return []
        models = [
            str(item.get("name"))
            for item in payload.get("models", [])
            if isinstance(item, dict) and item.get("name")
        ]
        current = self.state.model
        self.ollama_models_combo.blockSignals(True)
        self.ollama_models_combo.clear()
        self.ollama_models_combo.addItems(models)
        self.ollama_models_combo.setCurrentText(current or (models[0] if models else ""))
        self.ollama_models_combo.blockSignals(False)
        if not current and models:
            self.state.model = models[0]
        return models

    def execute_ollama_tool(self, name: str, arguments: dict[str, Any]) -> str:
        workspace = self.current_workspace().resolve()
        relative = str(arguments.get("path") or ".")
        try:
            target = (workspace / relative).resolve()
            target.relative_to(workspace)
        except (OSError, ValueError):
            return "Error: path is outside the selected workspace."
        try:
            if name == "list_workspace":
                if not target.is_dir():
                    return "Error: path is not a directory."
                entries = [item.name + ("/" if item.is_dir() else "") for item in target.iterdir()]
                return "\n".join(sorted(entries)[:500]) or "(empty directory)"
            if name == "read_workspace_file":
                if not target.is_file():
                    return "Error: file does not exist."
                return target.read_text(encoding="utf-8", errors="replace")[:MAX_COMMAND_OUTPUT_SAVE_CHARS]
            if name == "write_workspace_file":
                if self.state.sandbox == "read-only":
                    return "Error: workspace is read-only."
                if target.exists() and target.is_dir():
                    return "Error: path names a directory. Use a relative file path such as artifacts/IMPLEMENTATION_NOTES.md."
                content = arguments.get("content")
                if not isinstance(content, str):
                    return "Error: content must be a string."
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                return f"Wrote {target.relative_to(workspace)}."
            if name == "apply_workspace_patch":
                if self.state.sandbox == "read-only":
                    return "Error: workspace is read-only."
                if not target.is_file():
                    return "Error: file does not exist."
                old_text = arguments.get("old_text")
                new_text = arguments.get("new_text")
                if not isinstance(old_text, str) or not isinstance(new_text, str) or not old_text:
                    return "Error: old_text and new_text must be non-empty strings."
                content = target.read_text(encoding="utf-8", errors="replace")
                occurrences = content.count(old_text)
                if occurrences != 1:
                    return f"Error: old_text matched {occurrences} times; reread the file and provide one exact unique match."
                target.write_text(content.replace(old_text, new_text, 1), encoding="utf-8")
                return f"Patched {target.relative_to(workspace)}."
            if name == "run_workspace_command":
                if not self.state.bypass_approvals_and_sandbox:
                    return "Error: command execution requires Normal local shell mode."
                command = arguments.get("command")
                if not isinstance(command, str) or not command.strip():
                    return "Error: command must be a non-empty string."
                if os.name == "nt":
                    process = subprocess.Popen(
                        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                        cwd=workspace, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                        text=True, encoding="utf-8", errors="replace",
                    )
                else:
                    process = subprocess.Popen(command, cwd=workspace, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
                with self._ollama_tool_process_lock:
                    self._ollama_tool_process = process
                try:
                    stdout, stderr = process.communicate(timeout=self.state.ollama_command_timeout_seconds)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
                    output = (stdout + stderr)[:MAX_COMMAND_OUTPUT_SAVE_CHARS]
                    return f"exit=124\nError: command timed out after {self.state.ollama_command_timeout_seconds} seconds.\n{output}"
                finally:
                    with self._ollama_tool_process_lock:
                        if self._ollama_tool_process is process:
                            self._ollama_tool_process = None
                output = (stdout + stderr)[:MAX_COMMAND_OUTPUT_SAVE_CHARS]
                return f"exit={process.returncode}\n{output}"
            return f"Error: unsupported tool {name!r}."
        except OSError as exc:
            return f"Error: {exc}"

    def stop_ollama_tool_process(self) -> None:
        with self._ollama_tool_process_lock:
            process = self._ollama_tool_process
        if process and process.poll() is None:
            process.kill()

    def on_model_changed(self, value: str) -> None:
        self.state.model = value

    def on_provider_changed(self, value: str) -> None:
        self.state.provider = value if value in PROVIDERS else "Codex"
        self.current_output.setPlainText(self.current_step_text())

    def on_extra_args_changed(self, value: str) -> None:
        self.state.extra_args = value

    def on_sandbox_changed(self, value: str) -> None:
        self.state.sandbox = value

    def on_bypass_changed(self, checked: bool) -> None:
        self.state.bypass_approvals_and_sandbox = checked
        if checked:
            self.set_status("Normal local shell mode runs commands without Codex sandboxing.")

    def on_skip_git_changed(self, checked: bool) -> None:
        self.state.skip_git_check = checked

    def on_ephemeral_changed(self, checked: bool) -> None:
        self.state.ephemeral = checked

    def on_auto_advance_changed(self, checked: bool) -> None:
        self.state.auto_advance = checked

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


def main() -> None:
    status = dependency_preflight()
    if "--preflight" in sys.argv:
        print(status.message)
        raise SystemExit(0 if status.ok else 1)
    if not status.ok:
        print(status.message, file=sys.stderr)
        print("Install PyQt6 and ensure powershell_codex_viewer.py is importable before launching the workbench.", file=sys.stderr)
        raise SystemExit(1)

    app = QtWidgets.QApplication(sys.argv)
    window = MultiAgentCodexWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
