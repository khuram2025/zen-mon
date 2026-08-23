$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$controller = 'http://192.168.8.221'
$expectedSha = '631be21679787965abb392f482290103a20349d7e4344e145aa61d84b5821d69'
$package = Join-Path $env:TEMP 'zenplus-agent-1.8.0-offline.exe'

# The only package source is the LAN appliance. No public release URL, package
# manager, or runtime download is used on the endpoint.
Invoke-WebRequest -UseBasicParsing -Uri "$controller/api/v1/agents/packages/windows/latest?arch=amd64" -OutFile $package
$actualSha = (Get-FileHash -Algorithm SHA256 -LiteralPath $package).Hash.ToLowerInvariant()
if ($actualSha -ne $expectedSha) {
    throw "Appliance package checksum mismatch: $actualSha"
}

$install = Start-Process -FilePath $package -ArgumentList @(
    '/machine',
    '/quiet',
    '/norestart',
    'CONTROLLER_URL="http://192.168.8.221"',
    'INSTALL_PROFILE="combined"'
) -Wait -PassThru
if ($install.ExitCode -ne 0) {
    throw "ZenPlus Agent setup failed with exit code $($install.ExitCode)"
}

$agentDir = 'C:\Program Files\ZenPlus\Agent'
$statusPath = 'C:\ProgramData\ZenPlus\Agent\state\status.json'
$deadline = (Get-Date).AddMinutes(2)
$status = $null
do {
    Start-Sleep -Seconds 3
    if (Test-Path $statusPath) {
        try { $status = Get-Content $statusPath -Raw | ConvertFrom-Json } catch { $status = $null }
    }
    $gateway = Get-Process zenplus-telemetry-gateway -ErrorAction SilentlyContinue
} while ((-not $gateway -or -not $status.local_apm.gateway.healthy) -and (Get-Date) -lt $deadline)

$service = Get-CimInstance Win32_Service -Filter "Name='ZenPlusAgent'"
$version = & "$agentDir\zenplus-agent.exe" version
$health = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:13133/' -TimeoutSec 5

$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000
$later = $now + 100000000
$payload = @{
    resourceSpans = @(@{
        resource = @{ attributes = @(
            @{ key = 'service.name'; value = @{ stringValue = 'zenplus-offline-apm-validation' } },
            @{ key = 'deployment.environment'; value = @{ stringValue = 'prod' } },
            @{ key = 'test.network.mode'; value = @{ stringValue = 'appliance-lan-only' } }
        ) }
        scopeSpans = @(@{ spans = @(@{
            traceId = '5b8efff798038103d269b633813fc60c'
            spanId = 'eee19b7ec3c1b174'
            name = 'offline-agent-validation'
            kind = 2
            startTimeUnixNano = [string]$now
            endTimeUnixNano = [string]$later
            status = @{ code = 1 }
        }) })
    })
} | ConvertTo-Json -Depth 12 -Compress
$otlp = Invoke-WebRequest -UseBasicParsing -Method Post -Uri 'http://127.0.0.1:4318/v1/traces' -ContentType 'application/json' -Body $payload -TimeoutSec 10

[pscustomobject]@{
    computer = $env:COMPUTERNAME
    version = $version
    package_source = 'ZenPlus appliance LAN'
    package_sha256 = $actualSha
    install_profile = 'combined'
    service_state = $service.State
    service_start_mode = $service.StartMode
    auth_state = $status.auth_state
    agent_id = $status.agent_id
    server_id = $status.server_id
    apm_state = $status.local_apm.state
    apm_profile = $status.local_apm.profile
    gateway_managed = $status.local_apm.gateway.managed
    gateway_healthy = $status.local_apm.gateway.healthy
    gateway_version = $status.local_apm.gateway.version
    gateway_health_http = $health.StatusCode
    local_otlp_http = $otlp.StatusCode
    bundles = $status.local_apm.bundles
    bundle_manifest = Test-Path "$agentDir\apm\bundle-manifest.json"
    dotnet_pack = Test-Path "$agentDir\apm\instrumentation\dotnet\VERSION"
    java_pack = Test-Path "$agentDir\apm\instrumentation\java\opentelemetry-javaagent.jar"
    node_pack = Test-Path "$agentDir\apm\instrumentation\node\node_modules\@opentelemetry\auto-instrumentations-node"
    python_pack = Test-Path "$agentDir\apm\instrumentation\python\wheelhouse"
} | ConvertTo-Json -Depth 8 -Compress
