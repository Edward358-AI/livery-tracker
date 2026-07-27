# Keeps the tracker alive: restarts on exit code 42 (self-update) or crash.
# Used by the logon Scheduled Task that install.ps1 creates.
$root = $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"

while ($true) {
    & $python -m livery_tracker
    $code = $LASTEXITCODE
    if ($code -eq 0) { break }          # clean shutdown -> stay stopped
    if ($code -eq 42) {
        Write-Host "Restarting after self-update..."
        continue
    }
    Write-Host "Tracker exited with code $code - restarting in 15s..."
    Start-Sleep -Seconds 15
}
