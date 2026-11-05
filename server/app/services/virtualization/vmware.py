"""vSphere Web Services read-only inventory, QuickStats and triggered alarms."""
from urllib.parse import quote, urlsplit

from .common import ConnectorError, Snapshot, number, percent, tls_context


def attr(obj, path, default=None):
    for key in path.split("."):
        obj = getattr(obj, key, None)
        if obj is None:
            return default
    return obj


def scaled(value, scale):
    n = number(value)
    return n * scale if n is not None else None


def performance(content, objects, snapshot, vim):
    """Collect only aggregate counters; unavailable counters remain absent."""
    counters = {
        "cpu.usage.average": ("cpu_usage_percent", .01),
        "cpu.ready.summation": ("cpu_ready_ms", 1),
        "mem.usage.average": ("memory_usage_percent", .01),
        "mem.vmmemctl.average": ("memory_balloon_bytes", 1024),
        "disk.usage.average": ("io_bandwidth_bytes_per_second", 1024),
        "disk.numberReadAveraged.average": ("read_iops", 1),
        "disk.numberWriteAveraged.average": ("write_iops", 1),
        "disk.totalReadLatency.average": ("read_latency_us", 1000),
        "disk.totalWriteLatency.average": ("write_latency_us", 1000),
        "net.usage.average": ("network_bytes_per_second", 1024),
    }
    try:
        manager = content.perfManager
        selected = {}
        for counter in manager.perfCounter:
            name = f"{counter.groupInfo.key}.{counter.nameInfo.key}.{counter.rollupType}"
            if name in counters:
                selected[counter.key] = counters[name]
        if not selected:
            raise ValueError("No supported counters")
        by_id = {r["external_id"]: r for r in snapshot.resources}
        for offset in range(0, len(objects), 50):
            specs = [vim.PerformanceManager.QuerySpec(entity=obj, intervalId=20, maxSample=1,
                metricId=[vim.PerformanceManager.MetricId(counterId=cid, instance="") for cid in selected])
                for obj in objects[offset:offset+50]]
            for result in manager.QueryPerf(querySpec=specs):
                resource = by_id.get(result.entity._moId)
                if not resource:
                    continue
                for series in result.value:
                    if series.id.counterId in selected and series.value:
                        metric, factor = selected[series.id.counterId]
                        v = scaled(series.value[-1], factor)
                        if v is not None:
                            resource["metrics"][metric] = v
        snapshot.capabilities["performance_counters"] = True
    except Exception:
        snapshot.capabilities["performance_counters"] = False
        snapshot.warnings.append("Some vSphere performance counters are unavailable; inventory and QuickStats remain available")


