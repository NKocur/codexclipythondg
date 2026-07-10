param(
    [string]$Python = "",
    [switch]$InstallDeps
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Resolve-Python {
    param([string]$Requested)

    if ($Requested) {
        return $Requested
    }
    if ($env:WORKBENCH_PYTHON) {
        return $env:WORKBENCH_PYTHON
    }

    foreach ($candidate in @("py -3.12", "py -3.13", "py -3.11", "python")) {
        $parts = $candidate.Split(" ")
        $exe = $parts[0]
        $args = @($parts | Select-Object -Skip 1) + @("-c", "import sys; print(sys.executable)")
        try {
            $resolved = & $exe @args 2>$null
            if ($LASTEXITCODE -eq 0 -and $resolved) {
                return $candidate
            }
        } catch {
        }
    }

    throw "Could not find Python. Pass -Python 'py -3.12' or set WORKBENCH_PYTHON."
}

function Invoke-Python {
    param(
        [string]$PythonCommand,
        [string[]]$PythonArgs
    )

    $parts = $PythonCommand.Split(" ")
    $exe = $parts[0]
    $args = @($parts | Select-Object -Skip 1) + $PythonArgs
    & $exe @args
}

$PythonCommand = Resolve-Python $Python
Write-Host "Using Python: $PythonCommand"

if ($InstallDeps) {
    Invoke-Python $PythonCommand @("-m", "pip", "install", "--upgrade", "pyinstaller", "PyQt6")
}

Invoke-Python $PythonCommand @("-c", "import PyInstaller, PyQt6; import powershell_codex_viewer; print('packaging dependencies ok')")

if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "Missing build/runtime dependencies. Install them with:"
    Write-Host "  .\scripts\build_exe.ps1 -InstallDeps"
    Write-Host ""
    Write-Host "If dragongui is installed only in a specific Python, pass it explicitly:"
    Write-Host "  .\scripts\build_exe.ps1 -Python 'py -3.12'"
    exit 1
}

Invoke-Python $PythonCommand @("-m", "PyInstaller", "--clean", "--noconfirm", "pyqt_multi_agent_workbench.spec")

Write-Host ""
Write-Host "Built: $RepoRoot\dist\MultiAgentWorkbench.exe"
Write-Host "Codex CLI is still external. On the target machine, verify that 'codex.cmd' is on PATH or use Detect in the app."
