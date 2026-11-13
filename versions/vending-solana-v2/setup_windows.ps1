# setup_windows.ps1 - Prepare the Python environment on Windows.
# Usage: powershell -ExecutionPolicy Bypass -File .\setup_windows.ps1
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$candidates = @(
    "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
    "C:\Program Files\Python312\python.exe",
    "C:\Python312\python.exe"
)
$py = $candidates | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source }
}
if (-not $py) {
    throw "Python 3.12 not found. Install it with: winget install -e --id Python.Python.3.12"
}

Write-Host "==> Python: $py"
if (-not (Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "==> Creating virtual environment..."
    & $py -m venv .venv
}
Write-Host "==> Installing dependencies..."
& ".venv\Scripts\python.exe" -m pip install --upgrade pip
& ".venv\Scripts\python.exe" -m pip install -r requirements-windows.txt

if (-not (Test-Path ".env") -and (Test-Path ".env.example")) {
    Copy-Item ".env.example" ".env"
    Write-Host "==> .env created from .env.example (edit it if needed)"
}

Write-Host ""
Write-Host "Done. Start the app with: powershell -ExecutionPolicy Bypass -File .\run_windows.ps1"
