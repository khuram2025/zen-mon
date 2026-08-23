$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$installer = Join-Path $env:TEMP 'zenplus-agent-1.11.2.msi'
& curl.exe -k -f -sS -L -o $installer `
    'https://192.168.8.221/api/v1/agents/packages/windows/latest?arch=amd64'
if ($LASTEXITCODE -ne 0) { throw "curl download failed with $LASTEXITCODE" }

$expected = 'cfbf7080432cd9e39cd50e5cae42423f8464b598303c9478b57469c533975664'
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $installer).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw 'Agent MSI checksum mismatch' }

$arguments = @(
    '/i', "`"$installer`"", '/quiet', '/norestart',
    'CONTROLLER_URL="https://192.168.8.221"',
    'INSTALL_PROFILE="combined"',
    '/l*v', '"C:\Windows\Temp\zenplus-agent-1.11.2-install.log"'
)
$process = Start-Process msiexec.exe -ArgumentList $arguments -Wait -PassThru
if ($process.ExitCode -ne 0) {
    Get-Content -LiteralPath 'C:\Windows\Temp\zenplus-agent-1.11.2-install.log' -Tail 120 -ErrorAction SilentlyContinue
    throw "msiexec exited with $($process.ExitCode)"
}

Start-Sleep -Seconds 8
$control = 'C:\Program Files\ZenPlus\Agent\zenplus-agentctl.exe'
[pscustomobject]@{
    Version = (& $control version)
    Service = (Get-Service ZenPlusAgent).Status.ToString()
    IIS = (Get-Service W3SVC).Status.ToString()
    Gateway = (Test-NetConnection 127.0.0.1 -Port 4318 -InformationLevel Quiet)
    SHA256 = $actual
} | ConvertTo-Json -Compress
