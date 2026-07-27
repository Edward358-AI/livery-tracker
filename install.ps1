# Aircraft Livery Tracker — one-line installer / updater for Windows.
#
#   iwr -useb https://raw.githubusercontent.com/Edward358-AI/livery-tracker/main/install.ps1 | iex
#
# Installs to %LOCALAPPDATA%\LiveryTracker (override with $env:LT_INSTALL_DIR),
# sets up Python + dependencies, runs the first-time wizard, and registers a
# no-admin Scheduled Task so the tracker starts at logon and keeps itself
# updated. Safe to re-run: it upgrades code in place and never touches your
# data or bot tokens.
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072

$Repo = "Edward358-AI/livery-tracker"
$InstallDir = $env:LT_INSTALL_DIR
if (-not $InstallDir) { $InstallDir = Join-Path $env:LOCALAPPDATA "LiveryTracker" }
$Headers = @{ "User-Agent" = "LiveryTracker-Installer" }

Write-Host ""
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "   Aircraft Livery Tracker - installer"        -ForegroundColor Cyan
Write-Host "=============================================" -ForegroundColor Cyan
Write-Host "   Install folder: $InstallDir"
Write-Host ""

# --- 1. Find (or install) Python 3.10+ --------------------------------------
function Find-Python {
    foreach ($cmd in @("python", "py")) {
        try {
            $exe = (Get-Command $cmd -ErrorAction Stop).Source
            $ver = & $exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($ver) {
                $parts = "$ver".Trim().Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 10) { return $exe }
            }
        } catch {}
    }
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"
    if (Test-Path $guess) { return $guess }
    return $null
}

$Python = Find-Python
if (-not $Python) {
    Write-Host "Python 3.10+ was not found." -ForegroundColor Yellow
    $answer = Read-Host "Install Python 3.12 automatically with winget? [Y/n]"
    if ($answer -and $answer.Trim().ToLower().StartsWith("n")) {
        Write-Host "Install Python from https://www.python.org/downloads/ and re-run this installer."
        exit 1
    }
    winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
    $Python = Find-Python
    if (-not $Python) {
        Write-Host "Python installed but not found yet - close this window, open a NEW terminal, and re-run the installer." -ForegroundColor Yellow
        exit 1
    }
}
Write-Host "[1/5] Python: $Python"

# --- 2. Download the latest release (falls back to main) ---------------------
$zipUrl = "https://codeload.github.com/$Repo/zip/refs/heads/main"
$tag = "main"
try {
    $rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $Headers
    if ($rel.zipball_url) { $zipUrl = $rel.zipball_url; $tag = $rel.tag_name }
} catch {}
Write-Host "[2/5] Downloading $tag ..."

$tmp = Join-Path $env:TEMP ("lt-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $tmp | Out-Null
$zipPath = Join-Path $tmp "src.zip"
Invoke-WebRequest -Uri $zipUrl -Headers $Headers -OutFile $zipPath
Expand-Archive -Path $zipPath -DestinationPath $tmp
$srcRoot = Get-ChildItem -Path $tmp -Directory | Select-Object -First 1

# --- 3. Copy code into place (state in data\ and .env is never touched) ------
# Stop a running installer-managed instance first so files aren't in use.
try { Stop-ScheduledTask -TaskName "LiveryTracker" -ErrorAction Stop } catch {}

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
$codeItems = @("livery_tracker", "requirements.txt", "runner.ps1", "runner.sh",
               "install.ps1", "install.sh", "tracker.sh", "livery-tracker.service",
               "install-service.ps1", "README.md", "LICENSE")
foreach ($item in $codeItems) {
    $src = Join-Path $srcRoot.FullName $item
    if (-not (Test-Path $src)) { continue }
    $dst = Join-Path $InstallDir $item
    if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
    Copy-Item $src $dst -Recurse
}
Remove-Item $tmp -Recurse -Force
Write-Host "[3/5] Code installed."

# --- 4. Virtual environment + dependencies -----------------------------------
$venvPy = Join-Path $InstallDir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    & $Python -m venv (Join-Path $InstallDir ".venv")
}
& $venvPy -m pip install --quiet --upgrade pip
& $venvPy -m pip install --quiet -r (Join-Path $InstallDir "requirements.txt")
Write-Host "[4/5] Dependencies ready."

# --- 5. First-run wizard + autostart -----------------------------------------
Push-Location $InstallDir
try {
    if (-not (Test-Path (Join-Path $InstallDir ".env")) -and
        -not (Test-Path (Join-Path $InstallDir "data\.env"))) {
        Write-Host ""
        Write-Host "Time to set up your Telegram bots - the wizard will walk you through it." -ForegroundColor Cyan
        & $venvPy -m livery_tracker --setup
    }
} finally { Pop-Location }

$runner = Join-Path $InstallDir "runner.ps1"
$autostart = ""
try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName "LiveryTracker" -Action $action -Trigger $trigger `
        -Settings $settings -Force | Out-Null
    Start-ScheduledTask -TaskName "LiveryTracker"
    $autostart = "Scheduled Task 'LiveryTracker' (starts at logon)"
} catch {
    $startup = [Environment]::GetFolderPath("Startup")
    $cmdFile = Join-Path $startup "LiveryTracker.cmd"
    "start `"`" powershell -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`"" |
        Out-File -FilePath $cmdFile -Encoding ascii
    Start-Process powershell -WindowStyle Hidden `
        -ArgumentList "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$runner`""
    $autostart = "Startup-folder entry (starts at logon)"
}

Write-Host ""
Write-Host "=============================================" -ForegroundColor Green
Write-Host "   Livery Tracker is installed and running!"   -ForegroundColor Green
Write-Host "=============================================" -ForegroundColor Green
Write-Host "   Version:    $tag"
Write-Host "   Autostart:  $autostart"
Write-Host "   Updates:    automatic (daily check at 4 AM, or /update in Telegram)"
Write-Host ""
Write-Host "   Open Telegram and send /status to your bot to say hello."
Write-Host "   Add aircraft with /add <tail>, airports with /addairport <code>."
Write-Host ""
