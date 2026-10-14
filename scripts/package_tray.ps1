param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$')]
    [string]$Version
)
$ErrorActionPreference = 'Stop'
$repoRoot = Split-Path -Parent $PSScriptRoot
Push-Location $repoRoot
try {
    $exe = Join-Path $repoRoot 'dist\WorkbenchTray\WorkbenchTray.exe'
    if (-not (Test-Path -LiteralPath $exe)) { throw 'Build the tray application first: scripts\build_tray.ps1 -InstallDeps' }
    $report = 'build\release-selftest-' + [Guid]::NewGuid().ToString('N') + '.json'
    # A relative output path avoids quoting ambiguity when the checkout contains spaces.
    $check = Start-Process -FilePath $exe -ArgumentList @('--self-test-output', $report) -WindowStyle Hidden -PassThru
    if (-not $check.WaitForExit(60000)) {
        $check.Kill()
        throw 'Packaged self-test timed out.'
    }
    $check.Refresh()
    if ($check.ExitCode -ne 0) { throw "Packaged self-test failed: $($check.ExitCode)" }
    $result = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
    if (-not $result.ok -or -not $result.frozen -or $result.port -eq 8765) { throw 'Invalid packaged self-test result.' }
    & ./.venv-tray/Scripts/python.exe scripts/package_tray.py --version $Version
    if ($LASTEXITCODE -ne 0) { throw 'Release packaging failed.' }
} finally { Pop-Location }
