$ErrorActionPreference = "Stop"

$agent = Join-Path $PSScriptRoot "..\dist\zenplus-agent.exe"
if (!(Test-Path $agent)) {
  throw "zenplus-agent.exe was not found. Run scripts\build.ps1 first."
}

& $agent uninstall-service

