$ErrorActionPreference = 'Stop'
$now = [DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds() * 1000000
$later = $now + 125000000
$payload = @{
    resourceSpans = @(@{
        resource = @{ attributes = @(
            @{ key = 'service.name'; value = @{ stringValue = 'zenplus-offline-apm-validation' } },
            @{ key = 'deployment.environment'; value = @{ stringValue = 'prod' } },
            @{ key = 'service.version'; value = @{ stringValue = '1.8.0' } },
            @{ key = 'test.network.mode'; value = @{ stringValue = 'appliance-lan-only' } }
        ) }
        scopeSpans = @(@{ spans = @(@{
            traceId = '6b8efff798038103d269b633813fc60d'
            spanId = 'ffe19b7ec3c1b175'
            name = 'offline-agent-validation-success'
            kind = 2
            startTimeUnixNano = [string]$now
            endTimeUnixNano = [string]$later
            status = @{ code = 1 }
        }) })
    })
} | ConvertTo-Json -Depth 12 -Compress
$response = Invoke-WebRequest -UseBasicParsing -Method Post -Uri 'http://127.0.0.1:4318/v1/traces' -ContentType 'application/json' -Body $payload -TimeoutSec 10
Start-Sleep -Seconds 6
$status = Get-Content 'C:\ProgramData\ZenPlus\Agent\state\status.json' -Raw | ConvertFrom-Json
[pscustomobject]@{
    local_otlp_status = $response.StatusCode
    agent_version = $status.agent_version
    auth_state = $status.auth_state
    apm_state = $status.local_apm.state
    gateway_healthy = $status.local_apm.gateway.healthy
    discovered = $status.local_apm.discovered
    bundles = $status.local_apm.bundles
} | ConvertTo-Json -Depth 6 -Compress