def collect(config):
    from pyVim.connect import SmartConnect, Disconnect
    from pyVmomi import vim
    u = urlsplit(config.endpoint)
    si, view = None, None
    try:
        si = SmartConnect(host=u.hostname, port=u.port or 443, user=config.username,
                          pwd=config.secret.get_secret_value(), sslContext=tls_context(config.ca_pem),
                          httpConnectionTimeout=20)
        content = si.RetrieveContent()
        snapshot = Snapshot(version=str(content.about.fullName),
                            capabilities=dict(clusters=True, hosts=True, vms=True, storage=True, alerts=True))
        view = content.viewManager.CreateContainerView(content.rootFolder,
            [vim.ClusterComputeResource, vim.HostSystem, vim.VirtualMachine, vim.Datastore], True)
        health = {"green": "healthy", "yellow": "warning", "red": "critical", "gray": "unknown"}
        performance_objects = []
        alarm_sources = {}
        for obj in view.view:
            rid = obj._moId
            m, parent, power, attributes = {}, None, None, {}
            if isinstance(obj, vim.ClusterComputeResource):
                kind = "cluster"
                m = dict(cpu_capacity_hz=scaled(attr(obj, "summary.totalCpu"), 1000000),
                         cpu_cores=attr(obj, "summary.numCpuCores"),
                         memory_capacity_bytes=attr(obj, "summary.totalMemory"))
                datastores = {d._moId: d for d in obj.datastore or []}.values()
                capacities = [(attr(d, "summary.capacity"), attr(d, "summary.freeSpace")) for d in datastores]
                if capacities and all(a is not None and b is not None for a, b in capacities):
                    capacity = sum(a for a, _ in capacities)
                    used = sum(a - b for a, b in capacities)
                    m.update(storage_capacity_bytes=capacity, storage_used_bytes=used,
                             storage_usage_percent=percent(used, capacity))
            elif isinstance(obj, vim.HostSystem):
                kind = "host"
                parent = "cluster:" + obj.parent._moId if isinstance(obj.parent, vim.ClusterComputeResource) else None
                mhz = attr(obj, "summary.hardware.cpuMhz")
                cores = attr(obj, "summary.hardware.numCpuCores")
                capacity = mhz * cores if mhz is not None and cores is not None else None
                memory = attr(obj, "summary.hardware.memorySize")
                used_memory = scaled(attr(obj, "summary.quickStats.overallMemoryUsage"), 1024**2)
                m = dict(cpu_capacity_hz=scaled(capacity, 1000000), cpu_cores=cores,
                         cpu_usage_percent=percent(attr(obj, "summary.quickStats.overallCpuUsage"), capacity),
                         memory_capacity_bytes=memory, memory_used_bytes=used_memory,
                         memory_usage_percent=percent(used_memory, memory))
                power = str(attr(obj, "runtime.powerState", "unknown"))
                m["uptime_seconds"] = attr(obj, "summary.quickStats.uptime")
                attributes = dict(hypervisor=attr(obj, "summary.config.product.fullName"),
                                  model=attr(obj, "summary.hardware.model"), vendor=attr(obj, "summary.hardware.vendor"),
                                  cpu_model=attr(obj, "summary.hardware.cpuModel"),
                                  maintenance="yes" if attr(obj, "runtime.inMaintenanceMode") else None,
                                  connection=attr(obj, "runtime.connectionState"))
            elif isinstance(obj, vim.VirtualMachine):
                kind = "vm"
                host = attr(obj, "runtime.host")
                parent = "host:" + host._moId if host else None
                cores = attr(obj, "summary.config.numCpu")
                mhz = attr(host, "summary.hardware.cpuMhz")
                capacity = cores * mhz if cores is not None and mhz is not None else None
                memory = scaled(attr(obj, "summary.config.memorySizeMB"), 1024**2)
                used_memory = scaled(attr(obj, "summary.quickStats.guestMemoryUsage"), 1024**2)
                committed = attr(obj, "summary.storage.committed")
                uncommitted = attr(obj, "summary.storage.uncommitted")
                disk_capacity = committed + uncommitted if committed is not None and uncommitted is not None else None
                m = dict(cpu_cores=cores, cpu_capacity_hz=scaled(capacity, 1000000),
                         cpu_usage_percent=percent(attr(obj, "summary.quickStats.overallCpuUsage"), capacity),
                         memory_capacity_bytes=memory, memory_used_bytes=used_memory,
                         memory_usage_percent=percent(used_memory, memory),
                         storage_used_bytes=committed, storage_capacity_bytes=disk_capacity,
                         storage_usage_percent=percent(committed, disk_capacity))
                power = str(attr(obj, "runtime.powerState", "unknown"))
                m["uptime_seconds"] = attr(obj, "summary.quickStats.uptimeSeconds")
                addresses = [attr(obj, "guest.ipAddress")]
                for nic in attr(obj, "guest.net", []) or []:
                    addresses.extend(list(getattr(nic, "ipAddress", None) or []))
                attributes = dict(ip_addresses=[a for a in addresses if isinstance(a, str) and a and not a.startswith("fe80")],
                                  guest_os=attr(obj, "summary.config.guestFullName") or attr(obj, "guest.guestFullName"),
                                  guest_tools=attr(obj, "guest.toolsRunningStatus") or attr(obj, "summary.guest.toolsStatus"),
                                  hostname=attr(obj, "guest.hostName"), hypervisor="ESXi")
            else:
                kind = "storage"
                capacity, free = attr(obj, "summary.capacity"), attr(obj, "summary.freeSpace")
                used = capacity - free if capacity is not None and free is not None else None
                m = dict(storage_capacity_bytes=capacity, storage_used_bytes=used,
                         storage_usage_percent=percent(used, capacity))
                attributes = dict(type=attr(obj, "summary.type"))
            url = config.endpoint + "/ui/#/search?query=" + quote(str(obj.name), safe="")
            item = snapshot.resource(kind, rid, obj.name, parent_id=parent, power_state=power,
                                     health=health.get(str(obj.overallStatus), "unknown"), metrics=m, url=url,
                                     attributes=attributes)
            if kind in ("host", "vm"):
                performance_objects.append(obj)
            for alarm in obj.triggeredAlarmState or []:
                # Parent inventory objects may also expose a descendant's alarm.
                alarm_sources[str(alarm.key)] = (alarm, attr(alarm, "entity._moId", rid))
        resources_by_id = {r["external_id"]: r for r in snapshot.resources}
        for key, (alarm, entity_id) in alarm_sources.items():
            affected = resources_by_id.get(entity_id, {})
            snapshot.alerts.append(dict(id=key, title=str(alarm.alarm.info.name),
                severity=health.get(str(alarm.overallStatus), "unknown"), resolved=False,
                acknowledged=bool(alarm.acknowledged), resource_id=affected.get("id"),
                resource_url=affected.get("url"), url=affected.get("url")))
        performance(content, performance_objects, snapshot, vim)
        # Cluster usage is an aggregate of member host usage, never a sum of percentages.
        for c in (r for r in snapshot.resources if r["kind"] == "cluster"):
            hosts = [r["metrics"] for r in snapshot.resources if r["parent_id"] == c["id"] and r["kind"] == "host"]
            for name in ("cpu", "memory"):
                capacity_key = name + ("_capacity_hz" if name == "cpu" else "_capacity_bytes")
                if hosts and all(capacity_key in h and name + "_usage_percent" in h for h in hosts):
                    total = sum(h[capacity_key] for h in hosts)
                    used = sum(h[capacity_key] * h[name + "_usage_percent"] / 100 for h in hosts)
                    c["metrics"][name + "_usage_percent"] = percent(used, total)
        return snapshot
    except ConnectorError:
        raise
    except Exception:
        raise ConnectorError("vSphere collection failed; check TLS trust, read-only inventory permissions and API availability") from None
    finally:
        if view:
            try:
                view.Destroy()
            except Exception:
                pass
        if si:
            try:
                Disconnect(si)
            except Exception:
                pass
