from __future__ import annotations

import importlib
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from PyQt6 import QtCore, QtGui, QtWidgets

CodexExecRunner: Any = None
CodexRunState: Any = None
_resolve_codex_command: Callable[[], str] | None = None


MAX_LOG_LINES = 600
MAX_RAW_LINES = 300
MAX_INLINE_ACTIVITY_OUTPUT_CHARS = 6000
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
    allowed_handoffs: list[str] | None = None
    model: str = ""
    model_reasoning_effort: str = ""


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


def default_role_library() -> dict[str, dict[str, str]]:
    roles: dict[str, dict[str, str]] = {
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
            roles[name] = {
                "artifact_name": str(item.get("artifact_name") or f"{name}.md"),
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

    if "Default Pipeline" not in presets:
        presets["Default Pipeline"] = default_presets()["Default Pipeline"]
    for name in presets["Default Pipeline"]:
        roles.setdefault(name, default_role_library()[name])

    return roles, presets


def save_library(roles: dict[str, dict[str, Any]], presets: dict[str, list[str]]) -> None:
    LIBRARY_FILE.write_text(
        json.dumps({"roles": roles, "presets": presets}, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def optional_string_list(data: dict[str, Any], key: str) -> list[str] | None:
    if key not in data:
        return None
    value = data.get(key)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value]


def role_library_entry(role: AgentRole) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "artifact_name": role.artifact_name,
        "prompt": role.prompt,
    }
    if role.model.strip():
        entry["model"] = role.model.strip()
    if role.model_reasoning_effort.strip():
        entry["model_reasoning_effort"] = role.model_reasoning_effort.strip()
    if role.allowed_handoffs is not None:
        entry["allowed_handoffs"] = list(role.allowed_handoffs)
    return entry


def role_to_dict(role: AgentRole) -> dict[str, Any]:
    data = {
        "name": role.name,
        "artifact_name": role.artifact_name,
        "prompt": role.prompt,
        "status": role.status,
        "session_id": role.session_id,
    }
    if role.model.strip():
        data["model"] = role.model.strip()
    if role.model_reasoning_effort.strip():
        data["model_reasoning_effort"] = role.model_reasoning_effort.strip()
    if role.allowed_handoffs is not None:
        data["allowed_handoffs"] = list(role.allowed_handoffs)
    return data


def role_from_dict(data: dict[str, Any]) -> AgentRole | None:
    name = str(data.get("name") or "").strip()
    if not name:
        return None
    return AgentRole(
        name=name,
        artifact_name=str(data.get("artifact_name") or f"{name}.md"),
        prompt=str(data.get("prompt") or ""),
        status=str(data.get("status") or "queued"),
        session_id=str(data.get("session_id") or ""),
        allowed_handoffs=optional_string_list(data, "allowed_handoffs"),
        model=str(data.get("model") or ""),
        model_reasoning_effort=str(data.get("model_reasoning_effort") or ""),
    )


def workflow_state_to_dict(state: WorkflowState) -> dict[str, Any]:
    return {
        "goal": state.goal,
        "codex_cmd": state.codex_cmd,
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
    try:
        return str(path.resolve().relative_to(workspace.resolve()))
    except (OSError, ValueError):
        return str(path)


def load_session_index(workspace: Path) -> list[Path]:
    try:
        loaded = json.loads(session_index_file(workspace).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
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
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = workspace / path
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
    for path in discovered:
        key = path_key(path)
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def save_session_index(workspace: Path, paths: list[Path]) -> None:
    index_file = session_index_file(workspace)
    index_file.parent.mkdir(parents=True, exist_ok=True)
    payload = {"sessions": [path_for_session_index(workspace, path) for path in paths]}
    index_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def session_slug(text: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "-" for ch in text.strip())
    slug = "-".join(part for part in slug.split("-") if part)
    return slug[:48] or "session"


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
    event_ready = QtCore.pyqtSignal(dict)
    log_ready = QtCore.pyqtSignal(str, str)
    done_ready = QtCore.pyqtSignal(int, str)


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
            )
            for role in ROLE_PROMPTS
        ]
        self.runner: Any = None
        self.active_role: AgentRole | None = None
        self.run_started_at = 0.0
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
        self.signals.log_ready.connect(self.append_event)
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
        try:
            normalized = path.resolve()
        except OSError:
            normalized = path
        existing = [item for item in self.saved_session_paths if str(item).lower() != str(normalized).lower()]
        self.saved_session_paths = [normalized, *existing]
        try:
            save_session_index(self.current_workspace(), self.saved_session_paths)
        except OSError as exc:
            self.append_event(f"Could not update session index: {exc}", "warning")
        self.refresh_session_list()

    def refresh_session_list(self) -> None:
        indexed_paths = load_session_index(self.current_workspace())
        if self.saved_session_paths:
            indexed_keys = {path_key(path) for path in indexed_paths}
            indexed_paths.extend(path for path in self.saved_session_paths if path_key(path) not in indexed_keys)
        existing_paths: list[Path] = []
        for path in indexed_paths:
            if path.exists():
                existing_paths.append(path)
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
        path = Path(str(path_text))
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
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.session_payload(), indent=2, ensure_ascii=False), encoding="utf-8")
        except OSError as exc:
            if announce:
                QtWidgets.QMessageBox.critical(self, "Save Session", f"Could not save session:\n{exc}")
            self.set_status(f"Could not save session: {exc}")
            return
        self.session_file = path
        self.register_session_path(path)
        if announce:
            self.set_status(f"Saved session: {path}")
            self.append_event(f"Session saved: {path}")

    def auto_save_session(self) -> None:
        if self.session_file is not None:
            self.write_session_file(self.session_file, announce=False)

    def load_session(self) -> None:
        if self.runner and self.runner.process and self.runner.process.poll() is None:
            self.set_status("Stop the running agent before loading a session.")
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
        if self.runner and self.runner.process and self.runner.process.poll() is None:
            self.set_status("Stop the running agent before loading a session.")
            return
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
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
        self.session_file = path
        self.register_session_path(path)

        self.sync_controls_from_state()
        self._rebuild_role_status_buttons()
        self._sync_role_editor()
        self.set_phase(str(loaded.get("phase") or "Idle"), None, str(loaded.get("phase_detail") or "Session loaded."))
        self.refresh_all_outputs()
        self.refresh_artifacts()
        self.set_status(f"Loaded session: {path}")

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

        self.codex_cmd_input.blockSignals(True)
        self.codex_cmd_input.setText(self.state.codex_cmd)
        self.codex_cmd_input.blockSignals(False)

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

    # ----------------------------------------------------------- workflow

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
        self.auto_advance_checkbox.setChecked(False)
        self.set_status("Auto-advance paused.")

    def start_current_role(self) -> None:
        role = self.current_role()
        cwd = self.current_workspace()
        if not cwd.exists() or not cwd.is_dir():
            self.set_status(f"Workspace folder does not exist: {cwd}")
            return

        if self.runner and self.runner.process and self.runner.process.poll() is None:
            self.set_status("An agent is already running. Stop it before starting another.")
            self.set_phase("Waiting", role.name, "Another Codex process is still shutting down.")
            return

        self.ensure_session_file()
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
        self.set_status(f"{role.name} is running...")
        self.refresh_all_outputs()

        prompt = self.build_agent_prompt(role, self.pending_relay_message)
        self.pending_relay_message = ""
        run_state = CodexRunState(
            prompt=prompt,
            codex_cmd=self.state.codex_cmd,
            cwd=str(cwd),
            resume_session_id=role.session_id if not self.state.ephemeral else "",
            model=role.model.strip() or self.state.model,
            model_reasoning_effort=role.model_reasoning_effort.strip(),
            sandbox=self.state.sandbox,
            extra_args=self.state.extra_args,
            bypass_approvals_and_sandbox=self.state.bypass_approvals_and_sandbox,
            skip_git_check=self.state.skip_git_check,
            ephemeral=self.state.ephemeral,
        )
        self.runner = CodexExecRunner(
            run_state,
            on_event=lambda event: self.signals.event_ready.emit(event),
            on_log=lambda summary: self.signals.log_ready.emit(summary.text, summary.level),
            on_done=lambda code, stderr: self.signals.done_ready.emit(code, stderr),
        )
        self.runner.start()

    def stop_run(self) -> None:
        if not self.runner:
            self.set_status("No agent process is running.")
            return
        self.runner.stop()
        self.set_status("Stopping agent...")
        self.set_phase("Stopping", None, "Stopping active Codex process.")

    def stop_all_agents(self) -> None:
        if self.runner:
            self.runner.stop()

    @QtCore.pyqtSlot(int, str)
    def finish_run(self, returncode: int, stderr: str) -> None:
        role = self.active_role or self.current_role()
        elapsed = time.time() - self.run_started_at if self.run_started_at else 0.0
        stderr = stderr.strip()
        self.refresh_artifacts()
        self.clear_finished_runner()

        if returncode == 0:
            role.status = "complete"
            self.append_activity(f"[{role.name.lower()} completed] exit=0 in {elapsed:.1f}s")
            self.set_status(f"{role.name} completed in {elapsed:.1f}s.")
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
            first_line = stderr.splitlines()[0] if stderr else "no stderr output"
            self.append_event(f"{role.name} exited with code {returncode}: {first_line}", "error")
            self.set_status(f"{role.name} exited with code {returncode} after {elapsed:.1f}s.")
            self.set_phase("Blocked", role.name, first_line)

        self.refresh_all_outputs()
        self.auto_save_session()

    @QtCore.pyqtSlot(dict)
    def handle_event(self, event: dict[str, Any]) -> None:
        role_name = self.active_role.name if self.active_role else self.current_role().name
        event_type = str(event.get("type", "unknown"))
        self.raw_lines.append(json.dumps({"role": role_name, **event}, ensure_ascii=False))
        self.raw_lines = self.raw_lines[-MAX_RAW_LINES:]
        self.raw_output.setPlainText(self.log_text(self.raw_lines))

        if event_type == "thread.started":
            thread_id = str(event.get("thread_id") or event.get("id") or "")
            role = self.role_by_name(role_name)
            had_session = bool(role and role.session_id)
            if role and thread_id:
                role.session_id = thread_id
                self.auto_save_session()
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
                self.set_status(
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
            f"Expected artifact: artifacts/{role.artifact_name}\n\n"
            f"{handoff_rules}\n\n"
            f"Existing artifacts:\n{artifacts if artifacts else '(none yet)'}\n"
        )

    def allowed_handoff_targets(self, role: AgentRole) -> list[AgentRole]:
        targets = [target for target in self.roles if target.name != role.name]
        if role.allowed_handoffs is None:
            return targets
        allowed = set(role.allowed_handoffs)
        return [target for target in targets if target.name in allowed]

    def handoff_instructions(self, role: AgentRole) -> str:
        targets = self.allowed_handoff_targets(role)
        markers = ", ".join(f"{target.name}: {handoff_marker(target.name)}" for target in targets)
        return (
            "Agent handoff protocol:\n"
            f"- To pass work to another agent, write that agent's marker, then the direct message body, then {HANDOFF_END}, then {HANDOFF_DONE}.\n"
            f"- Available target markers: {markers if markers else '(none enabled for this role)'}.\n"
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
        self._sync_role_editor()
        self.pending_relay_message = handoff.body
        self.set_status(f"Relaying {handoff.source_role} message to {handoff.target_role}...")
        self.set_phase("Relaying", handoff.target_role, f"Message from {handoff.source_role}.")
        self.append_event(f"Relay queued: {handoff.source_role} -> {handoff.target_role}")
        self.launch_queued_relay()

    def launch_queued_relay(self, attempt: int = 0) -> None:
        self.clear_finished_runner()
        if self.runner and self.runner.process and self.runner.process.poll() is None:
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

    def clear_finished_runner(self) -> None:
        if self.runner and self.runner.process and self.runner.process.poll() is not None:
            self.runner = None

    def relay_pending_handoff(self) -> None:
        if not self.pending_handoff:
            self.set_status("No pending handoff to relay.")
            return

        if self.runner and self.runner.process and self.runner.process.poll() is None:
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
        candidates: list[Handoff] = []
        source = self.role_by_name(source_role)
        targets = self.allowed_handoff_targets(source) if source else [target for target in self.roles if target.name != source_role]
        for target in targets:
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
            path = artifact_dir / role.artifact_name
            artifact_ref = f"artifacts/{role.artifact_name}"
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
            path.write_text(output, encoding="utf-8", errors="replace")
        except OSError as exc:
            self.append_activity(f"[{role_name.lower()} output save failed] {exc}")
            self.append_activity(output)
            return
        relative = path.relative_to(self.current_workspace())
        self.append_activity(f"[{role_name.lower()} output saved] {relative} ({len(output):,} chars)")

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
        lines.append("")
        lines.append("Current role")
        lines.append(role.name)
        lines.append("")
        lines.append("Model")
        lines.append(effective_model)
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
        self.current_role().artifact_name = value
        self.refresh_all_outputs()

    def on_role_model_changed(self, value: str) -> None:
        self.current_role().model = value.strip()
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
        if self.runner and self.runner.process and self.runner.process.poll() is None:
            self.set_status("Stop the running agent before switching presets.")
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
        if self.runner and self.runner.process and self.runner.process.poll() is None:
            self.set_status("Stop the running agent before starting a new workflow.")
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
