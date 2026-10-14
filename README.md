# Multi-Agent Codex Workbench

PyQt workbench for running a file-backed multi-agent Codex workflow. The canonical app entry point is:

```powershell
py -3.12 .\pyqt_multi_agent_workbench.py
```

The app delegates Codex CLI execution to `powershell_codex_viewer.py`, which launches `codex exec --json` and streams JSONL events back to the UI.

## Requirements

- Python 3.11, 3.12, or 3.13. Python 3.12 is the primary build target used by the helper scripts.
- Codex CLI available on `PATH` or configured in the app.
- Runtime Python packages: `PyQt6` and `dragongui`.
- Build Python packages: `PyInstaller`, `PyQt6`, and `dragongui`.

## Important Files

- `pyqt_multi_agent_workbench.py`: main GUI application.
- `powershell_codex_viewer.py`: Codex CLI runner/helper used by the GUI.
- `role_presets.json`: saved role library and presets.
- `pyqt_multi_agent_workbench.spec`: PyInstaller packaging spec.
- `scripts/build_exe.ps1`: Windows build script.

## Folders

- `docs/`: historical notes and troubleshooting docs.
- `legacy/`: older prototype workbench files kept for reference.
- `artifacts/`: generated workflow outputs, ignored by git.
- `.codex-workbench-sessions/`: saved runtime sessions, ignored by git.

## Local web interface

### Windows tray application

Launch `dist\WorkbenchTray\WorkbenchTray.exe`. Keep the entire `WorkbenchTray`
folder together, including `_internal`; Python is bundled. Left-click the tray
icon to open the web interface, or right-click for status, **Open Workbench**,
**Start with Windows**, and **Quit**. Startup is optional and applies at your
Windows sign-in, without opening a browser or terminal. Enable it only after
placing the application in its permanent location; toggle it off before moving
or removing that folder, then back on from the new location if needed.

The icon changes color while running, waiting, or failed. Windows notifications
announce waiting states, completion, and failures (subject to Windows notification
settings). Quitting an owned server asks before stopping an active workflow.
If the normal web server is already running, the tray attaches to it and its Quit
item closes only the tray; it never takes over or stops that existing server.
If an attached server is later stopped, relaunch the tray to start its own server.

Packaged history and logs live under `%LOCALAPPDATA%\MultiAgentWorkbench`;
result files stay in the selected project workspace. Launching this build from
the checkout's `dist` folder imports existing checkout run records when it starts
its own server, without changing their original files. Codex/Claude/Ollama still
require their own installations and provider sign-in. This is a portable Windows
build, not an installer; startup is not enabled by building or testing it.

To rebuild in an isolated Python environment:

```powershell
.\scripts\build_tray.ps1 -InstallDeps
```

The build uses `.venv-tray` and leaves the regular server process alone. The
packaged `--self-test-output <path>` mode checks bundled assets and Markdown on a
temporary port without creating a tray icon or changing startup registration.

### GitHub release downloads

The **Windows tray release** Actions workflow builds on Windows, runs the web/tray
tests, checks the compiled executable on a temporary port, and packages the whole
app folder with a version manifest and SHA-256 checksum. It never starts an agent
or enables Windows startup. `dist/` stays ignored by Git.

Commit and push the tray source, requirements, build/package scripts, tests, and
`.github` workflow files before creating a release tag. To prepare a version:

```powershell
git tag -a tray-v0.1.0 -m "Workbench Tray 0.1.0"
git push origin tray-v0.1.0
```

The tag push creates a **draft** release with the Windows x64 ZIP and checksum.
Review the draft under the repository's Releases page, edit its notes if needed,
then publish it. Existing releases are never overwritten by a rerun; recovery
builds remain downloadable from the Actions run. No personal access token is needed;
the workflow uses GitHub's built-in token with release permissions.

For a build without a release, choose **Actions → Windows tray release → Run
workflow**. Manual runs upload an Actions artifact only. You can also package an
existing local build with `./scripts/package_tray.ps1 -Version preview`; the ZIP
and checksum appear in `dist/releases/`. The script smoke-tests the executable
before packaging and includes only the application bundle, not project files,
credentials, run history, or the build environment.

### Run from Python

From the repository root, run:

```powershell
python web_workbench.py
```

