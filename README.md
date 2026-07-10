# Multi-Agent Codex Workbench

PyQt workbench for running a file-backed multi-agent Codex workflow. The canonical app entry point is:

```powershell
py -3.12 .\pyqt_multi_agent_workbench.py
```

The app delegates Codex CLI execution to `powershell_codex_viewer.py`, which launches `codex exec --json` and streams JSONL events back to the UI.

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

## Build

```powershell
.\scripts\build_exe.ps1
```

Use `-InstallDeps` if PyInstaller or PyQt6 are missing:

```powershell
.\scripts\build_exe.ps1 -InstallDeps
```
