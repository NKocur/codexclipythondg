# Plan: Improved Multi-Agent Codex Workbench

## Goal

Create a new improved Python script for `fake_multi_agent_workbench.py` rather than modifying the original in place. The new script should preserve the working Codex JSONL orchestration behavior, make the multi-agent flow easier to understand, and improve the layout/interface for daily use.

Proposed output file: `improved_multi_agent_workbench.py`.

## Current Workspace Findings

- `fake_multi_agent_workbench.py` is a single-file DragonGUI app around `CodexExecRunner` from `powershell_codex_viewer.py`.
- The app defines five fixed roles: Planner, Reviewer, Implementer, Tester, and Archivist.
- It launches each role through `codex exec --json`, captures JSONL events, writes/reads markdown artifacts under `artifacts/`, and relays messages through explicit markers such as `[[HANDOFF_TO_REVIEWER]]`.
- It already has several layout improvements compared with `powershell_codex_viewer.py`: a sidebar, goal panel, status bar, conversation panel, tabs for current step/artifacts/activity/debug, and role prompt/settings controls.
- Main pain points are still single-class complexity, crowded first screen, mixed normal/debug surfaces, weak visual hierarchy for agent progress, and brittle marker parsing/state transitions that are hard to inspect.
- Existing repo state is dirty outside the target file: `powershell_codex_viewer.py` is modified, `__pycache__/powershell_codex_viewer.cpython-312.pyc` is modified, and `WINERROR_193_FIX.md` is untracked. Do not revert or rewrite these.

## Current Research Notes

Source used: current OpenAI Codex manual fetched through the `openai-docs` skill. Local cached manual reported current on 2026-07-06.

Relevant findings:

- `codex exec` is the documented non-interactive interface for scripts and automation.
- `codex exec --json` emits JSONL events including `thread.started`, `turn.started`, `turn.completed`, `turn.failed`, `item.*`, and `error`; item types include agent messages, reasoning, command executions, file changes, MCP tool calls, web searches, and plan updates.
- New automation should prefer explicit sandbox settings and least privilege; `danger-full-access` should be reserved for controlled environments.
- `codex exec resume <SESSION_ID>` is documented for continuing previous non-interactive sessions.
- Codex subagent guidance emphasizes keeping the main thread clean, delegating noisy work to specialized agents, returning summaries rather than raw logs, and being careful with parallel write-heavy workflows.
- Current Codex has native subagent concepts and custom agents, but this workbench is intentionally a file-backed external orchestrator. The improved script should make that distinction clear in UI labels and prompts rather than pretending to be native Codex subagents.

## Design Direction

Build a focused orchestration workbench:

- First screen: goal, agent pipeline, primary conversation, current artifact/summary.
- Secondary surfaces: role prompt, settings, event log, raw JSONL.
- Debug data should be available but visually subordinate.
- Agent state should be readable at a glance: queued, running, complete, blocked, handoff ready.
- Controls should map to workflow actions: Start, Stop, Step, Relay, Refresh, Clear.
- The app should make handoff state explicit: source, target, body length, validation status, and whether auto-relay is enabled.

## Implementation Steps

1. Create `improved_multi_agent_workbench.py` by copying the useful runtime behavior from `fake_multi_agent_workbench.py`.
2. Keep imports and execution compatible with the current repo: continue using `dragongui` and `CodexExecRunner` from `powershell_codex_viewer.py`.
3. Split responsibilities inside the new script even if kept in one file:
   - data classes for role/workflow/handoff state
   - prompt and artifact helpers
   - handoff parser/validator helpers
   - GUI class for presentation and event wiring
4. Refactor the layout:
   - top command band with goal, workspace, current phase, and primary workflow buttons
   - left pipeline column with role status rows and artifact status
   - center conversation or current agent output as the dominant panel
   - right/bottom tab group for artifacts, activity, prompts/settings, and debug
   - bottom status bar for concise runtime status
5. Improve status rendering:
   - centralize role status updates
   - show active role, elapsed time, pending handoff target, and auto-relay state
   - keep recent activity compact and newest-first as the current script does
6. Improve handoff handling:
   - keep existing marker protocol for compatibility
   - make extraction and validation standalone functions where possible
   - display rejection reasons in activity/events
   - avoid duplicate relays by retaining the current handoff key behavior
7. Improve artifact UX:
   - show all expected artifacts including `SUMMARY.md`
   - show missing artifact placeholders clearly
   - refresh artifacts after each run and from a button
8. Preserve behavior that matters:
   - `atexit` cleanup
   - Windows process lifetime handling indirectly through `CodexExecRunner`
   - role session IDs unless `ephemeral` is enabled
   - `--json`, `--color never`, workspace, model, sandbox, skip-git, extra args behavior from the runner
9. Add focused verification:
   - run `python -m py_compile improved_multi_agent_workbench.py`
   - if practical, instantiate the GUI class in a minimal smoke check without starting `app.run`
   - manually inspect the file for accidental original-file mutation

## Role Handoffs

Reviewer should check this plan before implementation:

- Is the proposed new-file approach aligned with the user request?
- Are there missing risks around DragonGUI API compatibility?
- Is the plan scoped tightly enough to avoid rewriting the runner or unrelated files?

Implementer should:

- Create `improved_multi_agent_workbench.py`.
- Avoid touching `fake_multi_agent_workbench.py` unless a read-only comparison is needed.
- Prefer small internal helper functions over a large framework rewrite.
- Keep the script runnable the same way as the original.

Tester should:

- Compile the new script.
- Verify no syntax/runtime import errors occur before `app.run`.
- If DragonGUI supports local execution in this environment, launch the script long enough to confirm the window initializes, then stop it.
- Document any untested GUI behavior honestly.

Archivist should:

- Summarize the new script, verification results, and any known follow-up improvements.

## Risks

- DragonGUI API details may be underdocumented locally; implementation should reuse known-good component patterns from the existing scripts.
- GUI smoke testing may be limited in headless or noninteractive environments.
- The current app's marker protocol can still be fooled by malformed model output; improvements should validate and surface issues, but not overcomplicate the protocol.
- Changing default sandbox behavior could surprise users. Preserve existing defaults unless deliberately changed and called out.
- Parallel native Codex subagents are documented, but this user asked for an improved external multi-agent workbench. Do not pivot to native subagents instead of creating the new file.

## Stop Conditions

- Stop and return to reviewer if `improved_multi_agent_workbench.py` cannot import because required local dependencies are unavailable.
- Stop and ask for direction if DragonGUI lacks the layout primitives needed for the planned interface and no close pattern exists in current files.
- Stop before changing `powershell_codex_viewer.py` unless the new script cannot work without a runner fix.
- Stop after producing the new script and basic verification; do not run a full multi-agent Codex workflow unless explicitly requested, because it can consume significant time/tokens.

## Direct Message To Reviewer

Please review `artifacts/PLAN.md` for scope, risks, and technical accuracy. Pay special attention to whether the plan appropriately preserves the existing Codex JSONL runner behavior while improving the UI in a new script, and whether any required DragonGUI compatibility checks are missing.
