param([switch]$InstallDeps)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $trayPython = Join-Path $repoRoot '.venv-tray\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $trayPython)) {
        & python -m venv .venv-tray
        if ($LASTEXITCODE -ne 0) { throw 'Could not create the tray build environment.' }
    }
    if ($InstallDeps) {
        & $trayPython -m pip install -r requirements-tray.txt
        if ($LASTEXITCODE -ne 0) { throw 'Could not install tray build dependencies.' }
    }
    & $trayPython -m PyInstaller --noconfirm workbench_tray.spec
    if ($LASTEXITCODE -ne 0) { throw 'Tray application build failed.' }
    Write-Host 'Built dist\WorkbenchTray\WorkbenchTray.exe. Keep the entire WorkbenchTray folder together.'
} finally {
    Pop-Location
}
