from __future__ import annotations

import atexit
import importlib
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

dg: Any = None
CodexExecRunner: Any = None
CodexRunState: Any = None
_resolve_codex_command: Callable[[], str] | None = None


MAX_LOG_LINES = 600
MAX_RAW_LINES = 300
HANDOFF_MIN_LENGTH = 20
HANDOFF_MAX_LENGTH = 6000
HANDOFF_END = "[[END_HANDOFF]]"
HANDOFF_DONE = "[[COMMANDDOCK_DONE]]"
APPROVED = "[[APPROVED]]"


@dataclass
class DependencyStatus:
    ok: bool
    message: str
    dragongui_version: str = "unknown"


@dataclass
class HandoffParseResult:
    handoff: "Handoff | None"
    rejection_reason: str = ""


@dataclass(frozen=True)
class ArtifactSpec:
    name: str
    label: str


def load_runtime_dependencies() -> DependencyStatus:
    global dg, CodexExecRunner, CodexRunState, _resolve_codex_command
    if dg is not None and CodexExecRunner is not None and CodexRunState is not None:
        version = str(getattr(dg, "__version__", "unknown"))
        return DependencyStatus(True, f"DragonGUI available ({version}).", version)

    try:
        dg = importlib.import_module("dragongui")
        viewer = importlib.import_module("powershell_codex_viewer")
    except ModuleNotFoundError as exc:
        return DependencyStatus(False, f"Missing runtime dependency: {exc}")
    except Exception as exc:
        return DependencyStatus(False, f"Could not load runtime dependencies: {exc}")

    CodexExecRunner = viewer.CodexExecRunner
    CodexRunState = viewer.CodexRunState
    _resolve_codex_command = viewer.resolve_codex_command
    version = str(getattr(dg, "__version__", "unknown"))
    return DependencyStatus(True, f"DragonGUI available ({version}).", version)


def dependency_preflight() -> DependencyStatus:
    return load_runtime_dependencies()


def resolve_codex_command() -> str:
    status = load_runtime_dependencies()
    if status.ok and _resolve_codex_command is not None:
        return _resolve_codex_command()
    return "codex"


def handoff_marker(role_name: str) -> str:
    normalized = role_name.strip().upper().replace(" ", "_")
    return f"[[HANDOFF_TO_{normalized}]]"


def extract_latest_valid_handoff(buffer: str, marker: str) -> str:
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

    for _start, body, between_end_and_done in reversed(candidates):
        if len(between_end_and_done) > 80:
            continue
        return body
    return ""


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


@dataclass
class WorkflowState:
    goal: str = (
        "Build a compact multi-agent Codex workflow that plans, reviews, implements, "
        "tests, and archives through markdown handoffs."
    )
    codex_cmd: str = field(default_factory=resolve_codex_command)
    cwd: str = field(default_factory=lambda: str(Path.cwd()))
    model: str = ""
    sandbox: str = "workspace-write"
    extra_args: str = ""
    bypass_approvals_and_sandbox: bool = True
    skip_git_check: bool = True
    ephemeral: bool = False
    auto_advance: bool = True
    current_role_index: int = 0


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


