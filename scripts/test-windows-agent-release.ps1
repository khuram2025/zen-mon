$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$manifestUrl = 'https://zentryc.com/downloads/zenplus-agent/stable/manifest.json'
$manifest = Invoke-RestMethod $manifestUrl
$package = Join-Path $env:TEMP $manifest.file_name

Invoke-WebRequest -UseBasicParsing -Uri $manifest.download_url -OutFile $package
$hash = (Get-FileHash -Algorithm SHA256 $package).Hash.ToLowerInvariant()
if ($hash -ne $manifest.sha256) {
    throw "Downloaded setup checksum mismatch: $hash"
}
if (-not $manifest.requires_authenticode -or $manifest.signature_status -ne 'Valid') {
    throw "Release manifest does not attest a valid Authenticode signature"
}
$signature = Get-AuthenticodeSignature -LiteralPath $package
if ($signature.Status -ne 'Valid') {
    throw "Downloaded setup is not Authenticode trusted: $($signature.Status)"
}

# Exercise a clean public installation even when the VM was used for an
# earlier release verification with the same semantic version.
$existingEntries = @(
    Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' |
        Where-Object DisplayName -eq 'ZenPlus Agent'
)
foreach ($entry in $existingEntries) {
    if ($entry.WindowsInstaller -eq 1 -and $entry.PSChildName -match '^\{[0-9A-Fa-f-]+\}$') {
        $uninstall = Start-Process msiexec.exe -ArgumentList @(
            '/x',
            $entry.PSChildName,
            '/qn',
            '/norestart'
        ) -Wait -PassThru
        if ($uninstall.ExitCode -ne 0) {
            throw "Existing MSI uninstall failed with exit code $($uninstall.ExitCode)"
        }
    }
}

$process = Start-Process -FilePath $package -ArgumentList @(
    '/machine',
    '/quiet',
    '/norestart',
    'CONTROLLER_URL="https://192.168.8.221"'
) -Wait -PassThru
if ($process.ExitCode -ne 0) {
    throw "ZenPlus setup failed with exit code $($process.ExitCode)"
}

Start-Sleep -Seconds 3
$agentExe = 'C:\Program Files\ZenPlus\Agent\zenplus-agent.exe'
$agentApp = 'C:\Program Files\ZenPlus\Agent\zenplus-agent-app.exe'
$version = & $agentExe version
$service = Get-CimInstance Win32_Service -Filter "Name='ZenPlusAgent'"
$appsEntries = @(
    Get-ItemProperty 'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*' |
        Where-Object DisplayName -eq 'ZenPlus Agent'
)
$startupShortcut = 'C:\ProgramData\Microsoft\Windows\Start Menu\Programs\Startup\ZenPlus Agent.lnk'
$manifestProbe = Invoke-RestMethod $manifestUrl
$shortcut = (New-Object -ComObject WScript.Shell).CreateShortcut($startupShortcut)

if ($version -ne $manifest.latest_version) {
    throw "Installed version $version does not match manifest version $($manifest.latest_version)"
}
if ($service.State -ne 'Running' -or $service.StartMode -ne 'Auto') {
    throw "Agent service is not running automatically"
}
if ($appsEntries.Count -ne 1 -or $appsEntries[0].Publisher -ne 'Zentryc') {
    throw "Expected one Zentryc Apps & Features entry"
}
if (-not $appsEntries[0].InstallLocation) {
    throw "Apps & Features install location is missing"
}
if (-not (Test-Path $agentApp) -or -not (Test-Path $startupShortcut)) {
    throw "Tray application or startup shortcut is missing"
}

[pscustomobject]@{
    Computer = $env:COMPUTERNAME
    Version = $version
    ServiceState = $service.State
    ServiceStartMode = $service.StartMode
    AppsEntryCount = $appsEntries.Count
    AppsVersion = $appsEntries[0].DisplayVersion
    AppsPublisher = $appsEntries[0].Publisher
    InstallLocation = $appsEntries[0].InstallLocation
    TrayAppExists = Test-Path $agentApp
    StartupShortcutExists = Test-Path $startupShortcut
    StartupShortcutTarget = $shortcut.TargetPath
    StartupShortcutArguments = $shortcut.Arguments
    PublicManifestVersion = $manifestProbe.latest_version
    PublicManifestHash = $manifestProbe.sha256
    InstalledPackageHash = $hash
    SignatureStatus = $manifestProbe.signature_status
} | ConvertTo-Json -Compress
