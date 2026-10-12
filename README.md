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
Invalid/missing choice markers, missing artifacts, CLI errors, or the turn limit stop
the workflow and preserve completed outputs for inspection.

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

### Build on a previous run

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
python -m unittest discover -s tests -p test_run_history.py
node tests/test_live_adapter.cjs
node tests/test_role_deliverables.cjs
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