class MultiAgentCodexGui:
    def __init__(self) -> None:
        self.dependency_status = load_runtime_dependencies()
        if not self.dependency_status.ok:
            raise RuntimeError(self.dependency_status.message)

        self.app = dg.App(theme=dg.Theme.dark())
        self.state = WorkflowState()
        self.roles = [
            AgentRole(role.name, role.artifact_name, role.prompt, role.status, role.session_id)
            for role in ROLE_PROMPTS
        ]
        self.runner: CodexExecRunner | None = None
        self.runner_lock = threading.Lock()
        self.active_role: AgentRole | None = None
        self.run_started_at = 0.0

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
        self.artifact_outputs: dict[str, Any] = {}

        atexit.register(self.stop_all_agents)

        self.goal_input: dg.TextArea
        self.conversation_output: dg.TextArea
        self.current_output: dg.TextArea
        self.activity_output: dg.TextArea
        self.role_prompt_input: dg.TextArea
        self.plan_output: dg.TextArea
        self.review_output: dg.TextArea
        self.implementation_output: dg.TextArea
        self.test_output: dg.TextArea
        self.summary_output: dg.TextArea
        self.handoff_output: dg.TextArea
        self.pipeline_output: dg.TextArea
        self.artifact_status_output: dg.TextArea
        self.events_output: dg.TextArea
        self.raw_output: dg.TextArea
        self.status: dg.Label
        self.phase_label: dg.Label
        self.agent_label: dg.Label
        self.detail_label: dg.Label
        self.workspace_input: dg.TextInput
        self.codex_cmd_input: dg.TextInput
        self.model_input: dg.TextInput
        self.extra_args_input: dg.TextInput
        self.sandbox_dropdown: dg.Dropdown
        self.bypass_checkbox: dg.Checkbox
        self.skip_git_checkbox: dg.Checkbox
        self.ephemeral_checkbox: dg.Checkbox
        self.auto_advance_checkbox: dg.Checkbox

    def run(self) -> None:
        self.app.stylesheet(
            """
            Window { gap: 0; }
            Panel { padding: 12px; gap: 10px; border-radius: 8px; }
            Button.primary { background: accent; color: background; }
            TextArea.mono { font-family: Consolas, monospace; font-size: 13px; }
            TextArea.prompt { font-family: Consolas, monospace; font-size: 13px; }
            Label.status { color: muted; }
            Label.section { font-weight: 700; }
            """
        )

        win = dg.Window("Multi-Agent Codex Workbench", width=1360, height=860)

        with dg.VLayout(style={"height": "100%", "gap": 8, "padding": 10}):
            with dg.MenuBar():
                with dg.Menu("Workflow"):
                    dg.MenuItem("New Workflow")
                    dg.MenuItem("Open Artifacts Folder")
                    dg.MenuItem("Export Transcript")
                with dg.Menu("Agents"):
                    dg.MenuItem("Edit Role Profiles")
                    dg.MenuItem("Duplicate Workflow")
                with dg.Menu("Debug"):
                    dg.MenuItem("Show JSONL")
                    dg.MenuItem("Validate Handoffs")

            with dg.HLayout(style={"flex_grow": 1, "min_height": 0, "gap": 10}):
                with dg.Sidebar(title="Workbench", width=310):
                    dg.Label("Agent Pipeline", class_="section", wrap=False)
                    self.pipeline_output = dg.TextArea(
                        value=self.pipeline_text(),
                        rows=11,
                        wrap=False,
                        class_="mono",
                        style={"height": 210},
                    )
                    dg.Label("Handoff", class_="section", wrap=False, style={"margin_top": 12})
                    self.handoff_output = dg.TextArea(
                        value=self.handoff_status_text(),
                        rows=7,
                        wrap=True,
                        class_="mono",
                        style={"height": 140},
                    )
                    dg.Label("Artifacts", class_="section", wrap=False, style={"margin_top": 12})
                    self.artifact_status_output = dg.TextArea(
                        value="",
                        rows=8,
                        wrap=False,
                        class_="mono",
                        style={"height": 170},
                    )

                with dg.ScrollArea(axis="y", style={"flex_grow": 1, "min_width": 0, "min_height": 0}):
                    with dg.VLayout(style={"gap": 8, "min_width": 0}):
                        with dg.Panel("Command Center", style={"flex_shrink": 0}):
                            self.goal_input = dg.TextArea(
                                value=self.state.goal,
                                rows=3,
                                wrap=True,
                                class_="prompt",
                                on_change=self.on_goal_changed,
                            )
                            with dg.HLayout(style={"gap": 8}):
                                dg.Label("Workspace", wrap=False, style={"width": 90})
                                self.workspace_input = dg.TextInput(
                                    value=self.state.cwd,
                                    on_change=self.on_cwd_changed,
                                    style={"flex_grow": 1},
                                )
                                dg.Button("Use App Folder", on_click=self.use_app_folder)
                            with dg.HLayout(style={"gap": 8, "align_items": "center"}):
                                dg.Label("Preflight", wrap=False, style={"width": 90})
                                dg.Label(self.dependency_status.message, class_="status", wrap=True, style={"flex_grow": 1})
                            with dg.HLayout(style={"gap": 8, "align_items": "center"}):
                                self.phase_label = dg.Label("Idle", wrap=False, style={"width": 120})
                                self.agent_label = dg.Label("Agent: none", wrap=False, style={"width": 160})
                                self.detail_label = dg.Label(
                                    "No agent is running.",
                                    class_="status",
                                    wrap=True,
                                    style={"flex_grow": 1},
                                )
                            with dg.Toolbar(gap=8, compact=True):
                                dg.Button("Start", on_click=self.start_workflow, class_="primary")
                                dg.Button("Pause", on_click=self.pause_workflow)
                                dg.Button("Step Once", on_click=self.start_current_role)
                                dg.Button("Stop", on_click=self.stop_run)
                                dg.ToolbarSeparator()
                                dg.Button("Relay Pending", on_click=self.relay_pending_handoff)
                                dg.Button("Refresh Artifacts", on_click=self.refresh_artifacts)
                                dg.Button("Clear", on_click=self.clear_outputs)

                        with dg.HLayout(style={"flex_grow": 1, "min_height": 0, "gap": 10}):
                            with dg.Panel("Conversation", style={"flex_grow": 3, "flex_basis": 0, "min_height": 0}):
                                self.conversation_output = dg.TextArea(
                                    value="",
                                    rows=22,
                                    wrap=True,
                                    class_="mono",
                                    style={"height": "100%", "min_height": 0},
                                )

                            with dg.Tabs(value="current", style={"flex_grow": 2, "flex_basis": 0, "min_height": 0}):
                                with dg.Tab("Current Step", value="current"):
                                    with dg.Panel("Current Agent", style={"min_height": 0}):
                                        self.current_output = dg.TextArea(
                                            value=self.current_step_text(),
                                            rows=18,
                                            wrap=True,
                                            class_="mono",
                                        )

                                with dg.Tab("Artifacts", value="artifacts"):
                                    with dg.Panel("Markdown Handoffs", style={"min_height": 0}):
                                        with dg.Tabs(value="plan"):
                                            with dg.Tab("PLAN.md", value="plan"):
                                                self.plan_output = dg.TextArea(value="", rows=18, wrap=True, class_="mono")
                                                self.artifact_outputs["PLAN.md"] = self.plan_output
                                            with dg.Tab("REVIEW.md", value="review"):
                                                self.review_output = dg.TextArea(value="", rows=18, wrap=True, class_="mono")
                                                self.artifact_outputs["REVIEW.md"] = self.review_output
                                            with dg.Tab("IMPLEMENTATION", value="impl"):
                                                self.implementation_output = dg.TextArea(value="", rows=18, wrap=True, class_="mono")
                                                self.artifact_outputs["IMPLEMENTATION_NOTES.md"] = self.implementation_output
                                            with dg.Tab("TESTS", value="tests"):
                                                self.test_output = dg.TextArea(value="", rows=18, wrap=True, class_="mono")
                                                self.artifact_outputs["TEST_RESULTS.md"] = self.test_output
                                            with dg.Tab("SUMMARY.md", value="summary"):
                                                self.summary_output = dg.TextArea(value="", rows=18, wrap=True, class_="mono")
                                                self.artifact_outputs["SUMMARY.md"] = self.summary_output

                                with dg.Tab("Activity", value="activity"):
                                    with dg.Panel("Agent Activity", style={"min_height": 0}):
                                        self.activity_output = dg.TextArea(value="", rows=18, wrap=False, class_="mono")

                                with dg.Tab("Role Prompt", value="prompt"):
                                    with dg.Panel("Current Role Prompt", style={"min_height": 0}):
                                        self.role_prompt_input = dg.TextArea(
                                            value=self.current_role().prompt,
                                            rows=10,
                                            wrap=True,
                                            class_="prompt",
                                            on_change=self.on_role_prompt_changed,
                                        )
                                        with dg.HLayout(style={"gap": 8}):
                                            dg.Label("Model", wrap=False, style={"width": 90})
                                            self.model_input = dg.TextInput(
                                                value="",
                                                placeholder="Codex config default",
                                                on_change=self.on_model_changed,
                                                style={"flex_grow": 1},
                                            )
                                        with dg.HLayout(style={"gap": 8}):
                                            dg.Label("Codex", wrap=False, style={"width": 90})
                                            self.codex_cmd_input = dg.TextInput(
                                                value=self.state.codex_cmd,
                                                on_change=self.on_codex_cmd_changed,
                                                style={"flex_grow": 1},
                                            )
                                            dg.Button("Detect", on_click=self.detect_codex_command)
                                        with dg.HLayout(style={"gap": 8}):
                                            dg.Label("Sandbox", wrap=False, style={"width": 90})
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
                                        with dg.HLayout(style={"gap": 8}):
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
                                            self.auto_advance_checkbox = dg.Checkbox(
                                                "Auto-relay handoffs",
                                                checked=self.state.auto_advance,
                                                on_change=self.on_auto_advance_changed,
                                            )
                                        with dg.HLayout(style={"gap": 8}):
                                            dg.Label("Extra args", wrap=False, style={"width": 90})
                                            self.extra_args_input = dg.TextInput(
                                                value="",
                                                placeholder="--profile name --config key=value",
                                                on_change=self.on_extra_args_changed,
                                                style={"flex_grow": 1},
                                            )

                                with dg.Tab("Debug", value="debug"):
                                    with dg.Panel("Debug", style={"min_height": 0}):
                                        with dg.Tabs(value="events"):
                                            with dg.Tab("Events", value="events"):
                                                self.events_output = dg.TextArea(value="", rows=18, wrap=False, class_="mono")
                                            with dg.Tab("Raw JSONL", value="raw"):
                                                self.raw_output = dg.TextArea(value="", rows=18, wrap=False, class_="mono")

            with dg.StatusBar():
                self.status = dg.Label("Ready.", class_="status", wrap=False)
                dg.SmallButton("Planner", on_click=lambda: self.select_role(0))
                dg.SmallButton("Reviewer", on_click=lambda: self.select_role(1))
                dg.SmallButton("Implementer", on_click=lambda: self.select_role(2))
                dg.SmallButton("Tester", on_click=lambda: self.select_role(3))
                dg.SmallButton("Archivist", on_click=lambda: self.select_role(4))

        self.refresh_all_outputs()
        self.app.run(win)

    def gui(self, callback: Callable[[], None]) -> None:
        self.app.call_soon_threadsafe(callback)

    def current_role(self) -> AgentRole:
        return self.roles[self.state.current_role_index]

    def artifacts_dir(self) -> Path:
        return Path(self.state.cwd.strip() or Path.cwd()) / "artifacts"

    def start_workflow(self) -> None:
        self.clear_outputs()
        for role in self.roles:
            role.status = "queued"
        self.state.current_role_index = 0
        self.pending_relay_message = ""
        self.pending_handoff = None
        self.start_current_role()

    def pause_workflow(self) -> None:
        self.state.auto_advance = False
        self.status.set_value("Auto-advance paused.")

    def start_current_role(self) -> None:
        role = self.current_role()
        cwd = Path(self.state.cwd.strip() or Path.cwd())
        if not cwd.exists() or not cwd.is_dir():
            self.status.set_value(f"Workspace folder does not exist: {cwd}")
            return

        with self.runner_lock:
            if self.runner and self.runner.process and self.runner.process.poll() is None:
                self.status.set_value("An agent is already running. Stop it before starting another.")
                self.set_phase("Waiting", role.name, "Another Codex process is still shutting down.")
                return

            self.artifacts_dir().mkdir(exist_ok=True)
            role.status = "running"
            self.active_role = role
            self.run_started_at = time.time()
            self.set_phase("Starting", role.name, "Launching Codex process.")
            if self.pending_relay_message:
                self.append_conversation("Relay", self.pending_relay_message)
            else:
                self.append_conversation("User", self.state.goal)
            self.append_conversation(role.name, "Starting Codex run.")
            self.append_activity(f"[{role.name.lower()} running] {role.artifact_name}")
            self.status.set_value(f"{role.name} is running...")
            self.refresh_all_outputs()

            prompt = self.build_agent_prompt(role, self.pending_relay_message)
            self.pending_relay_message = ""
            run_state = CodexRunState(
                prompt=prompt,
                codex_cmd=self.state.codex_cmd,
                cwd=str(cwd),
                resume_session_id=role.session_id if not self.state.ephemeral else "",
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
                self.status.set_value("No agent process is running.")
                return
            self.runner.stop()
        self.status.set_value("Stopping agent...")
        self.set_phase("Stopping", None, "Stopping active Codex process.")

    def stop_all_agents(self) -> None:
        runner = self.runner
        if runner:
            runner.stop()

    def finish_run(self, returncode: int, stderr: str) -> None:
        role = self.active_role or self.current_role()
        elapsed = time.time() - self.run_started_at if self.run_started_at else 0.0
        stderr = stderr.strip()
        self.refresh_artifacts()
        self.clear_finished_runner()

        if returncode == 0:
            role.status = "complete"
            self.append_activity(f"[{role.name.lower()} completed] exit=0 in {elapsed:.1f}s")
            self.status.set_value(f"{role.name} completed in {elapsed:.1f}s.")
            self.set_phase("Complete", role.name, f"Finished in {elapsed:.1f}s.")

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
                    self.status.set_value(
                        f"Handoff ready: {handoff.source_role} -> {handoff.target_role}. Click Relay Pending."
                    )
            elif self.state.current_role_index < len(self.roles) - 1:
                self.state.current_role_index += 1
                self.current_role().status = "waiting"
                self.role_prompt_input.set_value(self.current_role().prompt)
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
            first_line = stderr.splitlines()[0] if stderr else "no stderr output"
            self.append_event(f"{role.name} exited with code {returncode}: {first_line}", "error")
            self.status.set_value(f"{role.name} exited with code {returncode} after {elapsed:.1f}s.")
            self.set_phase("Blocked", role.name, first_line)

        self.refresh_all_outputs()

    def handle_event(self, event: dict[str, Any]) -> None:
        role_name = self.active_role.name if self.active_role else self.current_role().name
        event_type = str(event.get("type", "unknown"))
        self.raw_lines.append(json.dumps({"role": role_name, **event}, ensure_ascii=False))
        self.raw_lines = self.raw_lines[-MAX_RAW_LINES:]
        self.raw_output.set_value(self.log_text(self.raw_lines))

        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or event.get("id") or "")
            role = self.role_by_name(role_name)
            had_session = bool(role and role.session_id)
            if role and thread_id:
                role.session_id = thread_id
            self.append_event(f"{role_name}: thread started {thread_id}".strip())
            mode = "resumed" if had_session else "started"
            self.set_phase("Connected", role_name, f"Codex thread {mode}.")
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
                self.status.set_value(
                    f"{role_name} completed; waiting for Codex process exit before relaying handoff."
                )
                self.set_phase("Finalizing", role_name, "Waiting for Codex process exit before relay.")
            else:
                self.set_phase("Finalizing", role_name, "Codex turn completed.")
            return
        if event_type in {"turn.failed", "thread.error", "error"}:
            self.append_event(f"{role_name}: {event_type}: {self.compact_json(event)}", "error")
            self.set_phase("Error", role_name, event_type)
            return
        if event_type.startswith("item."):
            self.handle_item_event(role_name, event_type, event)
            return
        self.append_event(f"{role_name}: {event_type}: {self.compact_json(event)}")

    def handle_item_event(self, role_name: str, event_type: str, event: dict[str, Any]) -> None:
        item = event.get("item") if isinstance(event.get("item"), dict) else event
        item_type = str(item.get("type") or item.get("item_type") or item.get("kind") or "item")

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
            status = item.get("status") or event_type.rsplit(".", 1)[-1]
            self.append_activity(f"[{role_name.lower()} {status} exit={exit_code}] {command}")
            self.set_phase("Working", role_name, str(command))
            output = item.get("aggregated_output") or item.get("output") or item.get("stdout")
            if output:
                self.append_activity(str(output).rstrip())
            self.append_event(f"{role_name}: command_execution")
            return

        if item_type == "file_change":
            changes = item.get("changes")
            if isinstance(changes, list) and changes:
                for change in changes:
                    if isinstance(change, dict):
                        kind = change.get("kind") or change.get("type") or "change"
                        path = change.get("path") or change.get("file") or ""
                        self.append_activity(f"[{role_name.lower()} file {kind}] {path}".rstrip())
            else:
                self.append_activity(f"[{role_name.lower()} file_change] {self.compact_json(item)}")
            self.append_event(f"{role_name}: file_change")
            self.set_phase("Editing", role_name, "File changes detected.")
            return

        if item_type in {"mcp_tool_call", "web_search", "todo_list"}:
            self.append_activity(f"[{role_name.lower()} {item_type}] {self.compact_json(item)}")
            self.append_event(f"{role_name}: {item_type}")
            self.set_phase("Using Tool", role_name, item_type)
            return

        self.append_event(f"{role_name}: {event_type}: {item_type}")

    def build_agent_prompt(self, role: AgentRole, relay_message: str = "") -> str:
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
            f"Expected artifact: artifacts/{role.artifact_name}\n\n"
            f"{handoff_rules}\n\n"
            f"Existing artifacts:\n{artifacts if artifacts else '(none yet)'}\n"
        )

    def handoff_instructions(self, role: AgentRole) -> str:
        markers = ", ".join(f"{target.name}: {handoff_marker(target.name)}" for target in self.roles if target.name != role.name)
        return (
            "Agent handoff protocol:\n"
            f"- To pass work to another agent, write that agent's marker, then the direct message body, then {HANDOFF_END}, then {HANDOFF_DONE}.\n"
            f"- Available target markers: {markers}.\n"
            f"- To end an approved review loop, write {APPROVED} then {HANDOFF_DONE}.\n"
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
        self.role_prompt_input.set_value(target.prompt)
        self.pending_relay_message = handoff.body
        self.status.set_value(f"Relaying {handoff.source_role} message to {handoff.target_role}...")
        self.set_phase("Relaying", handoff.target_role, f"Message from {handoff.source_role}.")
        self.append_event(f"Relay queued: {handoff.source_role} -> {handoff.target_role}")
        self.launch_queued_relay()

    def launch_queued_relay(self, attempt: int = 0) -> None:
        self.clear_finished_runner()
        if self.runner and self.runner.process and self.runner.process.poll() is None:
            if attempt >= 20:
                self.status.set_value("Relay is still waiting for the previous agent to exit.")
                self.set_phase("Relay Waiting", self.current_role().name, "Previous Codex process has not exited.")
                return
            delay = 0.25
            self.status.set_value(f"Relay queued; waiting for previous agent to exit ({attempt + 1}).")
            self.set_phase("Relay Queued", self.current_role().name, "Waiting for previous process shutdown.")
            threading.Timer(delay, lambda: self.gui(lambda: self.launch_queued_relay(attempt + 1))).start()
            return

        self.append_event(f"Launching relayed agent: {self.current_role().name}")
        self.start_current_role()

    def clear_finished_runner(self) -> None:
        with self.runner_lock:
            if self.runner and self.runner.process and self.runner.process.poll() is not None:
                self.runner = None

    def relay_pending_handoff(self) -> None:
        if not self.pending_handoff:
            self.status.set_value("No pending handoff to relay.")
            return

        if self.runner and self.runner.process and self.runner.process.poll() is None:
            self.status.set_value("Current agent is still running; pending handoff will relay when it exits.")
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
            self.status.set_value("Reviewer approved the workflow.")
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
        candidates: list[Handoff] = []
        for target in self.roles:
            if target.name == source_role:
                continue
            marker = handoff_marker(target.name)
            body = self.extract_latest_valid_handoff(buffer, marker)
            if body and self.validate_handoff_body(source_role, marker, body):
                candidates.append(Handoff(source_role, target.name, marker, body))
        return candidates[-1] if candidates else None

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

    def read_artifact_context(self) -> str:
        parts = []
        artifact_dir = self.artifacts_dir()
        for role in self.roles:
            path = artifact_dir / role.artifact_name
            if path.exists():
                try:
                    parts.append(f"--- artifacts/{role.artifact_name} ---\n{path.read_text(encoding='utf-8', errors='replace')}")
                except OSError as exc:
                    parts.append(f"--- artifacts/{role.artifact_name} ---\nCould not read artifact: {exc}")
        return "\n\n".join(parts)

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
                    output.set_value(text)
                status_lines.append(f"ok       artifacts/{artifact_name} ({len(text):,} chars)")
            else:
                if output:
                    output.set_value(f"artifacts/{artifact_name} has not been created yet.")
                status_lines.append(f"missing  artifacts/{artifact_name}")
        self.artifact_status_output.set_value("\n".join(status_lines))

    def refresh_all_outputs(self) -> None:
        self.current_output.set_value(self.current_step_text())
        self.pipeline_output.set_value(self.pipeline_text())
        self.handoff_output.set_value(self.handoff_status_text())
        self.conversation_output.set_value(self.conversation_text())
        self.activity_output.set_value(self.log_text(self.activity_lines))
        self.events_output.set_value(self.log_text(self.event_lines))
        self.raw_output.set_value(self.log_text(self.raw_lines))
        if time.monotonic() - self._last_artifact_refresh > 1.0:
            self.refresh_artifacts()

    def current_step_text(self) -> str:
        lines = []
        for index, role in enumerate(self.roles, start=1):
            marker = ">" if index - 1 == self.state.current_role_index else " "
            memory = "session" if role.session_id else "new"
            lines.append(f"{marker} {index}. {role.name:<12} {role.status:<9} {memory}")
        lines.append("")
        lines.append("Current role")
        lines.append(self.current_role().name)
        lines.append("")
        lines.append("Role instruction")
        lines.append(self.current_role().prompt)
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
        self.status.set_value("Cleared.")
        self.set_phase("Idle", None, "No agent is running.")
        self.refresh_all_outputs()

    def append_conversation(self, speaker: str, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.conversation_lines.append(f"{speaker}:\n{text}")
        self.conversation_lines = self.conversation_lines[-120:]
        self.conversation_output.set_value(self.conversation_text())

    def append_activity(self, text: str) -> None:
        self.activity_lines.append(text)
        self.activity_lines = self.activity_lines[-MAX_LOG_LINES:]
        self.activity_output.set_value(self.log_text(self.activity_lines))

    def append_event(self, text: str, level: str = "info") -> None:
        prefix = time.strftime("%H:%M:%S")
        if level != "info":
            prefix = f"{prefix} {level.upper()}"
        self.event_lines.append(f"{prefix}  {text}")
        self.event_lines = self.event_lines[-MAX_LOG_LINES:]
        self.events_output.set_value(self.log_text(self.event_lines))

    def set_phase(self, phase: str, agent_name: str | None, detail: str) -> None:
        self.phase = phase
        self.phase_detail = detail
        self.phase_label.set_value(phase)
        self.agent_label.set_value(f"Agent: {agent_name or 'none'}")
        self.detail_label.set_value(detail)
        if hasattr(self, "pipeline_output"):
            self.pipeline_output.set_value(self.pipeline_text())
        if hasattr(self, "handoff_output"):
            self.handoff_output.set_value(self.handoff_status_text())

    @staticmethod
    def log_text(lines: list[str]) -> str:
        return "\n".join(reversed(lines))

    def conversation_text(self) -> str:
        return "\n\n".join(reversed(self.conversation_lines))

    def select_role(self, index: int) -> None:
        self.state.current_role_index = index
        self.role_prompt_input.set_value(self.current_role().prompt)
        self.refresh_all_outputs()
        self.status.set_value(f"Selected {self.current_role().name}.")

    def on_goal_changed(self, value: str) -> None:
        self.state.goal = value

    def on_role_prompt_changed(self, value: str) -> None:
        self.current_role().prompt = value
        self.current_output.set_value(self.current_step_text())

    def on_codex_cmd_changed(self, value: str) -> None:
        self.state.codex_cmd = value

    def detect_codex_command(self) -> None:
        self.state.codex_cmd = resolve_codex_command()
        self.codex_cmd_input.set_value(self.state.codex_cmd)
        self.status.set_value(f"Using Codex command: {self.state.codex_cmd}")

    def on_cwd_changed(self, value: str) -> None:
        old_cwd = self.state.cwd
        self.state.cwd = value
        if value != old_cwd:
            self.clear_agent_sessions()
        self.refresh_artifacts()

    def use_app_folder(self) -> None:
        self.state.cwd = str(Path(__file__).resolve().parent)
        self.clear_agent_sessions()
        self.workspace_input.set_value(self.state.cwd)
        self.refresh_artifacts()
        self.status.set_value(f"Workspace set to {self.state.cwd}")

    def clear_agent_sessions(self) -> None:
        for role in self.roles:
            role.session_id = ""
        self.append_event("Workspace changed; cleared per-agent Codex session ids.")

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


if __name__ == "__main__":
    status = dependency_preflight()
    if "--preflight" in sys.argv:
        print(status.message)
        raise SystemExit(0 if status.ok else 1)
    if not status.ok:
        print(status.message, file=sys.stderr)
        print("Install DragonGUI in this Python environment before launching the workbench.", file=sys.stderr)
        raise SystemExit(1)
    MultiAgentCodexGui().run()
