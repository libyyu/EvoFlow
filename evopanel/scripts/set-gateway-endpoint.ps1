<#
.SYNOPSIS
  CLI fallback for the EvoFlow desktop "Cloud Gateway" endpoint (P0).

.DESCRIPTION
  The desktop settings page (Settings -> Remote Access -> Cloud Gateway) writes
  <profile_dir>/gateway-endpoint.json. This script writes the same file without
  launching the UI, which is useful for headless / scripted setup.

  Priority used by the desktop app:
    EVOFLOW_GATEWAY_URL (process env) > gateway-endpoint.json > local sidecar

  The profile directory is intentionally NOT guessed from a hash here. If more
  than one profile exists, pass -ConfigPath explicitly (see -List, or copy the
  "config path" value shown in the settings page).

.EXAMPLE
  .\set-gateway-endpoint.ps1 -List

.EXAMPLE
  .\set-gateway-endpoint.ps1 -BaseUrl https://gateway.example.com

.EXAMPLE
  .\set-gateway-endpoint.ps1 -ConfigPath "$env:USERPROFILE\.evoflow\profiles\default\gateway-endpoint.json" -Clear

.NOTES
  Restart EvoFlow after changing this file. URL resolution picks up the change
  on the next launch; a restart is also what skips spawning the local Gateway
  and LangGraph sidecars.
#>
param(
  [string]$BaseUrl,
  [string]$ConfigPath,
  [switch]$Dev,
  [switch]$Clear,
  [switch]$List
)

$ErrorActionPreference = 'Stop'

function Get-EvoflowRoot {
  if ($env:EVOFLOW_CONFIG_DIR) { return $env:EVOFLOW_CONFIG_DIR }
  $homeDir = if ($env:USERPROFILE) { $env:USERPROFILE } else { $HOME }
  if ($Dev) { return (Join-Path $homeDir '.evoflow-dev') }
  return (Join-Path $homeDir '.evoflow')
}

function Get-ProfileDirs {
  $profiles = Join-Path (Get-EvoflowRoot) 'profiles'
  if (-not (Test-Path -LiteralPath $profiles)) { return @() }
  return @(Get-ChildItem -LiteralPath $profiles -Directory)
}

function Read-EndpointValue([string]$path) {
  if (-not (Test-Path -LiteralPath $path)) { return '<not set>' }
  try {
    $obj = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    return [string]$obj.base_url
  } catch {
    return '<invalid json>'
  }
}

$dirs = Get-ProfileDirs

if ($List) {
  Write-Host "EvoFlow config root: $(Get-EvoflowRoot)"
  if ($dirs.Count -eq 0) {
    Write-Host 'No profile directories found. Launch EvoFlow once, then retry.'
    return
  }
  foreach ($d in $dirs) {
    $p = Join-Path $d.FullName 'gateway-endpoint.json'
    Write-Host ("{0,-24} {1,-16} {2}" -f $d.Name, (Read-EndpointValue $p), $p)
  }
  return
}

if (-not $ConfigPath) {
  if ($dirs.Count -eq 0) {
    throw 'No profile directories found. Launch EvoFlow once, or pass -ConfigPath with the path shown in Settings -> Remote Access.'
  }
  if ($dirs.Count -gt 1) {
    throw 'Multiple profiles found. Run -List and pass -ConfigPath explicitly so the wrong profile is not modified.'
  }
  $ConfigPath = Join-Path $dirs[0].FullName 'gateway-endpoint.json'
}

$parent = Split-Path -Parent $ConfigPath
if (-not $parent) { throw 'Invalid -ConfigPath (no parent directory).' }

if ($Clear) {
  foreach ($name in @('gateway-endpoint.json', 'backend-runtime.json')) {
    $target = Join-Path $parent $name
    if (Test-Path -LiteralPath $target) {
      Remove-Item -LiteralPath $target -Force
      Write-Host "Removed $target"
    }
  }
  Write-Host 'Cloud Gateway config cleared. Restart EvoFlow to spawn the local Gateway again.'
  return
}

if (-not $BaseUrl) {
  throw 'Pass -BaseUrl https://... (or -Clear to remove, -List to inspect).'
}

$normalized = $BaseUrl.Trim().TrimEnd('/')
if ($normalized -notmatch '^https?://[^/\s]+') {
  throw "Invalid Gateway URL: $BaseUrl (expected http://host or https://host, without /api)."
}

if (-not (Test-Path -LiteralPath $parent)) {
  New-Item -ItemType Directory -Path $parent -Force | Out-Null
}

$payload = ([ordered]@{
  base_url   = $normalized
  updated_at = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
}) | ConvertTo-Json

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ConfigPath, ($payload + "`n"), $utf8NoBom)
Write-Host "Saved cloud Gateway endpoint: $normalized"
Write-Host "Config file: $ConfigPath"
Write-Host 'Restart EvoFlow. Note: EVOFLOW_GATEWAY_URL in the process environment still wins over this file.'