Open **http://127.0.0.1:8765**. Use `--port 8766` if needed. The web server uses
the Python standard library plus `markdown-it-py` for formatted Markdown previews;
install the web dependency with `python -m pip install -r requirements-web.txt`.
PyQt and dragongui are not required for it.
Codex CLI must be installed and signed in on this machine.
Launch the server from your normal terminal, not a coding tool's restricted
execution sandbox: child CLI processes inherit that restriction and may fail to
access their home directory or authentication. Startup now checks `codex login status`
before serving the page and reports failures immediately. Workbench still opens if
sign-in needs attention so you can recover from its interface. This does not run an
agent or change the sandbox setting used for agent tasks.

Use **Provider sign-in** (also available under Connections) to check Codex and Claude.
**Sign in** starts the official CLI login flow, which opens the provider's browser
page. Finish there, then check the status again and retry your task. If the browser
does not open, the dialog shows the terminal command to use. Login attempts expire
after three minutes and can be cancelled. Credentials and login output are not sent
to the web UI. Claude sign-in is supported, but live task execution remains Codex-only.

Every Codex run checks sign-in first. Recognized expired-credential errors also offer
sign-in recovery after a failed run; quota, network, and filesystem errors do not.
Authentication checks confirm stored credentials, not subscription entitlement or
available usage. Signing in never automatically retries a task.

The server serves the v2 design with a live adapter. Choose **Whole team** or
**One agent**, enter a goal, and choose a project folder in the workspace dialog.
Browse reads real directories and refreshes automatically while open, or immediately
with **Refresh**. You can also type a full path. File access defaults to read-only on the first live visit.

The role editor also supports **Additional required reports**: enter an HTML and/or
PDF filename (for example `REPORT.html` and `REPORT.pdf`). Leave both blank for the
usual output only. Requirements persist with saved teams and apply in single-agent
mode too; file-edit permission is required. Describe the report content in the role's
job instructions. The agent creates the reports alongside the team's Markdown notes,
and the workflow stops if a required file is missing, empty, or fails a basic format
check. PDF generation depends on tools available to the agent; Workbench does not
bundle a PDF converter or verify full document validity. Results offer downloads
(up to 50 MB). **Open** displays formatted Markdown, isolated HTML, or the browser's
native PDF viewer. The same previews are available from History. HTML previews
support embedded styling and data images; scripts, remote resources, and embedded
frames are disabled. PDFs can always be downloaded if the browser cannot display them.
Files remain
in the run's artifact directory after a server restart and remain downloadable
from History while those files are still present.

For a team run:

To build from scratch, click **New blank team** beside the team picker. Add teammates
one at a time, then use the picker’s **Save as a new team** action to name and keep
the lineup. Empty drafts survive refreshes; add at least one teammate before running.

1. Pick or edit a team. If it contains other providers, click **Use Codex for this
   team**. This explicitly changes all roles to Codex and clears incompatible model
   names on converted roles. Claude/Ollama task execution is not connected yet.
2. In Workspace, choose **Can edit files**. Team runs need to write shared artifacts;
   the single-agent option still supports read-only work.
3. Set a goal, optionally change the maximum agent turns (default 20), and click
   **Run team**. One real `codex exec --json` process runs at a time.
4. Watch activity and open actual generated Markdown files in Results. Each run uses
   a unique `artifacts/workbench/<run-id>/` directory. Revisited roles update their
   output file; Results identifies the version. Previews are limited to 100,000
   characters, with the full path shown when truncated.

**Next in line** advances in order. **Let them choose** requires a valid handoff to
one of the selected teammates and supports send-backs. A final reviewer with only
earlier recipients can explicitly approve completion. **End the run** finishes early.
Invalid/missing choice markers, missing artifacts, or CLI errors stop
the workflow and preserve completed outputs for inspection.
At the turn limit, the workflow pauses before the next agent and asks whether to
continue. **Allow N more turns** adds another block equal to the initial limit;
**Stop** ends the run. No agent runs while that decision is pending. Each extension
requires another explicit choice, and the pending handoff is preserved.

**Pause after this agent** lets the current process finish and waits before launching
the next role. **Continue** resumes that handoff. Turning off **Keep going automatically**
waits at every handoff. You can change this switch while waiting. **Stop** terminates
the current process or cancels a waiting handoff. It does not undo changes already made.
Keep the server terminal open; Ctrl+C stops it. Activity updates every 700 ms.

