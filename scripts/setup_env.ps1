[CmdletBinding()]
param(
    [switch]$WithDev
)

$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$repoRoot = Split-Path -Parent $PSScriptRoot
$lockName = if ($WithDev) { 'requirements-dev.lock' } else { 'requirements-core.lock' }
$lockPath = Join-Path $repoRoot $lockName
$venvPython = if ($IsWindows) {
    Join-Path $repoRoot '.venv\Scripts\python.exe'
} else {
    Join-Path $repoRoot '.venv/bin/python'
}

if (-not (Test-Path -LiteralPath $lockPath)) {
    throw "Required lock file is missing: $lockPath"
}

$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) {
    Push-Location $repoRoot
    try {
        $arguments = @('sync', '--locked')
        if ($WithDev) { $arguments += @('--extra', 'dev') }
        & $uv.Source @arguments
        if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE" }
    } finally {
        Pop-Location
    }
} else {
    if (-not (Test-Path -LiteralPath $venvPython)) {
        & python -m venv (Join-Path $repoRoot '.venv')
        if ($LASTEXITCODE -ne 0) { throw "python -m venv failed with exit code $LASTEXITCODE" }
    }
    & $venvPython -m pip install --require-hashes -r $lockPath
    if ($LASTEXITCODE -ne 0) { throw "dependency installation failed with exit code $LASTEXITCODE" }
}

$verifyArguments = @((Join-Path $repoRoot 'scripts\verify_runtime.py'))
if ($WithDev) { $verifyArguments += '--dev' }
& $venvPython @verifyArguments
if ($LASTEXITCODE -ne 0) { throw "runtime verification failed with exit code $LASTEXITCODE" }

Write-Host "Environment ready: $venvPython"
