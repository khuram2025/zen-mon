$ErrorActionPreference = 'Stop'
$agentDir = 'C:\Program Files\ZenPlus\Agent'
$configPath = 'C:\ProgramData\ZenPlus\Agent\config\agent.yaml'
$statusPath = 'C:\ProgramData\ZenPlus\Agent\state\status.json'
$service = Get-CimInstance Win32_Service -Filter "Name='ZenPlusAgent'" -ErrorAction SilentlyContinue
$version = if (Test-Path "$agentDir\zenplus-agent.exe") { & "$agentDir\zenplus-agent.exe" version } else { $null }
$status = if (Test-Path $statusPath) { Get-Content $statusPath -Raw | ConvertFrom-Json } else { $null }
$config = if (Test-Path $configPath) { Get-Content $configPath -Raw } else { '' }
$externalUrls = @()
if ($config -match '(?im)^\s*(controller_url|controller|site_id|policy_id|token|enrollment_token)\s*:') {
    $externalUrls = [regex]::Matches($config, '(?im)^\s*(controller_url|controller|site_id|policy_id|token|enrollment_token)\s*:\s*(.*)$') |
        ForEach-Object { if ($_.Groups[1].Value -match 'token') { "$($_.Groups[1].Value): <redacted>" } else { "$($_.Groups[1].Value): $($_.Groups[2].Value)" } }
}
[pscustomobject]@{
    computer = $env:COMPUTERNAME
    version = $version
    service_state = $service.State
    service_start_mode = $service.StartMode
    auth_state = $status.auth_state
    agent_id = $status.agent_id
    server_id = $status.server_id
    apm = $status.local_apm
    gateway_processes = @(Get-Process zenplus-telemetry-gateway -ErrorAction SilentlyContinue).Count
    otlp_4317 = [bool](Get-NetTCPConnection -LocalPort 4317 -State Listen -ErrorAction SilentlyContinue)
    otlp_4318 = [bool](Get-NetTCPConnection -LocalPort 4318 -State Listen -ErrorAction SilentlyContinue)
    apm_bundle_manifest = Test-Path "$agentDir\apm\bundle-manifest.json"
    dotnet_pack = Test-Path "$agentDir\apm\instrumentation\dotnet\VERSION"
    java_pack = Test-Path "$agentDir\apm\instrumentation\java\opentelemetry-javaagent.jar"
    node_pack = Test-Path "$agentDir\apm\instrumentation\node\node_modules\@opentelemetry\auto-instrumentations-node"
    python_pack = Test-Path "$agentDir\apm\instrumentation\python\wheelhouse"
    config_public_fields = $externalUrls
} | ConvertTo-Json -Depth 8 -Compress
