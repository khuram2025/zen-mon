param(
    [Parameter(Mandatory=$true)][string]$Disk,
    [Parameter(Mandatory=$true)][string]$Ovf,
    [Parameter(Mandatory=$true)][string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$vmwareDirectory = 'C:\Program Files (x86)\VMware\VMware Workstation'
$taskOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $taskOutput | Out-Null
$taskDisk = Join-Path $taskOutput 'zenplus-sensor-disk1.vmdk'
if (Test-Path -LiteralPath $taskDisk) { throw 'Use a fresh output directory.' }
# Normalize QEMU stream output with VMware's own writer. Some Workstation
# versions stop hashing QEMU output before its trailing sparse-disk padding.
& "$vmwareDirectory\vmware-vdiskmanager.exe" -r $Disk -t 5 $taskDisk
if ($LASTEXITCODE -ne 0) { throw 'VMware disk normalization failed.' }
$taskOvf = Join-Path $taskOutput 'zenplus-sensor.ovf'
$descriptor = [xml](Get-Content -LiteralPath $Ovf -Raw)
$ns = 'http://schemas.dmtf.org/ovf/envelope/1'
$file = $descriptor.GetElementsByTagName('File', $ns)[0]
$file.SetAttribute('href', $ns, 'zenplus-sensor-disk1.vmdk')
$file.SetAttribute('size', $ns, [string](Get-Item -LiteralPath $taskDisk).Length)
$descriptor.Save($taskOvf)
$lines = foreach ($path in @($taskOvf, $taskDisk)) {
    'SHA256(' + [System.IO.Path]::GetFileName($path) + ')= ' + (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
}
[System.IO.File]::WriteAllLines((Join-Path $taskOutput 'zenplus-sensor.mf'), $lines, [System.Text.UTF8Encoding]::new($false))
& tar -cf (Join-Path $taskOutput 'zenplus-sensor.ova') -C $taskOutput zenplus-sensor.ovf zenplus-sensor.mf zenplus-sensor-disk1.vmdk
if ($LASTEXITCODE -ne 0) { throw 'OVA packaging failed.' }
& "$vmwareDirectory\OVFTool\ovftool.exe" --verifyOnly (Join-Path $taskOutput 'zenplus-sensor.ova')
if ($LASTEXITCODE -ne 0) { throw 'OVA validation failed.' }
Get-FileHash -LiteralPath (Join-Path $taskOutput 'zenplus-sensor.ova') -Algorithm SHA256
