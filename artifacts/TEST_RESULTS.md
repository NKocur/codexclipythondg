# Test Results: Canonical PyQt Workbench Audit Fixes

## Scope

Tested the fixes for `pyqt_multi_agent_workbench.py` and `powershell_codex_viewer.py` against the audit findings in `artifacts/CODE_AUDIT_REPORT.md`.

Added focused pytest coverage in `tests/test_audit_fixes.py` for:

- artifact-name validation and safe fallback
- session-index containment, safe relative persistence, and unsafe delete rejection
- latest-by-offset handoff selection
- bounded command-output retention and bounded write-failure fallback
- stale run event/log/done callback rejection
- atomic JSON replacement failure behavior
- runner stop safety, exception cleanup after `Popen`, and explicit reporting when forced cleanup may leave a process alive

## Commands And Results

### Focused Unit Tests

Command:

```powershell
python -m pytest tests\test_audit_fixes.py -q
```

Result: Passed.

Output:

```text
...........                                                              [100%]
11 passed in 0.35s
```

### Static Compile Check

Command:

```powershell
python -m py_compile pyqt_multi_agent_workbench.py powershell_codex_viewer.py tests\test_audit_fixes.py
```

Result: Passed.

Output: no output.

## Notes

- The first test run exposed two test-harness issues, not implementation defects: an invalid `Path.touch(times=...)` call and a fake Windows process missing runner job-attachment behavior. The tests were corrected and rerun successfully.
- Importing the real `dragongui` backend during tests printed a Windows fatal-exception trace after pytest completed. The test module now stubs `dragongui` before importing `powershell_codex_viewer.py` because these unit tests exercise runner classes only and do not need the GUI backend.
- No full GUI smoke test, packaged executable build, or real Codex CLI run was performed in this tester pass.
- Existing unrelated dirty workspace state was left untouched.

## Next Steps

Run a manual GUI smoke test and a real short Codex execution after confirming the local `dragongui`, PyQt6, and Codex CLI runtime environment is healthy.

[[HANDOFF_TO_MAINTAINABILITY_REVIEWER]]
Focused audit-fix tests are in place and passing for the canonical PyQt workbench and shared runner. I added `tests/test_audit_fixes.py` covering artifact validation, session index containment and unsafe delete rejection, latest-by-offset handoff selection, bounded command-output retention including write-failure fallback, stale run callback rejection, atomic JSON write failure behavior, runner stop/exception cleanup, and explicit cleanup-failure reporting when a process may still be alive. Verification passed with `python -m pytest tests\test_audit_fixes.py -q` (`11 passed`) and `python -m py_compile pyqt_multi_agent_workbench.py powershell_codex_viewer.py tests\test_audit_fixes.py`. See `artifacts/TEST_RESULTS.md` for details. I did not run a full GUI smoke test, packaged build, or real Codex CLI workflow.
[[END_HANDOFF]]
[[COMMANDDOCK_DONE]]
