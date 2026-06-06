<#
.SYNOPSIS
  Launch Chrome with the CDP debug port enabled, using a dedicated profile.

.DESCRIPTION
  Chrome 136+ silently ignores --remote-debugging-port when launched against
  the default user-data-dir. This script always uses a dedicated profile at
  $env:LOCALAPPDATA\damia-timesheet-bot\chrome-profile so the port actually opens.

  First-time use: log in to Damia in the launched window; the session cookie
  persists in the dedicated profile for subsequent runs.

.PARAMETER Port
  CDP debug port (default 9222).

.PARAMETER ProfileDir
  Override the dedicated profile directory.

.PARAMETER KillExisting
  Kill all running chrome.exe processes before launching. Use when you suspect
  a stale Chrome is holding the profile lock.

.PARAMETER Probe
  After Chrome is up and the port is reachable, run spikes/damia_probe.py.

.PARAMETER WatchSeconds
  When -Probe is set, pass this through to the probe's --watch-seconds.

.EXAMPLE
  .\scripts\launch-chrome.ps1
  .\scripts\launch-chrome.ps1 -KillExisting
  .\scripts\launch-chrome.ps1 -KillExisting -Probe -WatchSeconds 90
#>

[CmdletBinding()]
param(
    [int]   $Port = 9222,
    [string]$ProfileDir = "$env:LOCALAPPDATA\damia-timesheet-bot\chrome-profile",
    [string]$ChromeExe = "C:\Program Files\Google\Chrome\Application\chrome.exe",
    [string]$StartUrl = "https://damia.timesheetportal.com/",
    [switch]$KillExisting,
    [switch]$Probe,
    [int]   $WatchSeconds = 45,
    [int]   $PortWaitSeconds = 15
)

$ErrorActionPreference = 'Stop'

if (-not (Test-Path $ChromeExe)) {
    throw "Chrome not found at: $ChromeExe  (pass -ChromeExe to override)"
}

if ($KillExisting) {
    $procs = Get-Process chrome -ErrorAction SilentlyContinue
    if ($procs) {
        Write-Host "Killing $($procs.Count) existing chrome.exe process(es)..."
        $procs | Stop-Process -Force
        Start-Sleep -Milliseconds 500
    }
}

# Detect the "default profile + flag silently ignored" case before launching.
$defaultProfile = "$env:LOCALAPPDATA\Google\Chrome\User Data"
if ([System.IO.Path]::GetFullPath($ProfileDir).TrimEnd('\') -ieq
    [System.IO.Path]::GetFullPath($defaultProfile).TrimEnd('\')) {
    throw "ProfileDir points at Chrome's default user-data-dir. Chrome 136+ will silently ignore --remote-debugging-port. Pick a dedicated directory."
}

New-Item -ItemType Directory -Force -Path $ProfileDir | Out-Null

Write-Host "Launching Chrome..."
Write-Host "  exe:        $ChromeExe"
Write-Host "  port:       $Port"
Write-Host "  profileDir: $ProfileDir"
Write-Host "  startUrl:   $StartUrl"

Start-Process -FilePath $ChromeExe -ArgumentList @(
    "--remote-debugging-port=$Port",
    "--user-data-dir=$ProfileDir",
    $StartUrl
)

# Poll the CDP port until it answers or we give up.
$deadline = (Get-Date).AddSeconds($PortWaitSeconds)
$ready = $false
Write-Host -NoNewline "Waiting for CDP on port $Port"
while ((Get-Date) -lt $deadline) {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -UseBasicParsing -TimeoutSec 1
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch { }
    Write-Host -NoNewline "."
    Start-Sleep -Milliseconds 500
}
Write-Host ""

if (-not $ready) {
    throw "CDP port $Port did not come up within $PortWaitSeconds s. Try -KillExisting, or check that no enterprise policy blocks remote debugging."
}

$info = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/json/version" -UseBasicParsing | Select-Object -ExpandProperty Content | ConvertFrom-Json
Write-Host "CDP ready: $($info.Browser)"

if ($Probe) {
    Write-Host ""
    Write-Host "Running probe (watch window = ${WatchSeconds}s)..."
    & uv run python -m spikes.damia_probe --watch-seconds $WatchSeconds
}
