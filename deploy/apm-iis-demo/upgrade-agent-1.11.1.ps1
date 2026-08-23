$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$installer = Join-Path $env:TEMP 'zenplus-agent-1.11.1.exe'
& curl.exe -k -f -sS -L -o $installer `
    'https://192.168.8.221/downloads/zenplus-agent-1.11.1.exe'
if ($LASTEXITCODE -ne 0) {
    throw "curl download failed with $LASTEXITCODE"
}

$expected = 'b7a660d6883718584ff757448c45fd7109bee2fd004656c19db145e1e46b1b63'
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
if ($actual -ne $expected) {
    throw 'Agent EXE checksum mismatch'
}

$arguments = @(
    '/machine', '/quiet', '/norestart',
    'CONTROLLER_URL="https://192.168.8.221"',
    'INSTALL_PROFILE="combined"'
)
& iisreset.exe /stop | Write-Output
try {
    & $installer @arguments 2>&1 | ForEach-Object { Write-Output $_ }
    $installerExitCode = $LASTEXITCODE
    if ($installerExitCode -ne 0) {
        throw "standalone installer exited with $installerExitCode"
    }
} finally {
    & iisreset.exe /start | Write-Output
}

Start-Sleep -Seconds 8
$control = 'C:\Program Files\ZenPlus\Agent\zenplus-agentctl.exe'
[pscustomobject]@{
    Version = (& $control version)
    Service = (Get-Service ZenPlusAgent).Status.ToString()
    Gateway = (Test-NetConnection 127.0.0.1 -Port 4318 -InformationLevel Quiet)
    SHA256 = $actual
} | ConvertTo-Json -Compress
