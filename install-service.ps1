# Installs the Livery Tracker as a Windows service using NSSM.
# Run from an ELEVATED (admin) PowerShell:
#   powershell -ExecutionPolicy Bypass -File install-service.ps1
# Remove the service with:
#   powershell -ExecutionPolicy Bypass -File install-service.ps1 -Uninstall
param([switch]$Uninstall)

$ErrorActionPreference = "Stop"
$ServiceName = "livery-tracker"
$RepoDir = $PSScriptRoot

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must be run from an elevated (admin) PowerShell."
}

$nssm = Get-Command nssm -ErrorAction SilentlyContinue
if (-not $nssm) {
    Write-Error "NSSM not found on PATH. Install it first:  winget install NSSM.NSSM  (then open a NEW admin PowerShell)."
}
$nssm = $nssm.Source

if ($Uninstall) {
    & $nssm stop $ServiceName
    & $nssm remove $ServiceName confirm
    Write-Host "Service '$ServiceName' removed."
    exit 0
}

$python = Join-Path $RepoDir ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Error "No .venv found. First run:  python -m venv .venv ; .venv\Scripts\pip install -r requirements.txt"
}
if (-not (Test-Path (Join-Path $RepoDir ".env"))) {
    Write-Error "No .env found. First run the setup wizard:  .venv\Scripts\python -m livery_tracker --setup"
}

$existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Service already exists - updating configuration..."
    & $nssm stop $ServiceName 2>$null
} else {
    & $nssm install $ServiceName $python "-m livery_tracker"
}

& $nssm set $ServiceName AppDirectory $RepoDir
& $nssm set $ServiceName DisplayName "Aircraft Livery Tracker"
& $nssm set $ServiceName Description "Telegram notifier for special-livery aircraft movements"
& $nssm set $ServiceName Start SERVICE_AUTO_START
& $nssm set $ServiceName AppStdout (Join-Path $RepoDir "tracker.log")
& $nssm set $ServiceName AppStderr (Join-Path $RepoDir "tracker.log")
& $nssm set $ServiceName AppRotateFiles 1
& $nssm set $ServiceName AppRotateBytes 5242880
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 10000

& $nssm start $ServiceName
Write-Host ""
Write-Host "Service '$ServiceName' installed and started. It will auto-start on boot."
Write-Host "  Status:  Get-Service $ServiceName"
Write-Host "  Logs:    Get-Content tracker.log -Tail 50 -Wait"
