# DragonGUI Layout Usability Notes

After building and using the Codex DragonGUI client, the main layout issue is that the app is doing two jobs at once: normal Codex interaction and debug/event inspection. Those should be separated more clearly.

## Recommended Direction

Make the app feel more like a Codex workbench and less like one tall diagnostic dashboard.

## Suggested Layout

```text
[ Prompt box                                      ]
[ Run ] [ Stop ] [ Clear ]        Status...

[ Conversation                                   ]

Tabs:
  Final Response | Activity | Debug Events | Raw JSONL

[ Settings collapsed at bottom/top ]
```

## Specific Improvements

1. Move configuration into a secondary settings area

   The Codex command, workspace, model, sandbox, extra args, and related options usually do not change every run. They take a lot of vertical room, so they should move into a settings panel, collapsible area, or secondary section.

2. Make Conversation the primary panel

   The most important view is the exchange between the user and Codex. Conversation should be the largest, most prominent area.

3. Use tabs for secondary output

   Final Response, Activity, Event Log, and Raw JSONL do not all need to be visible at once. Tabs would reduce vertical pressure and make the app easier to scan.

4. Keep status separate from controls

   Status text should not compete with buttons or form controls. A fixed bottom or top status bar would make long status messages easier to read.

5. Make Activity compact

   Activity should summarize command/tool/file actions first, then show details when needed. For example:

   ```text
   [ok] Get-Location
   [fail] rg --files
   [running] git status
   ```

6. Separate normal use from debug use

   Most normal runs need Prompt, Conversation, Activity, Run, and Stop. Raw JSONL and Event Log are debugging surfaces and should live behind a Debug tab or option.

## Why This Helps

The current app works, but adding Conversation made the vertical layout crowded. Tabs and a settings area would reduce scrolling, keep the primary workflow visible, and make the app feel closer to a focused Codex client.