function Get-ZenPlusHypervisorSnapshot {
    [CmdletBinding()]
    param()
    $ErrorActionPreference = 'Stop'
    $resources = [System.Collections.Generic.List[object]]::new()
    $alerts = [System.Collections.Generic.List[object]]::new()
    $warnings = [System.Collections.Generic.List[string]]::new()
    $computer = Get-CimInstance -ClassName Win32_ComputerSystem
    $os = Get-CimInstance -ClassName Win32_OperatingSystem
    $hostId = (Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID
    $processors = @(Get-CimInstance -ClassName Win32_Processor)
    $cpuHz = ($processors | ForEach-Object { [double]$_.MaxClockSpeed * $_.NumberOfCores * 1000000 } | Measure-Object -Sum).Sum
    $memory = [double]$computer.TotalPhysicalMemory
    $usedMemory = $memory - ([double]$os.FreePhysicalMemory * 1024)
    $hostMetrics = @{
        cpu_capacity_hz = $cpuHz
        cpu_cores = ($processors | Measure-Object -Property NumberOfCores -Sum).Sum
        memory_capacity_bytes = $memory
        memory_used_bytes = $usedMemory
        memory_usage_percent = 100 * $usedMemory / $memory
    }
    try {
        $cpu = Get-CimInstance -ClassName Win32_PerfFormattedData_HvStats_HyperVHypervisorLogicalProcessor |
            Where-Object Name -eq '_Total'
        if ($null -ne $cpu) { $hostMetrics.cpu_usage_percent = [double]$cpu.PercentTotalRunTime }
    } catch { $warnings.Add('Host hypervisor CPU counters are unavailable') }
    $clusterId = $null
    if (Get-Service -Name ClusSvc -ErrorAction SilentlyContinue) {
        try {
            $cluster = Get-Cluster
            $clusterId = [string]$cluster.Id
            $nodes = @(Get-ClusterNode)
            $clusterHealth = if (@($nodes | Where-Object State -ne 'Up').Count) { 'warning' } else { 'healthy' }
            $resources.Add(@{kind='cluster'; id=$clusterId; name=$cluster.Name; health=$clusterHealth; metrics=@{}})
        } catch { $warnings.Add('Failover cluster inventory is unavailable') }
    }
    $hostState = Get-CimInstance -Namespace 'root/virtualization/v2' -ClassName Msvm_ComputerSystem |
        Where-Object Name -eq $computer.Name | Select-Object -First 1
    $hostHealth = switch ($hostState.HealthState) { 5 {'healthy'} 10 {'warning'} 20 {'critical'} 25 {'critical'} 30 {'critical'} default {'unknown'} }
    $resources.Add(@{kind='host'; id=$hostId; name=$computer.Name; health=$hostHealth;
        parent_id=$(if ($clusterId) { "cluster:$clusterId" } else { $null }); metrics=$hostMetrics})
    foreach ($vm in @(Get-VM)) {
        $vmId = [string]$vm.Id
        $health = if ($vm.PrimaryOperationalStatus -eq 'Ok') { 'healthy' } else { 'warning' }
        $vmMetrics = @{
            cpu_cores = [int]$vm.ProcessorCount
            memory_assigned_bytes = [double]$vm.MemoryAssigned
            memory_demand_bytes = [double]$vm.MemoryDemand
            memory_capacity_bytes = [double]$(if ($vm.DynamicMemoryEnabled) { $vm.MemoryMaximum } else { $vm.MemoryStartup })
            uptime_seconds = [double]$vm.Uptime.TotalSeconds
        }
        if ($vm.State -eq 'Running') { $vmMetrics.cpu_usage_percent = [double]$vm.CPUUsage }
        if ($vm.MemoryAssigned -gt 0) { $vmMetrics.memory_demand_percent = 100 * $vm.MemoryDemand / $vm.MemoryAssigned }
        try {
            $disks = @($vm | Get-VMHardDiskDrive | Where-Object Path | ForEach-Object { Get-VHD -Path $_.Path })
            if ($disks.Count) {
                $vmMetrics.storage_capacity_bytes = ($disks | Measure-Object -Property Size -Sum).Sum
                $vmMetrics.storage_used_bytes = ($disks | Measure-Object -Property FileSize -Sum).Sum
                if ($vmMetrics.storage_capacity_bytes -gt 0) {
                    $vmMetrics.storage_usage_percent = 100 * $vmMetrics.storage_used_bytes / $vmMetrics.storage_capacity_bytes
                }
            }
        } catch { $warnings.Add("Virtual disk allocation unavailable for VM $vmId") }
        $resources.Add(@{kind='vm'; id=$vmId; name=$vm.Name; parent_id="host:$hostId";
            health=$health; power_state=[string]$vm.State; metrics=$vmMetrics})
        if ($health -ne 'healthy') {
            $alerts.Add(@{id="vm-health:$vmId"; title="Hyper-V VM $($vm.Name): $($vm.Status)";
                severity='warning'; resolved=$false; acknowledged=$false; resource_id="vm:$vmId"})
        }
    }
    foreach ($volume in @(Get-CimInstance -ClassName Win32_Volume -Filter 'DriveType=3')) {
        if ($volume.Capacity -gt 0) {
            $resources.Add(@{kind='storage'; id=$volume.DeviceID; name=$volume.Name; parent_id="host:$hostId";
                health='unknown'; metrics=@{storage_capacity_bytes=[double]$volume.Capacity;
                    storage_used_bytes=[double]($volume.Capacity - $volume.FreeSpace);
                    storage_usage_percent=100 * ($volume.Capacity - $volume.FreeSpace) / $volume.Capacity}})
        }
    }
    # Local event records are a 24-hour event window, not acknowledged/resolved Prism-style alarms.
    try {
        $eventErrors = @()
        $events = @(Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Hyper-V-VMMS-Admin';
            Level=1,2,3; StartTime=(Get-Date).AddHours(-24)} -MaxEvents 500 -ErrorAction SilentlyContinue -ErrorVariable eventErrors)
        if (@($eventErrors | Where-Object FullyQualifiedErrorId -NotLike 'NoMatchingEventsFound*').Count) {
            $warnings.Add('Hyper-V VMMS event log could not be read completely')
        }
        foreach ($event in $events) {
            $alerts.Add(@{id="vmms:$hostId`:$($event.RecordId)"; title=[string]$event.Message;
                severity=$(if ($event.Level -le 2) {'critical'} else {'warning'});
                resolved=$false; acknowledged=$false; resource_id="host:$hostId";
                created_at=$event.TimeCreated.ToUniversalTime().ToString('o')})
        }
        if ($events.Count -eq 500) { $warnings.Add('VMMS events are capped at the newest 500 records in 24 hours') }
    } catch { $warnings.Add('Hyper-V VMMS event log is unavailable') }
    @{
        schema_version=1; version="Hyper-V / $($os.Caption) $($os.Version)"
        resources=@($resources.ToArray()); alerts=@($alerts.ToArray()); warnings=@($warnings.ToArray())
        capabilities=@{hosts=$true; vms=$true; storage=$true; clusters=($null -ne $clusterId);
            alerts=$true; guest_memory_usage=$false; event_window_hours=24}
    } | ConvertTo-Json -Depth 12 -Compress
}
Export-ModuleMember -Function Get-ZenPlusHypervisorSnapshot
