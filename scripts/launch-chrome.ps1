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

.PARAMETER ChromeExe
  Path to chrome.exe. If omitted, auto-detected from the registry App Paths and the common
  install locations: Program Files, Program Files (x86), and per-user LocalAppData.

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
    [string]$ChromeExe = "",
    [string]$StartUrl = "https://damia.timesheetportal.com/",
    [switch]$KillExisting,
    [switch]$Probe,
    [int]   $WatchSeconds = 45,
    [int]   $PortWaitSeconds = 15
)

$ErrorActionPreference = 'Stop'

function Resolve-ChromeExe {
    param([string]$Explicit)
    if ($Explicit) { return $Explicit }

    $candidates = @()
    # Registry App Paths first (works wherever Chrome registered itself).
    foreach ($root in @(
        'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
        'HKLM:\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe',
        'HKCU:\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\chrome.exe')) {
        try {
            $p = (Get-ItemProperty -Path $root -ErrorAction Stop).'(default)'
            if ($p) { $candidates += $p }
        } catch { }
    }
    # Then the common install locations (64-bit, 32-bit, per-user).
    if ($env:ProgramFiles)        { $candidates += (Join-Path $env:ProgramFiles 'Google\Chrome\Application\chrome.exe') }
    if (${env:ProgramFiles(x86)}) { $candidates += (Join-Path ${env:ProgramFiles(x86)} 'Google\Chrome\Application\chrome.exe') }
    if ($env:LOCALAPPDATA)        { $candidates += (Join-Path $env:LOCALAPPDATA 'Google\Chrome\Application\chrome.exe') }

    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c)) { return $c }
    }
    return $null
}

function Reset-CleanExit {
    # After a forced kill, Chrome's profile is flagged as having crashed, so the next launch
    # reopens the previous tabs ("restore pages?"). Stamp a clean exit + disable session restore
    # so we get ONE fresh tab (the StartUrl) instead. Only safe to touch when no debug-profile
    # Chrome is running (we call this right after KillExisting).
    param([string]$ProfileDir)
    $prefs = Join-Path $ProfileDir 'Default\Preferences'
    if (-not (Test-Path $prefs)) { return }
    try {
        $j = Get-Content $prefs -Raw | ConvertFrom-Json -AsHashtable
        if (-not $j.ContainsKey('profile')) { $j['profile'] = @{} }
        $j['profile']['exit_type'] = 'Normal'
        $j['profile']['exited_cleanly'] = $true
        if (-not $j.ContainsKey('session')) { $j['session'] = @{} }
        $j['session']['restore_on_startup'] = 1   # 1 = New Tab page; do NOT restore last session
        ($j | ConvertTo-Json -Depth 100 -Compress) | Set-Content -Path $prefs -Encoding UTF8 -NoNewline
        Write-Host "Reset clean-exit flags (no tab restore on launch)."
    } catch {
        Write-Host "  (could not reset clean-exit flags: $($_.Exception.Message))"
    }
}

$ChromeExe = Resolve-ChromeExe -Explicit $ChromeExe
if (-not $ChromeExe -or -not (Test-Path $ChromeExe)) {
    throw ("Could not find chrome.exe automatically. Pass -ChromeExe 'C:\path\to\chrome.exe'. " +
           "Looked in the registry App Paths and Program Files / Program Files (x86) / LocalAppData.")
}

if ($KillExisting) {
    # Kill ONLY the Chrome processes launched against our dedicated debug profile — matched by
    # --user-data-dir in the command line (the browser process and all its children carry it).
    # Your normal Chrome (default user-data-dir) is left running.
    $needle = "--user-data-dir=$ProfileDir"
    $procs = @(Get-CimInstance Win32_Process -Filter "Name = 'chrome.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.CommandLine -and $_.CommandLine.Contains($needle) })
    if ($procs.Count -gt 0) {
        Write-Host "Killing $($procs.Count) debug-profile chrome.exe process(es) (your other Chrome windows are left alone)..."
        foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Milliseconds 500
    } else {
        Write-Host "No debug-profile Chrome found to kill."
    }
    # The kill marks the profile as crashed; stamp a clean exit so we don't get the previous
    # tabs restored on relaunch.
    Reset-CleanExit -ProfileDir $ProfileDir
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
    "--hide-crash-restore-bubble",   # belt-and-braces: never show the "restore pages?" bubble
    "--no-first-run",
    "--no-default-browser-check",
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
