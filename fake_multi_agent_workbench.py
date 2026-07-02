from __future__ import annotations

from pathlib import Path

import dragongui as dg


WORKFLOW_STEPS = """1. Planner        complete
2. Reviewer       complete
3. Implementer    running
4. Reviewer       waiting
5. Tester         queued
6. Archivist      queued"""

AGENT_BRIEF = """Planner
Creates PLAN.md from the user's goal.

Reviewer
Reads PLAN.md and writes REVIEW.md with gaps, risks, and approval status.

Implementer
Reads PLAN.md + REVIEW.md, edits the workspace, and writes IMPLEMENTATION_NOTES.md.

Tester
Runs verification, records TEST_RESULTS.md, and sends failures back to Implementer.

Archivist
Writes a final summary and stores the workflow transcript."""

CONVERSATION = """User:
Build a compact Codex GUI that can coordinate several specialized agents.

Planner:
I created PLAN.md with a staged workflow: shell runner, JSONL parser, artifact store, and UI migration.

Reviewer:
PLAN.md is good, but add explicit stop conditions and make every handoff file-backed.

Implementer:
I am applying the approved plan. Current task: migrate the single-agent UI into a workflow shell and keep Codex subprocess behavior unchanged.

Reviewer:
Waiting for IMPLEMENTATION_NOTES.md before re-review."""

PLAN_MD = """# PLAN.md

## Goal
Create a multi-agent Codex workbench that can pass markdown artifacts between role-specific Codex runs.

## Roles
- Planner creates the plan.
- Reviewer critiques and approves or requests changes.
- Implementer edits files.
- Tester verifies behavior.
- Archivist summarizes the completed run.

## Stop Conditions
- Reviewer approves the plan and final implementation.
- Tester reports passing checks.
- User stops the workflow manually.
- An agent reports a blocking error."""

REVIEW_MD = """# REVIEW.md

Status: changes requested

## Findings
- Add a clear artifact history so handoffs are inspectable.
- Keep debug JSONL separate from normal workflow output.
- Make each role prompt visible and editable before execution.

## Approval
Not approved until IMPLEMENTATION_NOTES.md exists."""

IMPLEMENTATION_NOTES = """# IMPLEMENTATION_NOTES.md

Current step: Implementer

## Changes In Progress
- Reuse CodexExecRunner for each role.
- Store per-run JSONL and final response.
- Append handoff markdown into the next role prompt.

## Next
Emit IMPLEMENTATION_NOTES.md, then return to Reviewer."""

ACTIVITY = """[planner completed] wrote artifacts/PLAN.md
[reviewer completed] wrote artifacts/REVIEW.md
[implementer running] reading PLAN.md + REVIEW.md
[command exit=0] rg --files
[command exit=0] Get-Content artifacts/PLAN.md
[file pending] powershell_codex_viewer.py"""

EVENTS = """12:14:02 workflow.started
12:14:05 planner.turn.completed
12:14:07 reviewer.turn.completed
12:14:10 implementer.turn.started
12:14:12 implementer.command.started rg --files
12:14:13 implementer.command.completed exit=0"""

