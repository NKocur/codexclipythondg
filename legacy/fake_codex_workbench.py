from __future__ import annotations

from pathlib import Path

import dragongui as dg


SAMPLE_CONVERSATION = """User:
Can you inspect this repo and make the Codex GUI easier to use?

Codex:
I found the current UI is doing two jobs at once: normal conversation and low-level debugging. I would keep the conversation central, move settings out of the way, and put activity/debug streams behind tabs.

User:
Can you make a fake UI with the updated DragonGUI library so we can compare?

Codex:
Here is a workbench-style layout with a stable top prompt area, a large conversation pane, secondary tabs, and a fixed status bar."""

SAMPLE_ACTIVITY = """[completed exit=0] Get-Location
C:\\Users\\nkocur\\Desktop\\Projects\\Python\\cmdlink

[completed exit=0] rg --files
README.md
powershell_codex_viewer.py
LAYOUT_USABILITY_NOTES.md
fake_codex_workbench.py

[file modified] powershell_codex_viewer.py"""

SAMPLE_EVENTS = """12:06:02  Thread started 019f-demo
12:06:03  Turn started
12:06:04  item.completed: command_execution
12:06:05  item.completed: agent_message (842 chars)
12:06:05  Turn completed | usage {...}"""

SAMPLE_JSONL = """{"type":"thread.started","thread_id":"019f-demo"}
{"type":"turn.started"}
{"type":"item.completed","item":{"type":"command_execution","command":"rg --files","exit_code":0}}
{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}
{"type":"turn.completed","usage":{"input_tokens":1200,"output_tokens":80}}"""


def set_status(status: dg.Label, text: str) -> None:
    status.set_value(text)


def build_app() -> tuple[dg.App, dg.Window]:
    app = dg.App(theme=dg.Theme.dark())
    app.stylesheet(
        """
        Window { gap: 0; }
        Panel { padding: 12px; gap: 10px; border-radius: 8px; }
        Button.primary { background: accent; color: background; }
        TextArea.mono { font-family: Consolas, monospace; font-size: 13px; }
        TextArea.prompt { font-family: Consolas, monospace; font-size: 13px; }
        Label.status { color: muted; }
        """
    )

    win = dg.Window("Codex Workbench Mock", width=1280, height=840)

    with dg.VLayout(style={"height": "100%", "gap": 8, "padding": 10}):
        with dg.MenuBar():
            with dg.Menu("File"):
                dg.MenuItem("New Run")
                dg.MenuItem("Copy Conversation")
            with dg.Menu("View"):
                dg.MenuItem("Conversation")
                dg.MenuItem("Activity")
            with dg.Menu("Help"):
                dg.MenuItem("Layout Notes")

        with dg.Panel("Prompt", style={"flex_shrink": 0}):
            prompt = dg.TextArea(
                value="Inspect this repo and summarize the Codex GUI layout issues.",
                rows=4,
                wrap=True,
                class_="prompt",
            )
            with dg.HLayout(style={"gap": 8, "align_items": "center"}):
                dg.Button("Run", class_="primary")
                dg.Button("Stop")
                dg.Button("Clear")
                dg.Button("Copy Conversation")
                dg.Label("Normal local shell mode", wrap=False, style={"margin_left": 12})

        with dg.HLayout(style={"flex_grow": 1, "min_height": 0, "gap": 10}):
            with dg.Panel("Conversation", style={"flex_grow": 2, "flex_basis": 0, "min_height": 0}):
                dg.TextArea(
                    value=SAMPLE_CONVERSATION,
                    rows=24,
                    wrap=True,
                    class_="mono",
                    style={"height": "100%", "min_height": 0},
                )

            with dg.Tabs(value="activity", style={"flex_grow": 1, "flex_basis": 0, "min_height": 0}):
                with dg.Tab("Final", value="final"):
                    with dg.Panel("Final Response", style={"min_height": 0}):
                        dg.TextArea(
                            value="The app should prioritize Conversation, keep Activity secondary, and move JSONL/Event streams into debug tabs.",
                            rows=24,
                            wrap=True,
                            class_="mono",
                            style={"height": "100%", "min_height": 0},
                        )

                with dg.Tab("Activity", value="activity"):
                    with dg.Panel("Activity", style={"min_height": 0}):
                        dg.TextArea(
                            value=SAMPLE_ACTIVITY,
                            rows=24,
                            wrap=False,
                            class_="mono",
                            style={"height": "100%", "min_height": 0},
                        )

                with dg.Tab("Settings", value="settings"):
                    with dg.Panel("Settings", style={"min_height": 0}):
                        with dg.VLayout(style={"gap": 8}):
                            with dg.HLayout(style={"gap": 8}):
                                dg.Label("Codex command", wrap=False, style={"width": 145})
                                dg.TextInput(
                                    value=str(Path.home() / "AppData/Roaming/npm/codex.CMD"),
                                    style={"flex_grow": 1},
                                )
                            with dg.HLayout(style={"gap": 8}):
                                dg.Label("Workspace", wrap=False, style={"width": 145})
                                dg.TextInput(value=str(Path.cwd()), style={"flex_grow": 1})
                            with dg.HLayout(style={"gap": 8, "align_items": "center"}):
                                dg.Label("Sandbox", wrap=False, style={"width": 145})
                                dg.Dropdown(
                                    ["normal local shell", "workspace-write", "read-only", "danger-full-access"],
                                    value="normal local shell",
                                    style={"width": 220},
                                )
                                dg.Checkbox("Skip git repo check", checked=True)
                                dg.Checkbox("Ephemeral", checked=False)
                            with dg.HLayout(style={"gap": 8}):
                                dg.Label("Extra args", wrap=False, style={"width": 145})
                                dg.TextInput(placeholder="--profile name --config key=value", style={"flex_grow": 1})

                with dg.Tab("Debug", value="debug"):
                    with dg.Panel("Debug Streams", style={"min_height": 0}):
                        with dg.Tabs(value="events"):
                            with dg.Tab("Events", value="events"):
                                dg.TextArea(value=SAMPLE_EVENTS, rows=20, wrap=False, class_="mono")
                            with dg.Tab("Raw JSONL", value="raw"):
                                dg.TextArea(value=SAMPLE_JSONL, rows=20, wrap=False, class_="mono")

        with dg.StatusBar():
            status = dg.Label("Ready. Mock UI only; no Codex subprocess is running.", class_="status", wrap=False)
            dg.SmallButton("Set Busy", on_click=lambda: set_status(status, "Running fake Codex task..."))
            dg.SmallButton("Set Ready", on_click=lambda: set_status(status, "Ready. Mock UI only; no Codex subprocess is running."))

    return app, win


if __name__ == "__main__":
    app, window = build_app()
    app.run(window)