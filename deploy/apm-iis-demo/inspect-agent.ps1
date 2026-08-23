$ErrorActionPreference = 'Continue'
$agentPath = 'C:\Program Files\ZenPlus\Agent\zenplus-agent.exe'
Get-Service -Name 'ZenPlusAgent' -ErrorAction SilentlyContinue |
    Select-Object Status, Name, StartType
if (Test-Path -LiteralPath $agentPath) {
    & $agentPath version
    Get-Item -LiteralPath $agentPath |
        Select-Object FullName, Length, LastWriteTimeUtc
} else {
    Write-Output 'AGENT_BINARY_MISSING'
}
