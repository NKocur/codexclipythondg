from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

import pyqt_multi_agent_workbench as workbench

sys.modules.setdefault("dragongui", types.SimpleNamespace())
import powershell_codex_viewer as runner_mod


class DummyText:
    def __init__(self) -> None:
        self.text = ""

    def setPlainText(self, text: str) -> None:
        self.text = text


class DummyItem:
    def __init__(self, value: str) -> None:
        self.value = value

    def data(self, _role: object) -> str:
        return self.value


class DummySessionList:
    def __init__(self, item: DummyItem | None) -> None:
        self.item = item

    def currentItem(self) -> DummyItem | None:
        return self.item


def lightweight_window(tmp_path: Path) -> workbench.MultiAgentCodexWindow:
    window = workbench.MultiAgentCodexWindow.__new__(workbench.MultiAgentCodexWindow)
    window.state = workbench.WorkflowState(cwd=str(tmp_path))
    window.roles = [
        workbench.AgentRole("Planner", "PLAN.md", "plan", allowed_handoffs=["Tester", "Implementer"]),
        workbench.AgentRole("Implementer", "IMPLEMENTATION_NOTES.md", "impl"),
        workbench.AgentRole("Tester", "TEST_RESULTS.md", "test"),
    ]
    window.state.current_role_index = 0
    window.activity_lines = []
    window.event_lines = []
    window.raw_lines = []
    window.agent_buffers = {}
    window.seen_handoffs = set()
    window.pending_handoff = None
    window.pending_relay_message = ""
    window.final_text_by_role = {}
    window.active_run_id = None
    window.run_lifecycle = "idle"
    window.runner = None
    window.active_role = None
    window.run_started_at = 0.0
    window.last_run_error = ""
    window.phase = "Idle"
    window.phase_detail = ""
    window.session_file = None
    window.saved_session_paths = []
    window.activity_output = DummyText()
    window.events_output = DummyText()
    window.raw_output = DummyText()
    window.status_messages = []
    window.phase_calls = []
    window.refreshes = 0
    window.autosaves = 0
    window.append_activity = lambda text: window.activity_lines.append(text)
    window.append_event = lambda text, level="info": window.event_lines.append(f"{level}:{text}")
    window.set_status = lambda text: window.status_messages.append(text)
    window.set_phase = lambda phase, agent_name, detail: window.phase_calls.append((phase, agent_name, detail))
    window.refresh_all_outputs = lambda: setattr(window, "refreshes", window.refreshes + 1)
    window.refresh_artifacts = lambda: None
    window.auto_save_session = lambda: setattr(window, "autosaves", window.autosaves + 1)
    window.clear_finished_runner = lambda force=False: setattr(window, "runner", None)
    window.refresh_session_list = lambda: setattr(window, "session_list_refreshed", True)
    return window


def test_artifact_name_validation_accepts_only_single_relative_names() -> None:
    assert workbench.validate_artifact_name("TEST_RESULTS.md") == "TEST_RESULTS.md"

    unsafe_names = [
        "",
        "   ",
        "..\\x.md",
        "../x.md",
        "subdir/x.md",
        "subdir\\x.md",
        "/tmp/x.md",
        "C:\\tmp\\x.md",
        "\\\\server\\share\\x.md",
        ".",
        "..",
    ]
    for name in unsafe_names:
        with pytest.raises(ValueError):
            workbench.validate_artifact_name(name)

    fallback, warning = workbench.safe_artifact_name("..\\x.md", "Spec Writer")
    assert fallback == "spec-writer.md"
    assert warning and "Invalid artifact name" in warning


def test_session_index_drops_unsafe_entries_and_persists_relative_safe_paths(tmp_path: Path) -> None:
    session_dir = workbench.session_dir_for_workspace(tmp_path)
    session_dir.mkdir()
    safe = session_dir / f"safe{workbench.SESSION_FILE_SUFFIX}"
    safe.write_text("{}", encoding="utf-8")
    discovered = session_dir / f"discovered{workbench.SESSION_FILE_SUFFIX}"
    discovered.write_text("{}", encoding="utf-8")
    outside = tmp_path / "outside.codex-workbench.json"
    outside.write_text("{}", encoding="utf-8")
    wrong_suffix = session_dir / "bad.json"
    wrong_suffix.write_text("{}", encoding="utf-8")

    index = {
        "sessions": [
            str(safe),
            str(outside),
            "..\\outside.codex-workbench.json",
            str(wrong_suffix),
            str(workbench.session_index_file(tmp_path)),
        ]
    }
    workbench.session_index_file(tmp_path).write_text(json.dumps(index), encoding="utf-8")
    warnings: list[str] = []

    loaded = workbench.load_session_index(tmp_path, warn=warnings.append)

    assert safe.resolve() in loaded
    assert discovered.resolve() in loaded
    assert outside.resolve() not in loaded
    assert wrong_suffix.resolve() not in loaded
    assert any("Unsafe session index entry dropped" in warning for warning in warnings)

    workbench.save_session_index(tmp_path, [safe, outside, wrong_suffix])
    saved_index = json.loads(workbench.session_index_file(tmp_path).read_text(encoding="utf-8"))
    assert saved_index == {"sessions": [str(safe.resolve().relative_to(tmp_path.resolve()))]}


