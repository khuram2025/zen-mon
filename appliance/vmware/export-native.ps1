# Package verified, powered-off and sealed disks with VMware's native converter.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$SealedVmx,
    [Parameter(Mandatory=$true)][string]$OutputOva,
    [Parameter(Mandatory=$true)][string]$Version,
    [string]$OvfTool = "${env:ProgramFiles(x86)}/VMware/VMware Workstation/OVFTool/ovftool.exe"
)
$ErrorActionPreference = 'Stop'
if (!(Test-Path -LiteralPath $OvfTool -PathType Leaf)) { throw 'VMware OVF Tool is required.' }
$vmx = (Resolve-Path -LiteralPath $SealedVmx).Path
$output = [IO.Path]::GetFullPath($OutputOva)
if (Test-Path -LiteralPath $output) { throw 'Refusing to replace an existing OVA.' }
if (Test-Path -LiteralPath ($vmx + '.lck')) { throw 'Source VM must be powered off.' }
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw 'Invalid release version.' }
$settings = Get-Content -LiteralPath $vmx -Raw
foreach ($required in @('firmware\s*=\s*"efi"', 'uefi.secureBoot.enabled\s*=\s*"TRUE"', 'memsize\s*=\s*"8192"', 'numvcpus\s*=\s*"4"')) {
    if ($settings -notmatch $required) { throw ('Missing appliance hardware setting: ' + $required) }
}
if ($settings -match '(?im)^ethernet[1-9]\d*\.present\s*=\s*"TRUE"' -or $settings -match '(?im)^ethernet\d+\.address\s*=') {
    throw 'Use one management NIC without a fixed MAC address.'
}
if ($settings -match '(?im)^(sata\d+:\d+|ide\d+:\d+)\.present\s*=\s*"TRUE"') { throw 'Remove seed and installation media before exporting.' }
New-Item -ItemType Directory -Force ([IO.Path]::GetDirectoryName($output)) | Out-Null
$annotation = "Zentryc LLC ZenPlus $Version. 4 vCPU, 8 GiB RAM, 40 + 100 GiB thin storage, UEFI Secure Boot. Connect the management NIC to DHCP, then follow the local Zentryc console. Unique administrator claim and 30-day trial begin at first boot. https://zentryc.com/products/"
& $OvfTool '--shaAlgorithm=SHA256' '--noNvramFile' "--annotation=$annotation" $vmx $output
if ($LASTEXITCODE -ne 0) { throw 'VMware export failed; do not release this artifact.' }
$hash = (Get-FileHash -LiteralPath $output -Algorithm SHA256).Hash.ToLowerInvariant()
[IO.File]::WriteAllText($output + '.sha256', "$hash  $([IO.Path]::GetFileName($output))`n", [Text.UTF8Encoding]::new($false))
Write-Output "Exported $output"
Write-Output "SHA256 $hash"
# A successful export is insufficient: strictly import this exact artifact twice,
# without --skipManifestCheck or --lax, and complete offline first-boot acceptance.
