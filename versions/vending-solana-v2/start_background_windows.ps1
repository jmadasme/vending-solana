# start_background_windows.ps1 - Start the app hidden with logs (for autostart).
# Prints the PID. Logs: vending.out.log / vending.err.log
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

$python = Join-Path $ProjectDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    throw "Virtual environment not found. Run .\setup_windows.ps1 first."
}

$out = Join-Path $ProjectDir "vending.out.log"
$err = Join-Path $ProjectDir "vending.err.log"
$p = Start-Process -FilePath $python `
    -ArgumentList @("-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000") `
    -WorkingDirectory $ProjectDir `
    -WindowStyle Hidden `
    -RedirectStandardOutput $out `
    -RedirectStandardError $err `
    -PassThru
Write-Output $p.Id
