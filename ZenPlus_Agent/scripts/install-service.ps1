param(
  [string]$Config = "$PSScriptRoot\..\config\agent.yaml"
)

$ErrorActionPreference = "Stop"
$agent = Join-Path $PSScriptRoot "..\dist\zenplus-agent.exe"
if (!(Test-Path $agent)) {
  throw "zenplus-agent.exe was not found. Run scripts\build.ps1 first."
}

& $agent install-service --config (Resolve-Path $Config).Path

