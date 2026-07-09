# Review: Improved Multi-Agent Codex Workbench Plan

## Approval Status

Approved with required implementation cautions.

The plan is aligned with the user request: create a new improved script, preserve the current file-backed Codex JSONL orchestration behavior, and avoid mutating `fake_multi_agent_workbench.py` or unrelated dirty files. The scope is reasonable and does not pivot to native Codex subagents.

## Findings

1. Runner behavior must be preserved by reusing `CodexExecRunner`, not by reimplementing command construction.

   `powershell_codex_viewer.py` already centralizes important command behavior, including fresh-run vs resume differences. In particular, fresh runs use `codex exec --json --color never`, `--cd`, sandbox handling, and optional flags, while resumed runs use `codex exec resume --json ... <SESSION_ID> -` and intentionally skip some fresh-run flags. The plan's preservation bullet is directionally correct, but the implementer should treat the runner as the compatibility boundary and avoid duplicating `_build_command()`.

2. DragonGUI compatibility is the main missing check.

   In this shell, `import dragongui` currently fails with `ModuleNotFoundError: No module named 'dragongui'`. The existing scripts therefore cannot be import-smoked here unless the environment is adjusted. The plan should explicitly require a preflight dependency check and should document when GUI initialization is untested because DragonGUI is unavailable.

3. A minimal class instantiation smoke test is insufficient for DragonGUI layout compatibility.

   `MultiAgentCodexGui.__init__()` does not construct widgets; most DragonGUI API usage happens inside `run()`. A test that only instantiates the GUI class would miss invalid widget names, constructor parameters, context-manager behavior, and style issues. If DragonGUI is available, the tester should launch the app just long enough to build the window, or at least exercise the layout-building path before `app.run()` if the implementation separates it.

4. The plan correctly preserves marker protocol compatibility, but validation should remain behavior-compatible.

   Existing handoff parsing relies on `[[HANDOFF_TO_*]]`, `[[END_HANDOFF]]`, `[[COMMANDDOCK_DONE]]`, `[[APPROVED]]`, length bounds, duplicate keys, and buffering by role. Moving this into helpers is a good improvement, but the implementer should avoid changing accepted/rejected message semantics unless the UI clearly reports the change.

5. Artifact UX improvement should include `SUMMARY.md` in both display and context.

   The current refresh UI omits a dedicated `SUMMARY.md` display even though the Archivist role expects it. The plan catches this. Implementation should ensure any new generic artifact mapping covers all role artifacts, including future additions, rather than hard-coding only four outputs again.

## Gaps And Risks

- The plan should add a concrete DragonGUI preflight: verify the module imports, record its version if available, and confirm the planned widgets exist or are already used successfully in existing files.
- If DragonGUI is unavailable, implementation can still produce the new script and run `py_compile`, but must not claim GUI runtime verification.
- New layout controls should be built only from known-good primitives already used locally unless the implementer can verify the additional API calls.
- Refactoring into helpers must not accidentally call widget methods before widgets are created; current code assumes UI fields exist after `run()` builds the layout.
- The existing repo is dirty. The plan correctly says not to revert unrelated changes; implementer should also avoid relying on bytecode or generated files.

## Recommended Plan Adjustment

Add this to verification:

- Run `python -m py_compile improved_multi_agent_workbench.py`.
- Run a dependency preflight such as `python -c "import dragongui, powershell_codex_viewer"`.
- If DragonGUI imports, perform a short window-build smoke test; if not, document `ModuleNotFoundError` and limit verification to compile/static review.
- Confirm `git status --short` shows only the new script and expected artifacts changed by this workflow.

## Handoff

[[HANDOFF_TO_IMPLEMENTER]]
The plan is approved with cautions. Create `improved_multi_agent_workbench.py` as a new file, preserve Codex behavior by reusing `CodexExecRunner` and `CodexRunState`, and do not reimplement command construction. Add/keep helpers for handoff parsing and artifact handling, but preserve the existing marker protocol and duplicate-handoff behavior. Important gap: DragonGUI is not importable in this shell right now (`ModuleNotFoundError: No module named 'dragongui'`), so include a dependency preflight and document any GUI smoke-test limitation. Make sure the artifact UI includes `SUMMARY.md`.
[[END_HANDOFF]]
[[COMMANDDOCK_DONE]]