def test_delete_selected_session_rejects_unsafe_path_before_confirmation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    window = lightweight_window(tmp_path)
    outside = tmp_path / "outside.codex-workbench.json"
    outside.write_text("do not delete", encoding="utf-8")
    window.session_list = DummySessionList(DummyItem(str(outside)))

    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("confirmation dialog should not be opened for unsafe paths")

    monkeypatch.setattr(workbench.QtWidgets.QMessageBox, "question", fail_if_called)

    workbench.MultiAgentCodexWindow.delete_selected_session(window)

    assert outside.exists()
    assert any("Refusing to delete" in message for message in window.status_messages)
    assert any("Unsafe session delete rejected" in event for event in window.event_lines)


def test_handoff_selection_uses_latest_source_offset_not_role_order(tmp_path: Path) -> None:
    window = lightweight_window(tmp_path)
    older_tester = (
        "[[HANDOFF_TO_TESTER]]\n"
        "Older tester handoff body with enough text to pass validation.\n"
        "[[END_HANDOFF]]\n"
        "[[COMMANDDOCK_DONE]]"
    )
    later_implementer = (
        "[[HANDOFF_TO_IMPLEMENTER]]\n"
        "Later implementer handoff body with enough text to pass validation.\n"
        "[[END_HANDOFF]]\n"
        "[[COMMANDDOCK_DONE]]"
    )

    handoff = workbench.MultiAgentCodexWindow.extract_best_handoff(
        window,
        "Planner",
        older_tester + "\nnoise\n" + later_implementer,
    )

    assert handoff is not None
    assert handoff.target_role == "Implementer"
    assert handoff.body.startswith("Later implementer")


def test_command_output_retention_bounds_activity_saved_file_and_file_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    window = lightweight_window(tmp_path)
    output_dir = tmp_path / "artifacts" / ".workbench-command-output"
    output_dir.mkdir(parents=True)
    for index in range(workbench.MAX_COMMAND_OUTPUT_FILES + 3):
        path = output_dir / f"old-{index:02d}.txt"
        path.write_text("old", encoding="utf-8")
        old_time = time.time() - 1000 - index
        import os

        os.utime(path, (old_time, old_time))

    monkeypatch.setattr(workbench.time, "strftime", lambda _fmt: "20260713-010203")
    large_output = "x" * (workbench.MAX_COMMAND_OUTPUT_SAVE_CHARS + 1234)

    workbench.MultiAgentCodexWindow.append_command_output(window, "Tester", large_output)

    saved = output_dir / "20260713-010203-tester.txt"
    assert saved.exists()
    expected_saved, expected_truncated = workbench.bounded_command_output(large_output)
    assert expected_truncated
    assert saved.read_text(encoding="utf-8") == expected_saved
    assert len(list(output_dir.glob("*.txt"))) == workbench.MAX_COMMAND_OUTPUT_FILES
    assert window.activity_lines[0] == "x" * workbench.INLINE_ACTIVITY_OUTPUT_PREVIEW_CHARS
    assert len("\n".join(window.activity_lines)) < workbench.INLINE_ACTIVITY_OUTPUT_PREVIEW_CHARS + 500
    assert "output truncated" in window.activity_lines[-1]
    assert "keeping newest" in window.activity_lines[-1]


