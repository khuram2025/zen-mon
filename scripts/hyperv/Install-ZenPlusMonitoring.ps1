# Run locally as Administrator on each Hyper-V host after reviewing the module.
# This installs a restricted command endpoint; it does not configure listeners or firewall policy.
[CmdletBinding()]
param([Parameter(Mandatory)][string]$MonitoringPrincipal)
$ErrorActionPreference = 'Stop'
if ((Get-CimInstance Win32_ComputerSystem).DomainRole -ge 4) {
    throw 'Do not install this endpoint on a domain controller.'
}
$moduleRoot = Join-Path $env:ProgramFiles 'WindowsPowerShell\Modules\ZenPlusMonitoring'
$roleRoot = Join-Path $moduleRoot 'RoleCapabilities'
New-Item -ItemType Directory -Path $roleRoot -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'ZenPlusMonitoring.psm1') -Destination $moduleRoot -Force
New-ModuleManifest -Path (Join-Path $moduleRoot 'ZenPlusMonitoring.psd1') -RootModule 'ZenPlusMonitoring.psm1' `
    -ModuleVersion '1.0.0' -FunctionsToExport 'Get-ZenPlusHypervisorSnapshot' -CmdletsToExport @() -VariablesToExport @() -AliasesToExport @()
New-PSRoleCapabilityFile -Path (Join-Path $roleRoot 'ZenPlusReadOnly.psrc') `
    -ModulesToImport 'ZenPlusMonitoring' -VisibleFunctions 'Get-ZenPlusHypervisorSnapshot'
$configRoot = Join-Path $env:ProgramData 'ZenPlusMonitoring'
New-Item -ItemType Directory -Path $configRoot -Force | Out-Null
$roles = @{}
$roles[$MonitoringPrincipal] = @{RoleCapabilities='ZenPlusReadOnly'}
$sessionPath = Join-Path $configRoot 'ZenPlusMonitoring.pssc'
New-PSSessionConfigurationFile -Path $sessionPath -SessionType RestrictedRemoteServer `
    -RunAsVirtualAccount -RoleDefinitions $roles -TranscriptDirectory (Join-Path $configRoot 'Transcripts')
if (-not (Test-PSSessionConfigurationFile -Path $sessionPath)) { throw 'JEA configuration validation failed' }
Register-PSSessionConfiguration -Name ZenPlusMonitoring -Path $sessionPath -Force
Write-Output 'ZenPlusMonitoring installed. Configure a trusted WinRM HTTPS listener and a source-scoped TCP 5986 firewall rule.'
