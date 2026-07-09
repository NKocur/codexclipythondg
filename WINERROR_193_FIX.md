# WinError 193 Investigation and Fixes

Two unrelated bugs were blocking `fake_multi_agent_workbench.py` from starting/running. Both are fixed.

## 1. Codex launch failure: `WinError 193: %1 is not a valid Win32 application`

**Symptom**: Hitting "Start" launched Codex fine at the process level, but any real task
(one that needed Codex to actually run shell commands) failed, and the app's log showed:

```
[stderr]
[WinError 193] %1 is not a valid Win32 application

ERROR  Planner exited with code 1: [WinError 193] %1 is not a valid Win32 application
Starting: C:\Users\nashk\AppData\Roaming\npm\codex exec --json --color never ...
```

Note the launched path has **no extension** (`...\npm\codex`, not `...\npm\codex.cmd`).

**Root cause**: `resolve_codex_command()` in `powershell_codex_viewer.py` used
`shutil.which()` to find the `codex` executable. npm installs three files for a global
CLI on Windows:

- `codex` — a POSIX shell shim (plain text, no extension) for Git Bash/WSL
- `codex.cmd` — a Windows batch shim (the one that actually works)
- `codex.ps1` — a PowerShell shim

Python 3.12 changed `shutil.which()`'s Windows resolution so it can match the **bare
`codex` file directly**, ahead of the `.cmd`/`.exe` variants, even though `PATHEXT`
includes `.CMD`. Confirmed directly:

```powershell
# Python 3.11
> shutil.which('codex')
C:\Users\nashk\AppData\Roaming\npm\codex.CMD

# Python 3.12 (what this app runs under)
> shutil.which('codex')
C:\Users\nashk\AppData\Roaming\npm\codex
```

That bare `codex` file is a text script, not a PE binary — Windows can't `CreateProcess`
it directly, hence `WinError 193`. This is also why it worked on the other computer: a
different Python version (or a different npm layout) resolved the name differently.

**Fix** (`powershell_codex_viewer.py`, `resolve_codex_command()`): check the explicit
extensions before the bare name.

```python
def resolve_codex_command() -> str:
    for name in ("codex.cmd", "codex.exe", "codex"):  # was: ("codex", "codex.cmd", "codex.exe")
        resolved = shutil.which(name)
        if resolved:
            return resolved
    ...
```

No rebuild needed — this is a plain Python fix in this repo.

## 2. Startup crash: wgpu `Surface::configure` panic (DragonGui)

**Symptom**: The app crashed immediately on launch (before "Start" was even clicked):

```
thread '<unnamed>' panicked at .../wgpu-29.0.1/src/backend/wgpu_core.rs:3879:18:
wgpu error: Validation Error
Caused by:
  In Surface::configure
    `SurfaceOutput` must be dropped before a new `Surface` is made
pyo3_runtime.PanicException: wgpu error: Validation Error
```

**Root cause**: In `DragonGui/native/src/runtime.rs`, `WgpuState::render()` acquired the
current surface texture and, when wgpu reported it as `Suboptimal` (common right after a
resize/DPI change or on certain GPU/driver/compositor combinations), immediately called
`surface.configure()` again **while still holding that texture**:

```rust
let texture = match self.surface.get_current_texture() {
    wgpu::CurrentSurfaceTexture::Success(t) => t,
    wgpu::CurrentSurfaceTexture::Suboptimal(t) => {
        self.surface.configure(&self.device, &self.config);  // reconfigures...
        t                                                     // ...while t is still alive
    }
    ...
```

wgpu requires the previously acquired `SurfaceTexture` to be dropped/presented before the
surface is reconfigured. Whether `Suboptimal` is ever returned is GPU/driver-dependent,
which is why this reproduced on this machine and not the other one.

**Fix** (`DragonGui/native/src/runtime.rs`, `WgpuState::render()`): treat `Suboptimal` the
same as `Success` — render and present the frame as-is, and let a later `Outdated`/`Lost`
frame trigger the reconfigure (the pattern already used elsewhere in the same file, in
`render_startup_loading_frame`):

```rust
let texture = match self.surface.get_current_texture() {
    wgpu::CurrentSurfaceTexture::Success(t)
    | wgpu::CurrentSurfaceTexture::Suboptimal(t) => t,
    ...
```

This required rebuilding the native extension (`maturin build --release` in `DragonGui/`)
and copying the resulting `_dragongui.pyd` into the Python 3.12 `site-packages/dragongui`
install actually used by this project.

## Unrelated, but adjacent

A separate multi-agent self-improvement run (using this workbench on its own codebase)
independently changed "bypass Codex sandbox and approvals" from default-on to opt-in, as a
deliberate safety hardening (see `artifacts/SUMMARY.md`). That's why real tasks needing
file writes now require checking **"Unsafe: bypass Codex sandbox and approvals"** in the
UI before hitting Start — it's not part of either fix above, just something that landed
around the same time.
