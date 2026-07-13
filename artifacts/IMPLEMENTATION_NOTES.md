# Implementation Notes

## Changed

- Updated the canonical `pyqt_multi_agent_workbench.py` rather than the older improved/prototype workbench.
- Added run lifecycle state and monotonically increasing run IDs before `CodexExecRunner.start()` is called. Event, log, and done callbacks now carry the captured run ID and stale callbacks are ignored.
- Replaced launch/load/preset guards that only checked `runner.process` with a lifecycle guard covering launching, running, and stopping.
- Added central artifact-name validation. Artifact names must be single relative file names; empty, absolute, drive-qualified, UNC, traversal, and separator-containing names fall back to a safe role-derived `<slug>.md` and emit warnings from GUI ingress points.
- Applied safe artifact names to role library migration/load/save, session role load/save, role editor changes, prompt construction, and artifact context reads.
- Constrained session index, load, save, and delete paths to `workspace/.codex-workbench-sessions/*.codex-workbench.json` behavior. Unsafe index entries are dropped with event warnings, and delete/load/save revalidate containment immediately.
- Replaced direct JSON persistence for role library, session index, and session files with same-directory temp-file writes followed by `os.replace`.
- Changed handoff arbitration to select the latest valid handoff by source offset in the output buffer instead of role order.
- Bounded command-output retention: inline activity output is capped, saved per-command output is capped, write failures log only a bounded preview, retained output files are pruned to a fixed newest-file count, and activity text discloses the caps.
- Hardened `CodexExecRunner` cleanup in `powershell_codex_viewer.py`: stop is safe before/during/after launch, exception/finally paths terminate still-live processes with bounded wait then kill, stderr drain threads are joined with a timeout, Windows job attachment failures are logged as warnings, and non-Windows runs start a process session so cleanup can target the process group.
- Addressed the maintainability review follow-up in `powershell_codex_viewer.py`: process termination now returns a structured `TerminationResult`, cleanup failures are logged as warnings, included in runner stderr, and a still-live process after forced kill produces an explicit nonzero cleanup failure.
- Updated `README.md` and `scripts/build_exe.ps1` so runtime/build dependencies include `PyQt6`, `PyInstaller`, and `dragongui`.

## Verification

- `python -m pytest tests\test_audit_fixes.py -q` passed with 11 tests.
- `python -m py_compile pyqt_multi_agent_workbench.py powershell_codex_viewer.py tests\test_audit_fixes.py` passed.
- `python -c "import PyQt6; import pyqt_multi_agent_workbench as m; print(m.validate_artifact_name('SPEC.md')); print(m.safe_artifact_name('..\\\\x.md','Role')[0])"` passed and printed `SPEC.md` plus the safe fallback `role.md`.

## Remains

- No GUI smoke test or packaged build was run in this implementation pass.
- Existing unrelated dirty workspace state remains, including prior edits to `role_presets.json` and the untracked `.serena/` directory.

[[HANDOFF_TO_CODE_REVIEWER]]
Implemented the audit fixes in the canonical PyQt workbench and shared runner, then addressed the maintainability follow-up for swallowed cleanup failures. `CodexExecRunner` now reports failed graceful/forced termination through warning logs, stderr, and a nonzero cleanup result when the process may still be alive. Focused verification passed with `python -m pytest tests\test_audit_fixes.py -q` (`11 passed`) and `python -m py_compile pyqt_multi_agent_workbench.py powershell_codex_viewer.py tests\test_audit_fixes.py`. Please continue with code review.
[[END_HANDOFF]]
[[COMMANDDOCK_DONE]]