This milestone supports one active run and fresh agent sessions. A browser refresh
reconnects to the active run. Saved run records survive server restarts in
`.codex-workbench-sessions/web-runs/`; earlier `web-run-*.json` snapshots are imported
automatically. Runs are checkpointed at launch, handoffs, and completion. A run that
was active when the server stopped appears as interrupted; it is not automatically
relaunched. Claude/Ollama execution and provider session memory are not connected yet.
Single-agent Results show the agent response; team Results
show the actual output files captured at each completed turn.
The original `mockup/index-v2.html` still opens independently as a simulated demo.

### Implement a long Markdown plan

For new runs, choose **Implement a plan · verified batches**. This upgraded preset
adds structured progress and revision-matched validation. It appears separately
so saved teams, customized prompts, and active runs keep their existing behavior.
After updating the app, let any active run finish before restarting the server.
The new preset is disabled when connected to an older server.

The coordinator can dispatch directly to implementation, review, or testing.
It inventories every source-plan step once, groups related steps into useful
batches, and selects work whose dependencies are verified. It can split independent
steps out of a blocked batch without carrying over approvals. Each role writes a
small `PLAN_UPDATE.json`; the server validates it and owns `PLAN_STATE.json`.
The source plan and registered step inventory cannot silently change mid-run.

The server fingerprints each batch's submitted changed-file manifest, assigns
implementation revisions, and accepts review/test receipts only for that revision.
Every acceptance criterion needs evidence and a passing result from both roles;
skipped checks cannot verify a batch. Changed files invalidate earlier receipts,
including affected previously verified batches and their dependent work. The
coordinator must arrange fresh validation. Reviewers also check that the file
manifest covers the actual changes. These gates validate reported evidence and
file identity; agents still judge plan coverage, evidence quality and correctness.

Repeated issue IDs or missing inputs are counted. Three occurrences send work
back to the coordinator and require a different approach before retrying that
batch. Other independent work can continue. When no useful work can proceed, the
coordinator requests input and the run pauses. Home shows what is missing and how
to resume; optionally enter updated instructions before pressing **Continue**.
Invalid structured updates also pause with a specific explanation, preserving
accepted state for the coordinator to repair. Turn limits still apply.

Home displays verified steps, work awaiting validation, blocked steps, the current
batch and revision, and an expandable step list. History distinguishes the run
status from the plan outcome. **Plan complete** requires every registered step
to be verified; partial and blocked reports can finish the run without claiming
plan completion. Each accepted role report/update is archived under
`evidence/turn-NNNN/` inside the run directory, so shared filenames do not erase
previous evidence. Handoffs carry the current batch and compact state summary;
full state and older evidence stay available on disk. Saved follow-up context
includes the structured state, but its approvals must be revalidated against the
new run's actual workspace.

The five role IDs/order and required handoff routes are part of this protocol.
You can customize role prompts, names and models, but removing/reordering roles
or removing required routes is rejected. Use a blank
team for an unrestricted workflow. This preset does not commit, push or deploy.

The earlier **Implement a Markdown plan** preset remains available:

Choose **Implement a Markdown plan** in the web team picker. All five roles use
Codex. Selecting this preset sets a 100-turn limit (agent executions, not plan
steps); you can lower it before starting. Give the plan's path in your goal, for
example: `Implement docs/IMPLEMENTATION_PLAN.md in dependency order. Review and
test every batch. Preserve unrelated changes.` Choose the project folder and
enable file edits.

The Plan Coordinator keeps `PLAN_PROGRESS.md` with stable step IDs, dependencies,
acceptance criteria, current batch, verified evidence, and remaining work. Each
batch goes through Batch Implementer, Batch Reviewer, and Batch Tester. Findings
go back for repair; verified batches return to the coordinator for the next batch.
Plan Reporter produces `PLAN_OUTCOME.md` when all steps are verified or the team
needs to report a blocker. Prompts require explicit COMPLETE/PARTIAL/BLOCKED
verdicts and forbid treating skipped checks as passing. These judgments are made
by the agents; the workflow enforces permitted routing and the turn limit.

