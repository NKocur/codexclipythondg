# Ollama Local Provider Plan

## Goal

Add `Ollama` as a global and per-role provider option in
`pyqt_multi_agent_workbench_codex_claude.py`. It must support local models
that can inspect the selected workspace, create workflow artifacts, edit
files, run verification, and emit the existing handoff markers.

This is not a text-only chat integration. The current role prompts expect an
agent to use the workspace and shell, so Ollama needs a host-managed tool loop.

## Constraints

- Keep `Codex` and `Claude` behavior unchanged.
- Preserve the current global provider and per-role `Default / Global` model.
- Use Ollama's local API at `http://localhost:11434/api` by default.
- Do not add the Ollama Python package. Use the standard library HTTP client
  unless a later requirement justifies a dependency.
- Restrict all filesystem tools to the selected workspace.
- Reuse the existing command-output limits, activity output, event log, and
  workflow session persistence patterns.

## Provider And Settings

1. Add `Ollama` to `PROVIDERS`.
2. Extend `WorkflowState` with:
   - `ollama_base_url`, defaulting to `http://localhost:11434/api`
   - `ollama_keep_alive`, defaulting to an explicit, conservative duration
   - optional `ollama_context_length` and thinking controls, if exposed
3. Persist those values through `workflow_state_to_dict` and
   `workflow_state_from_dict`.
4. Add an Execution Settings section with:
   - base URL input
   - installed-model combo box
   - refresh-models action using `GET /api/tags`
   - endpoint/model status feedback
5. Continue using the existing global `Model` field and role `Role model`
   override for the selected Ollama model. A role with `Default / Global` uses
   the global provider and model.
6. Add an explicit `elif provider == "Ollama"` branch in
   `start_current_role`. Do not rely on the current fallback branch, because it
   treats every unrecognized provider as Codex.

## Remote Ollama Hosts

Ollama can also be hosted on another machine on the local network. Treat this
as an explicit remote connection mode, not as a variation of the default local
endpoint. The selected endpoint applies to every role using the Ollama provider.

### Connection Tab

Add a third top-level tab named `Ollama` alongside the existing `Run` and
`Agents` tabs. It is a configuration surface, not a workflow view. Organize it
into the following sections:

- Connection mode: `Local` or `Network host` segmented control.
- Endpoint: scheme, host/IP address, port, and API path; display the effective
  base URL. Default Local to `http://localhost:11434/api`.
- Security: HTTPS toggle/validation, optional custom CA certificate path, and
  a warning for plaintext HTTP outside loopback addresses.
- Authentication: optional bearer-token field, stored through the platform
  credential store when available; never write the raw token to workflow,
  preset, role-library, activity, or transcript JSON files.
- Model discovery: `Test connection` and `Refresh models` actions. The latter
  uses `GET /api/tags` against the configured endpoint and populates the
  Ollama model picker.
- Runtime: keep-alive setting, request timeout, and an optional display of
  loaded models from `GET /api/ps`.

The current `Execution Settings` box should show a concise Ollama connection
summary and an `Open Ollama Settings` command when Ollama is selected. Keep
the editable remote connection fields in the third tab so the Run view remains
focused on launching work.

### Configuration Model

Replace the single `ollama_base_url` plan item with an `OllamaConnectionSettings`
dataclass or equivalent structured fields:

- `mode`: `local` or `network`
- `base_url`: normalized API URL
- `verify_tls`: boolean
- `ca_bundle_path`: optional path
- `credential_key`: an opaque key/name used to retrieve the bearer token
- `connect_timeout_seconds` and `request_timeout_seconds`
- `keep_alive`

Persist only non-secret settings in workflow sessions. For portability, a
loaded session whose credential key cannot be resolved must prompt for a token
or report that the remote connection needs authentication; it must not silently
fall back to unauthenticated access.

### Remote Request Handling

1. Use the same `/api/tags`, `/api/ps`, and `/api/chat` APIs for local and
   remote hosts; only the URL, TLS, and authentication configuration differ.
2. Add an `Authorization: Bearer <token>` header only when a token is present.
3. Apply short connection timeouts for health/model discovery and a separately
   configurable, longer timeout for streaming agent requests.
4. Treat certificate verification failures, timeouts, DNS failures, refused
   connections, HTTP `401`/`403`, and unavailable models as distinct actionable
   errors in the Ollama tab and workflow activity log.
5. Do not log endpoint credentials, authorization headers, or raw HTTP request
   headers. Redact credentials in any diagnostic URL or exception text.
6. Close the active streamed HTTP response when Stop is selected; this is the
   only reliable client-side cancellation mechanism for a remote stream.

### Security Boundary

The Ollama host generates tool calls, but the workbench executes them on the
computer running this application. A remote model must never receive broader
tool permissions merely because it is on a private network.

- Keep the tool allow-list, workspace path checks, command timeouts, output
  caps, and sandbox/bypass behavior identical for local and remote Ollama.
