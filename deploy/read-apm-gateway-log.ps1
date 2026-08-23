$ErrorActionPreference = 'Stop'
$gatewayLog = 'C:\ProgramData\ZenPlus\Agent\logs\telemetry-gateway.log'
$agentLog = 'C:\ProgramData\ZenPlus\Agent\logs\agent.log'
[pscustomobject]@{
    gateway_log_exists = Test-Path $gatewayLog
    gateway_log_tail = @(
        Get-Content $gatewayLog -Tail 25 -ErrorAction SilentlyContinue |
            ForEach-Object { ($_ -replace '[^\x09\x20-\x7E]', '') }
    )
    agent_log_tail = @(
        Get-Content $agentLog -Tail 20 -ErrorAction SilentlyContinue |
            ForEach-Object { ($_ -replace '[^\x09\x20-\x7E]', '') }
    )
} | ConvertTo-Json -Depth 4 -Compress