def test_command_output_write_failure_logs_only_bounded_preview(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    window = lightweight_window(tmp_path)
    monkeypatch.setattr(workbench.Path, "write_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    large_output = "s" * (workbench.MAX_COMMAND_OUTPUT_SAVE_CHARS + 100)

    workbench.MultiAgentCodexWindow.append_command_output(window, "Tester", large_output)

    joined = "\n".join(window.activity_lines)
    assert len(joined) < workbench.INLINE_ACTIVITY_OUTPUT_PREVIEW_CHARS + 250
    assert "output save failed" in joined
    assert "disk full" in joined


def test_bounded_command_output_keeps_head_and_tail() -> None:
    small = "abc" * 100
    assert workbench.bounded_command_output(small) == (small, False)

    head = "H" * (workbench.MAX_COMMAND_OUTPUT_SAVE_CHARS // 2)
    tail = "T" * (workbench.MAX_COMMAND_OUTPUT_SAVE_CHARS // 2)
    output = head + "M" * 500_000 + tail + "FINAL_ERROR_LINE"
    saved, truncated = workbench.bounded_command_output(output)
    assert truncated
    assert saved.startswith("H")
    assert saved.endswith("FINAL_ERROR_LINE")
    assert "chars omitted by workbench output cap" in saved
    marker_overhead = 200
    assert len(saved) <= workbench.MAX_COMMAND_OUTPUT_SAVE_CHARS + marker_overhead


def test_prune_command_output_files_enforces_total_byte_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    output_dir = tmp_path / ".workbench-command-output"
    output_dir.mkdir()
    monkeypatch.setattr(workbench, "MAX_COMMAND_OUTPUT_TOTAL_BYTES", 25)
    for index in range(5):
        path = output_dir / f"file-{index}.txt"
        path.write_text("0123456789", encoding="utf-8")
        stamp = time.time() - index
        os.utime(path, (stamp, stamp))

    workbench.MultiAgentCodexWindow.prune_command_output_files(output_dir)

    remaining = sorted(path.name for path in output_dir.glob("*.txt"))
    assert remaining == ["file-0.txt", "file-1.txt"]


def test_stale_run_callbacks_are_ignored(tmp_path: Path) -> None:
    window = lightweight_window(tmp_path)
    window.active_run_id = 7
    window.run_lifecycle = "running"
    window.active_role = window.roles[0]
    window.pending_handoff = workbench.Handoff("Planner", "Tester", workbench.handoff_marker("Tester"), "keep me")

    workbench.MultiAgentCodexWindow.handle_event(window, 6, {"type": "thread.started", "thread_id": "stale"})
    workbench.MultiAgentCodexWindow.handle_runner_log(window, 6, "stale log", "warning")
    workbench.MultiAgentCodexWindow.finish_run(window, 6, 0, "")

    assert window.active_run_id == 7
    assert window.run_lifecycle == "running"
    assert window.roles[0].status == "queued"
    assert window.raw_lines == []
    assert window.event_lines == []
    assert window.pending_handoff is not None


def test_atomic_write_json_replaces_valid_file_without_truncation_on_replace_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text('{"old": true}', encoding="utf-8")

    def fail_replace(_src: object, _dst: object) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(workbench.os, "replace", fail_replace)

    with pytest.raises(OSError):
        workbench.atomic_write_json(target, {"new": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}
    assert not list(tmp_path.glob(".state.json.*.tmp"))


class FakeStdin:
    def __init__(self, process: "FakeProcess") -> None:
        self.process = process

    def write(self, _text: str) -> None:
        raise RuntimeError("stdin failed")

    def close(self) -> None:
        pass


class FakeProcess:
    def __init__(self) -> None:
        self.pid = 12345
        self.returncode: int | None = None
        self.stdin = FakeStdin(self)
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


class StubbornFakeProcess(FakeProcess):
    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_calls += 1
        raise subprocess.TimeoutExpired("fake", timeout)


def test_runner_exception_after_popen_terminates_process_and_reports_done(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_process = FakeProcess()
    done: list[tuple[int, str]] = []
    logs: list[runner_mod.CodexEventSummary] = []
    events: list[dict[str, object]] = []

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *_args, **_kwargs: fake_process)

    runner = runner_mod.CodexExecRunner(
        runner_mod.CodexRunState(codex_cmd="codex", cwd=str(tmp_path), prompt="hello"),
        on_event=events.append,
        on_log=logs.append,
        on_done=lambda code, stderr: done.append((code, stderr)),
    )
    runner._build_command = lambda: ["codex", "exec", "--json", "-"]
    runner._attach_process_lifetime_job = lambda: None

    runner._run()

    assert fake_process.terminated
    assert fake_process.wait_calls >= 1
    assert done == [(-15, "stdin failed")]
    assert events == []


def test_runner_cleanup_failure_is_reported_and_nonzero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake_process = StubbornFakeProcess()
    done: list[tuple[int, str]] = []
    logs: list[runner_mod.CodexEventSummary] = []

    monkeypatch.setattr(runner_mod.subprocess, "Popen", lambda *_args, **_kwargs: fake_process)
    monkeypatch.setattr(runner_mod.sys, "platform", "win32")

    runner = runner_mod.CodexExecRunner(
        runner_mod.CodexRunState(codex_cmd="codex", cwd=str(tmp_path), prompt="hello"),
        on_event=lambda _event: None,
        on_log=logs.append,
        on_done=lambda code, stderr: done.append((code, stderr)),
    )
    runner._build_command = lambda: ["codex", "exec", "--json", "-"]
    runner._attach_process_lifetime_job = lambda: None

    runner._run()

    assert fake_process.terminated
    assert fake_process.killed
    assert done and done[0][0] == 1
    assert "stdin failed" in done[0][1]
    assert "Process cleanup warning" in done[0][1]
    assert "process may still be running" in done[0][1]
    assert any(log.level == "warning" and "Process cleanup warning" in log.text for log in logs)


def test_runner_stop_is_safe_before_and_after_process_assignment(tmp_path: Path) -> None:
    done: list[tuple[int, str]] = []
    runner = runner_mod.CodexExecRunner(
        runner_mod.CodexRunState(codex_cmd="codex", cwd=str(tmp_path), prompt="hello"),
        on_event=lambda _event: None,
        on_log=lambda _summary: None,
        on_done=lambda code, stderr: done.append((code, stderr)),
    )

    runner.stop()
    fake_process = FakeProcess()
    fake_process.stdin.write = lambda _text: None  # type: ignore[method-assign]
    runner.process = fake_process  # type: ignore[assignment]
    runner.stop()

    assert fake_process.terminated
    assert fake_process.poll() == -15