- Require an explicit acknowledgement before first use of a plaintext `http`
  endpoint whose host is not `localhost`, `127.0.0.1`, or `::1`.
- Prefer HTTPS and a bearer token for network hosts; document that exposing an
  unauthenticated Ollama server on a LAN gives every reachable client access to
  submitted prompts and model execution.
- Do not support arbitrary proxy configuration in the initial version. Add it
  only with a defined credential-redaction and TLS policy.

### Remote Host Tests

Add tests beyond the local provider coverage:

1. URL normalization for local, DNS, IPv4, and IPv6 hosts.
2. TLS verification and custom CA configuration behavior.
3. Bearer token retrieval, request headers, and log/transcript redaction.
4. Connection-test outcomes for timeout, DNS, certificate, `401`/`403`, and
   unavailable-model failures.
5. Equivalent streamed chat and tool-loop behavior against a mock remote HTTP
   server.
6. Confirmation that remote-model tool calls retain the same local workspace
   and command restrictions as local-model tool calls.

## Runner Design

Create `OllamaRunState` and `OllamaExecRunner`, parallel to
`ClaudeRunState` and `ClaudeExecRunner`.

`OllamaExecRunner` should:

1. Call `POST /api/chat` with the selected model, `stream: true`, and a
   message containing the existing generated role prompt.
2. Parse Ollama's newline-delimited JSON stream on a background thread.
3. Translate its lifecycle into the existing internal events:
   - emit `thread.started` with an application-generated run ID
   - emit tool activity as `item.started` / `item.completed`
   - emit the final text as an `item.completed` `agent_message`
   - emit `turn.completed` or `turn.failed`
4. Report HTTP and JSON errors as runner failures with useful endpoint/model
   diagnostics.
5. Support cancellation by closing the active HTTP response/connection and
   marking the run stopped.

Ollama does not provide a Codex/Claude-style server session ID. Initial
support should treat each role run as a fresh request; the artifacts directory
and relayed handoff message already provide durable context. Do not claim that
Ollama role sessions are resumable. Persisting full per-role message histories
can be considered later if it is needed.

## Tool Loop

Define a small, explicit tool schema for `POST /api/chat`:

- `list_workspace(path)`
- `read_workspace_file(path)`
- `write_workspace_file(path, content)`
- `run_workspace_command(command, timeout_seconds)`

Implementation requirements:

1. Validate every requested path with the existing workspace/path safety
   helpers before reading or writing.
2. Reject paths outside the selected workspace, including traversal and
   symlink escapes.
3. Route shell commands through a dedicated executor that observes the
   existing sandbox, bypass, and output-limit settings. Do not pass the
   generic `Extra args` setting to Ollama API calls.
4. Capture stdout/stderr with the existing truncation and saved-output rules.
5. Emit `command_execution` and `file_change` events in the current event
   shape so existing activity, artifact, and error UI works unchanged.
6. Add each assistant tool call and the resulting `tool` message to the Ollama
   conversation, then continue the API loop until no tool calls remain.
7. Set a maximum tool-loop iteration count and a per-command timeout to avoid
   runaway local-model behavior.
8. When command execution is not permitted by the selected settings, return a
   clear tool error to the model rather than attempting execution.

## Model Compatibility

Ollama tool use depends on the chosen model. The UI should state that agentic
roles require a tool-calling-capable local model, and should surface a clear
error when a model returns malformed or unsupported tool calls. The first
version should not maintain a hard-coded recommendation list because available
local models vary by machine and evolve quickly.

## Tests

Add focused tests using a local mock HTTP server or injected HTTP transport:

1. Provider and settings serialization, including global inheritance and role
   overrides.
2. Installed-model parsing from `/api/tags`.
3. NDJSON stream parsing, final agent message mapping, and error handling.
4. Single, multiple, and streamed tool-call loops.
5. Workspace traversal and symlink-escape rejection.
6. Shell permission, timeout, cancellation, and output truncation behavior.
7. Handoff marker capture from Ollama final text.
8. Regression coverage that Codex and Claude still use their existing runners.

## Documentation References

- [Ollama API introduction](https://docs.ollama.com/api/introduction)
- [Ollama streaming](https://docs.ollama.com/api/streaming)
- [Ollama tool calling](https://docs.ollama.com/capabilities/tool-calling)
- [Ollama local authentication](https://docs.ollama.com/api/authentication)

## Acceptance Criteria

- A user can choose Ollama globally or for a single role.
- A user can refresh and select a locally installed model.
- A tool-calling-capable local model can read/write only inside the selected
  workspace, run permitted commands, produce its expected artifact, and relay
  a handoff.
- Stop, timeout, malformed tool calls, unavailable endpoint, and missing model
  errors leave the workflow in a clear blocked/error state.
- Codex and Claude workflows continue to run and resume as before.