RAW_JSONL = """{"role":"planner","type":"turn.completed","artifact":"PLAN.md"}
{"role":"reviewer","type":"turn.completed","artifact":"REVIEW.md"}
{"role":"implementer","type":"item.started","item_type":"command_execution"}
{"role":"implementer","type":"item.completed","item_type":"command_execution","exit_code":0}"""


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
        Label.section { font-weight: 700; }
        """
    )

    win = dg.Window("Multi-Agent Codex Workbench Mock", width=1360, height=860)

    with dg.VLayout(style={"height": "100%", "gap": 8, "padding": 10}):
        with dg.MenuBar():
            with dg.Menu("Workflow"):
                dg.MenuItem("New Workflow")
                dg.MenuItem("Open Artifacts Folder")
                dg.MenuItem("Export Transcript")
            with dg.Menu("Agents"):
                dg.MenuItem("Edit Role Profiles")
                dg.MenuItem("Duplicate Workflow")
            with dg.Menu("Debug"):
                dg.MenuItem("Show JSONL")
                dg.MenuItem("Validate Handoffs")

        with dg.HLayout(style={"flex_grow": 1, "min_height": 0, "gap": 10}):
            with dg.Sidebar(title="Orchestration", width=250):
                dg.Label("Workflow", class_="section", wrap=False)
                dg.NavItem("Feature Build", page="feature", badge="running")
                dg.NavItem("Bug Fix", page="bug", badge="draft")
                dg.NavItem("Code Review", page="review")

                dg.Label("Agents", class_="section", wrap=False, style={"margin_top": 14})
                dg.NavItem("Planner", page="planner", badge="done")
                dg.NavItem("Reviewer", page="reviewer", badge="waiting")
                dg.NavItem("Implementer", page="implementer", badge="active")
                dg.NavItem("Tester", page="tester")
                dg.NavItem("Archivist", page="archivist")

                dg.Label("Artifacts", class_="section", wrap=False, style={"margin_top": 14})
                dg.NavItem("PLAN.md", page="plan")
                dg.NavItem("REVIEW.md", page="review-md")
                dg.NavItem("IMPLEMENTATION_NOTES.md", page="impl")
                dg.NavItem("TEST_RESULTS.md", page="tests")

            with dg.ScrollArea(axis="y", style={"flex_grow": 1, "min_width": 0, "min_height": 0}):
                with dg.VLayout(style={"gap": 8, "min_width": 0}):
                    with dg.Panel("Goal", style={"flex_shrink": 0}):
                        dg.TextArea(
                            value="Build a compact multi-agent Codex workflow that plans, reviews, implements, tests, and archives through markdown handoffs.",
                            rows=3,
                            wrap=True,
                            class_="prompt",
                        )
                        with dg.Toolbar(gap=8, compact=True):
                            dg.Button("Start", class_="primary")
                            dg.Button("Pause")
                            dg.Button("Step Once")
                            dg.Button("Stop")
                            dg.ToolbarSeparator()
                            dg.Button("Open Artifacts")
                            dg.Button("Copy Handoff")
    
                    with dg.HLayout(style={"flex_grow": 1, "min_height": 0, "gap": 10}):
                        with dg.Panel("Conversation", style={"flex_grow": 3, "flex_basis": 0, "min_height": 0}):
                            dg.TextArea(
                                value=CONVERSATION,
                                rows=22,
                                wrap=True,
                                class_="mono",
                                style={"height": "100%", "min_height": 0},
                            )
    
                        with dg.Tabs(value="current", style={"flex_grow": 2, "flex_basis": 0, "min_height": 0}):
                            with dg.Tab("Current Step", value="current"):
                                with dg.Panel("Implementer", style={"min_height": 0}):
                                    dg.TextArea(
                                        value=WORKFLOW_STEPS + "\n\n" + AGENT_BRIEF,
                                        rows=18,
                                        wrap=True,
                                        class_="mono",
                                    )
    
                            with dg.Tab("Artifacts", value="artifacts"):
                                with dg.Panel("Markdown Handoffs", style={"min_height": 0}):
                                    with dg.Tabs(value="plan"):
                                        with dg.Tab("PLAN.md", value="plan"):
                                            dg.TextArea(value=PLAN_MD, rows=18, wrap=True, class_="mono")
                                        with dg.Tab("REVIEW.md", value="review"):
                                            dg.TextArea(value=REVIEW_MD, rows=18, wrap=True, class_="mono")
                                        with dg.Tab("IMPLEMENTATION", value="impl"):
                                            dg.TextArea(value=IMPLEMENTATION_NOTES, rows=18, wrap=True, class_="mono")
    
                            with dg.Tab("Activity", value="activity"):
                                with dg.Panel("Agent Activity", style={"min_height": 0}):
                                    dg.TextArea(value=ACTIVITY, rows=18, wrap=False, class_="mono")
    
                            with dg.Tab("Role Prompt", value="prompt"):
                                with dg.Panel("Current Role Prompt", style={"min_height": 0}):
                                    dg.TextArea(
                                        value="You are the Implementer. Read PLAN.md and REVIEW.md. Make the smallest safe code changes. Write IMPLEMENTATION_NOTES.md when done.",
                                        rows=12,
                                        wrap=True,
                                        class_="prompt",
                                    )
                                    with dg.HLayout(style={"gap": 8}):
                                        dg.Label("Model", wrap=False, style={"width": 80})
                                        dg.TextInput(value="", placeholder="Codex config default", style={"flex_grow": 1})
                                    with dg.HLayout(style={"gap": 8}):
                                        dg.Label("Workspace", wrap=False, style={"width": 80})
                                        dg.TextInput(value=str(Path.cwd()), style={"flex_grow": 1})
                                    with dg.HLayout(style={"gap": 8}):
                                        dg.Checkbox("Normal local shell mode", checked=True)
                                        dg.Checkbox("Auto-advance after success", checked=False)
    
                            with dg.Tab("Debug", value="debug"):
                                with dg.Panel("Debug", style={"min_height": 0}):
                                    with dg.Tabs(value="events"):
                                        with dg.Tab("Events", value="events"):
                                            dg.TextArea(value=EVENTS, rows=18, wrap=False, class_="mono")
                                        with dg.Tab("Raw JSONL", value="raw"):
                                            dg.TextArea(value=RAW_JSONL, rows=18, wrap=False, class_="mono")

        with dg.StatusBar():
            status = dg.Label(
                "Workflow running: Implementer is preparing IMPLEMENTATION_NOTES.md",
                class_="status",
                wrap=False,
            )
            dg.SmallButton("Set Running", on_click=lambda: set_status(status, "Workflow running: Implementer is active"))
            dg.SmallButton("Set Waiting", on_click=lambda: set_status(status, "Waiting for Reviewer approval"))
            dg.SmallButton("Set Done", on_click=lambda: set_status(status, "Workflow complete"))

    return app, win


if __name__ == "__main__":
    app, window = build_app()
    app.run(window)