If the run reaches its limit, approve another block of turns to resume the same
workflow. After **Pause after this agent** reaches a waiting state, you can close
Workbench and later choose **Resume paused run** in History. This restores the
same run, original team, artifacts, pending handoff, plan state, and turn budget.
It waits for **Continue** (or explicit additional turns) before launching an agent.
Keep the workspace and result files in place. Resuming does not upgrade the team
to a newer preset. Runs interrupted midway through an agent cannot use this safe
checkpoint path.

If you stop a run or interrupt it before it pauses, use History's **Continue from this
run**, select the same preset, and ask it to resume using the saved progress ledger.
The coordinator is instructed to reconcile prior evidence with the current code
before skipping previously verified steps. This preset does not commit, push, or
deploy. New built-in presets are added to existing browser libraries without
replacing saved team edits.

### Build on a previous run

Verified batches use separate persistent Codex sessions for each role within a
run. A role resumes its own session on later visits; other roles retain independent
context. Session IDs survive a saved pause/restart. Changing a role's model starts
a new session for that role. Codex stores its normal session files locally; a new
Workbench run starts new role sessions. Current accepted batch state takes
precedence over earlier conversation context.

`BATCH_CONTEXT.json` provides the current batch, server-calculated hashes, drift
and receipts. The server records command starts/completions, elapsed time, exit
codes and bounded output in `evidence/turn-NNNN/commands.jsonl`. Agents should
reference this evidence instead of creating capture frameworks or whole-repository
hash inventories. Implementers focus on building and focused checks, reviewers
on independent change/risk inspection, and testers on comprehensive acceptance.
Repository-required checks still apply. Review may report `pending_tests` for
execution assigned to testing; this permits routing to the tester, never final
verification. Testing must pass all substantive criteria with a current review.
Both receipts are service-enforced completion gates, not circular prerequisites
to put in the batch's acceptance list.

Follow-ups copy supporting evidence alongside reports, preserving relative paths
and recording SHA-256 hashes in `source-evidence-manifest.json`. The copy is bounded
to 100 MB / 5000 files and rejects symlinks/junctions. Reusing evidence requires
checking its applicability to current files and environment; copied claims never
automatically become accepted receipts. To keep existing accepted progress,
restore a paused run instead of starting a follow-up with a fresh inventory.

Crash diagnostics are recorded locally while an agent runs. Each run gets a
`diagnostics/<run-id>/activity.jsonl` beside the saved run records (packaged app:
`%LOCALAPPDATA%/MultiAgentWorkbench/sessions/web-runs/diagnostics/`). Command
start/completion records include UTC time, turn, command ID and exit status;
activity is flushed to disk immediately. History snapshots also refresh during
activity, at most once every two seconds. A five-second sampler records system
CPU/memory and Workbench/descendant PIDs, names, CPU time, thread counts and memory.
CPU time is cumulative per process; compare consecutive samples to estimate load.
Sampling stops between turns and at shutdown and launches no external commands.

Logs rotate at roughly 8 MiB each, retaining four files per run. Diagnostic write
failures are exposed in `diagnostics_error` without failing the agent. Sampling
failures appear as `sampling_error` records. Command/output logs can contain
project data or secrets printed by tools; inspect them before sharing. These
measurements do not include hardware temperatures and cannot by themselves prove
what caused a machine crash. Sudden power loss can still lose the latest record.

If the provider reports that the selected model is at capacity, Workbench pauses
the affected agent and preserves its handoff and plan state. Use **Retry agent**
when ready, optionally entering a different model ID (blank uses the provider
default). This changes only that role in the current run. Failed capacity attempts
are retained separately and do not consume the completed-turn budget. Retries
are explicit, not automatic; the agent is instructed to inspect possible partial
file changes before repeating work. The saved pause can also be restored after
a restart. Other failures retain their existing error handling.

Exhausted reconnects, provider HTTP 500/502/503/504 responses, and recognized
connection/stream timeouts also pause for **Retry agent**, preserving the role's
session, incoming handoff and completed-turn budget. The CLI performs its own
reconnect attempts first; Workbench adds no unbounded automatic retry loop.
If the CLI recovers and emits `turn.completed`, earlier temporary error messages
remain in the log but no longer fail the successful turn. Artifacts, handoffs,
review and testing are still validated normally. Authentication, quota, code
errors and user-requested stops are not converted into reconnect pauses.

