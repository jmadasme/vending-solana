# kiosk_windows.ps1 - Open the vending UI fullscreen in Microsoft Edge.
# Requires the app running on http://127.0.0.1:8000 (see run_windows.ps1).
# Exit kiosk with Alt+F4 or Ctrl+Alt+Del.
$ErrorActionPreference = "Stop"
$url = "http://127.0.0.1:8000"

$edge = "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
if (-not (Test-Path $edge)) {
    $edge = "C:\Program Files\Microsoft\Edge\Application\msedge.exe"
}
if (-not (Test-Path $edge)) {
    throw "Microsoft Edge not found."
}

Start-Process -FilePath $edge -ArgumentList @(
    "--kiosk", $url,
    "--edge-kiosk-type=fullscreen",
    "--no-first-run",
    "--disable-features=Translate"
)
