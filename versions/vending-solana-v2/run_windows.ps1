# run_windows.ps1 - Run the FastAPI web app on Windows (port 8000).
# Usage: powershell -ExecutionPolicy Bypass -File .\run_windows.ps1
$ErrorActionPreference = "Stop"
$ProjectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectDir

if (-not (Test-Path ".venv\Scripts\python.exe")) {
    throw "Virtual environment not found. Run .\setup_windows.ps1 first."
}

cmd /c ".venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000 1>> vending.out.log 2>> vending.err.log"