Open **History**, inspect a saved run, and choose **Continue from this run**. Or use
**View** to reopen its completed Home screen, including the saved goal, team,
activity feed, and results. **Back to latest run** returns to the live view; status
polling does not replace the saved run while you inspect it. You can also use
**Build on a previous run** on Home. Select any team, enter new instructions, and
run it. The source selection defaults the workspace to the earlier project's folder;
you can choose a different folder before starting. For example, follow a research
run with "Compare the report with `my-notes.md`" or switch to a coding team and ask
it to plan and implement the findings. Place comparison files in the selected
workspace and name them in the goal.

A follow-up is a new linked run with its own outputs. The earlier run's registered
result files are copied into the new run's `source/` folder, and `source-context.json`
provides its goal, status, agent responses, and parent ID. This works across teams
and workspaces without resuming a provider conversation. It copies result files,
not an entire repository. Missing originals cause a clear startup error; source
files are limited to 100 MB total. History also supports downloading older results.

### Improve Workbench from a run

Open **Improve**, select a run (including one that is still running), and ask a
question. **Review and discuss** launches an independent, read-only Codex session
with the run snapshot, chat history and Workbench source. It does not take over,
pause or stop the team being reviewed. **Stop this request** stops only this chat
request. Conversations are saved locally; sending a message for a previously
selected run/source folder resumes its saved conversation. A running run is
captured again for each new message, so reviews describe the evidence available
at that time.

Choose **Prepare improvements** to produce a concrete patch. The server copies
the current tracked and nonignored Workbench files into an isolated Git snapshot,
including uncommitted changes, and the agent edits/tests that copy. It has no
remote and no access to change the source checkout through its workspace sandbox.
The page displays the exact diff, assistant-reported test commands/results and
limitations. These reports are not independent verification; inspect them and
the diff before applying. Failed/skipped reports do not enable Apply.

After reviewing the diff, check the approval box and choose **Apply reviewed
changes**. Applying is blocked while a team workflow is running, paused or stopping.
The server checks every affected source and candidate file against the reviewed
fingerprints, refusing stale patches rather than overwriting newer edits. It
preserves unrelated files and the source Git index. **Revert this improvement**
restores the pre-apply contents, provided the affected files have not changed
again. Neither action commits, pushes, deploys or restarts the server. Restart
manually when ready; packaged applications also need rebuilding from the updated
source. Preparing a new proposal replaces the displayed proposal, so review or
revert the current one first if needed.

The source folder defaults to this repository. Packaged applications need a
Workbench Git checkout selected under **Workbench source folder**. Git and a
signed-in Codex CLI are required for preparation. Chats and candidates live beside
web run history under `improvements/`; they can contain run logs and source copies.
Snapshots are limited to 10 MB per file and 100 MB total, and reviewable patches to
2 MB. This is a local development feature; the standalone HTML demo only displays
the tab. After updating, let existing runs finish before restarting to enable its
new server endpoints.

`workflow_service.py` owns web orchestration without GUI imports. `handoff_protocol.py`
contains the marker parser and validation shared with both desktop entry points.
`run_history.py` handles saved records and prepares source material for follow-ups.

The server binds only to loopback, validates local request origins, and requires
a per-server token for run/stop requests. It keeps Codex sandboxing enabled and
does not use the desktop runner's default sandbox bypass.

Run web integration tests without launching Codex or incurring model usage:

```powershell
python -m unittest discover -s tests -p test_web_workbench.py
python -m unittest discover -s tests -p test_provider_auth.py
python -m unittest discover -s tests -p test_workflow_service.py
python -m unittest discover -s tests -p test_structured_plan.py
python -m unittest discover -s tests -p test_improvement_service.py
python -m unittest discover -s tests -p test_run_history.py
node tests/test_live_adapter.cjs
node tests/test_improve_ui.cjs
node tests/test_role_deliverables.cjs
node tests/test_markdown_plan_preset.cjs --v2
node tests/test_document_preview.cjs
```

## Desktop build

```powershell
.\scripts\build_exe.ps1
```

Use `-InstallDeps` if PyInstaller, PyQt6, or dragongui are missing:

```powershell
.\scripts\build_exe.ps1 -InstallDeps
```
