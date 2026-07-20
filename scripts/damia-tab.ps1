<#
.SYNOPSIS
  Add the Damia bot tab to the CURRENT zellij session (run from INSIDE zellij).

.DESCRIPTION
  Uses `zellij action new-tab --layout` so your existing default layout — tab-bar,
  status-bar, navigation — is preserved; we only add a "damia" tab with the TUI plus
  placeholder probe panes.

  To start completely fresh first (one-shot kill of every session, no orphan processes):
      zellij delete-all-sessions --yes
      zellij attach --create main          # your default layout + plugins
  then run this script from inside that session.
#>
[CmdletBinding()]
param(
    [string]$Name = "damia"
)

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$layout   = (Resolve-Path (Join-Path $repoRoot "zellij\damia-tab.kdl")).Path

if (-not $env:ZELLIJ) {
    Write-Host "Not inside a zellij session." -ForegroundColor Yellow
    Write-Host "Start one first (keeps your default layout + plugins):"
    Write-Host "    zellij attach --create main"
    Write-Host "then re-run this script from inside it."
    exit 1
}

# The layout declares no `cwd` on purpose — it must not carry a machine-specific path. Pass the
# repo root (resolved from this script's own location) so every pane, and every relative path
# inside the layout, resolves correctly whatever the checkout location or user account.
Write-Host "Adding tab '$Name' from $layout"
Write-Host "  cwd: $repoRoot"
zellij action new-tab --cwd $repoRoot --layout $layout --name $Name
