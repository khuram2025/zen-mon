$ErrorActionPreference = 'Stop'
$statusFile = @(
    'C:\ProgramData\ZenPlus\Agent\state\agent-status.json',
    'C:\ProgramData\ZenPlus\Agent\state\status.json',
    'C:\ProgramData\ZenPlus\Agent\config\data\state\status.json'
) | Where-Object { Test-Path $_ } | Select-Object -First 1
$status = if (Test-Path $statusFile) { Get-Content $statusFile -Raw | ConvertFrom-Json } else { $null }
$service = Get-CimInstance Win32_Service -Filter "Name='ZenPlusAgent'"
[pscustomobject]@{
    status_path = $statusFile
    service_path = $service.PathName
    program_data_directories = @(Get-ChildItem 'C:\ProgramData\ZenPlus\Agent' -Directory -ErrorAction SilentlyContinue | ForEach-Object FullName)
    state_files = @(Get-ChildItem 'C:\ProgramData\ZenPlus\Agent\state' -File -ErrorAction SilentlyContinue | ForEach-Object FullName)
    config_state_files = @(Get-ChildItem 'C:\ProgramData\ZenPlus\Agent\config\data\state' -File -ErrorAction SilentlyContinue | ForEach-Object FullName)
    property_names = @($status.PSObject.Properties.Name)
    status = $status
    agent_log_tail = @(Get-Content 'C:\ProgramData\ZenPlus\Agent\logs\agent.log' -Tail 40 -ErrorAction SilentlyContinue)
    gateway_log_tail = @(Get-Content 'C:\ProgramData\ZenPlus\Agent\logs\telemetry-gateway.log' -Tail 60 -ErrorAction SilentlyContinue)
} | ConvertTo-Json -Depth 12 -Compress
