$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

Import-Module WebAdministration

$target = 'C:\inetpub\wwwroot\ZenPlusApmDemo'
foreach ($name in @('Default.aspx', 'Api.aspx', 'web.config')) {
    if (-not (Test-Path -LiteralPath (Join-Path $target $name))) {
        throw "Missing demo application file: $name"
    }
}

if (-not (Get-WebApplication -Site 'Default Web Site' -Name 'ZenPlusApmDemo' -ErrorAction SilentlyContinue)) {
    New-WebApplication -Site 'Default Web Site' -Name 'ZenPlusApmDemo' `
        -PhysicalPath $target -ApplicationPool 'DefaultAppPool' | Out-Null
}

Restart-WebAppPool -Name 'DefaultAppPool'
Start-Sleep -Seconds 3

$results = foreach ($action in @('success', 'slow', 'dependency', 'error')) {
    try {
        $response = Invoke-WebRequest -UseBasicParsing -UseDefaultCredentials `
            -Uri "http://127.0.0.1/ZenPlusApmDemo/Api.aspx?action=$action" `
            -TimeoutSec 15
        $status = [int]$response.StatusCode
    }
    catch {
        $status = [int]$_.Exception.Response.StatusCode.value__
    }
    [pscustomobject]@{ Action = $action; Status = $status }
}

[pscustomobject]@{
    Application = 'Default Web Site/ZenPlusApmDemo'
    Pool = (Get-WebAppPoolState -Name 'DefaultAppPool').Value
    Results = @($results)
    PhysicalPath = $target
} | ConvertTo-Json -Compress -Depth 5
