# Publish a release your friends' trackers will auto-install.
#   .\release.ps1 1.2.0
# Bumps __version__, runs the tests, commits, tags, pushes, and creates the
# GitHub release (the auto-updater only ever installs published releases).
param([Parameter(Mandatory = $true)][string]$Version)
$ErrorActionPreference = "Stop"

if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must look like 1.2.0" }

$init = Join-Path $PSScriptRoot "livery_tracker\__init__.py"
# Read/write as UTF-8 explicitly: PS 5.1's default read encoding is ANSI and
# silently corrupts non-ASCII characters.
$content = [System.IO.File]::ReadAllText($init)
$content = $content -replace '__version__ = ".*"', "__version__ = `"$Version`""
[System.IO.File]::WriteAllText($init, $content, (New-Object System.Text.UTF8Encoding($false)))

& (Join-Path $PSScriptRoot ".venv\Scripts\python.exe") -m pytest tests/ -q
if ($LASTEXITCODE -ne 0) { throw "Tests failed - not releasing." }

git add -A
git commit -m "Release v$Version"
git tag "v$Version"
git push origin main --tags
gh release create "v$Version" --title "v$Version" --generate-notes

Write-Host ""
Write-Host "Released v$Version - friends' trackers will pick it up at their next 4 AM check (or via /update)." -ForegroundColor Green
