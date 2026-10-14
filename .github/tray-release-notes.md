Windows x64 portable build of Multi-Agent Workbench.

1. Download and extract the ZIP.
2. Keep the entire WorkbenchTray folder together, including `_internal`.
3. Run `WorkbenchTray.exe`. Left-click its tray icon to open Workbench; right-click for status and settings.
4. Optionally enable **Start with Windows** after placing the folder in its permanent location.

Python is included. Codex, Claude, and Ollama require their own installations and sign-ins; live agent execution currently supports Codex.

If Workbench is already serving port 8765, the tray attaches to it and leaves it running when the tray exits. Startup is off by default. Packaged history is stored under `%LOCALAPPDATA%\MultiAgentWorkbench`; generated results remain in your selected workspace.

The `.sha256` file contains the ZIP's SHA-256 checksum. This build is unsigned.
