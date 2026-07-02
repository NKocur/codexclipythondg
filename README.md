# Codex Exec JSONL Console

DragonGUI app that runs Codex through the structured CLI path instead of scraping an interactive PowerShell window.

Run it with the Python 3.12 environment that has DragonGUI installed:

```powershell
py -3.12 .\powershell_codex_viewer.py
```

Use the prompt box to send a task to Codex. The app launches:

```powershell
codex exec --json --color never --sandbox workspace-write --cd <workspace> -
```

and reads newline-delimited JSON events from stdout. It shows:

- **Final Response**: latest `agent_message` text.
- **Activity**: command executions, file changes, tool calls, and related progress.
- **Event Log**: lifecycle events such as `thread.started`, `turn.started`, and `turn.completed`.
- **Raw JSONL**: recent raw events for debugging.

This is intentionally different from the first terminal-scraping version. The Codex TUI is rendered for humans; `codex exec --json` is the reliable path for Python software.