#requires -RunAsAdministrator
#requires -Modules Hyper-V
param(
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$SwitchName,
    [Parameter(Mandatory=$true)][string]$VMDirectory,
    [switch]$Start
)
$ErrorActionPreference='Stop'
$manifest=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'manifest.json') -Raw | ConvertFrom-Json
foreach ($disk in @('system.vhdx','data.vhdx')) {
    $source=Join-Path $PSScriptRoot $disk
    $expected=$manifest.files.$disk.sha256
    if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) {
        throw "Disk checksum mismatch: $disk"
    }
}
if (Get-VM -Name $Name -ErrorAction SilentlyContinue) { throw 'A VM with this name already exists.' }
$null=Get-VMSwitch -Name $SwitchName -ErrorAction Stop
if (Test-Path -LiteralPath $VMDirectory) { throw 'Choose a new directory to preserve existing VM disks.' }
$directory=[IO.Path]::GetFullPath($VMDirectory)
$null=New-Item -ItemType Directory -Path $directory
foreach ($disk in @('system.vhdx','data.vhdx')) {
    Copy-Item -LiteralPath (Join-Path $PSScriptRoot $disk) -Destination (Join-Path $directory $disk)
}
$vm=New-VM -Name $Name -Generation 2 -MemoryStartupBytes 8GB -Path $directory -NoVHD -SwitchName $SwitchName
Set-VMProcessor -VM $vm -Count 4
Set-VMMemory -VM $vm -DynamicMemoryEnabled $false
Add-VMHardDiskDrive -VM $vm -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 0 -Path (Join-Path $directory 'system.vhdx')
Add-VMHardDiskDrive -VM $vm -ControllerType SCSI -ControllerNumber 0 -ControllerLocation 1 -Path (Join-Path $directory 'data.vhdx')
$system=Get-VMHardDiskDrive -VM $vm | Where-Object ControllerLocation -eq 0
Set-VMFirmware -VM $vm -EnableSecureBoot On -SecureBootTemplate MicrosoftUEFICertificateAuthority -FirstBootDevice $system
if ($Start) { Start-VM -VM $vm }
Write-Output 'Appliance created. Leave Time Synchronization enabled. Open its console for the unique admin login and HTTPS address.'
