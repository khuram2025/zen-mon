"""Documented Prism Central GA v4 and Prism Element v2 read APIs."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import httpx

from .common import ConnectionConfig, ConnectorError, Snapshot, number, percent, tls_context


class Unsupported(ConnectorError):
    pass


def latest(value):
    # v4 stats are arrays of timestamp/value samples; missing (-1) is not zero.
    if isinstance(value, list):
        values = [v for v in value if isinstance(v, dict) and number(v.get("value")) is not None]
        return number(max(values, key=lambda x: str(x.get("timestamp", "")))["value"]) if values else None
    return number(value)


def metrics(data, config=None):
    config = config or {}
    if isinstance(data.get("stats"), list):
        data = max(data["stats"], key=lambda x: str(x.get("timestamp", "")), default={})
    data = dict(data)
    for dest, aliases in {
        "hypervisorMemoryUsagePpm": ("aggregateHypervisorMemoryUsagePpm", "aggregate_hypervisor_memory_usage_ppm", "memoryUsagePpm"),
        "controllerIoBandwidthKbps": ("ioBandwidthKbps", "controllerIoBandwidthkBps"),
        "controllerAvgIoLatencyUsecs": ("controllerAvgIoLatencyMicros", "controllerAvgIoLatencyuSecs"),
        "storageCapacityBytes": ("diskCapacityBytes",),
    }.items():
        if dest not in data:
            for alias in aliases:
                if alias in data:
                    data[dest] = data[alias]
                    break
    out = {}
    mapping = {
        "cpu_usage_percent": ("hypervisorCpuUsagePpm", "hypervisor_cpu_usage_ppm", 0.0001),
        "memory_usage_percent": ("hypervisorMemoryUsagePpm", "hypervisor_memory_usage_ppm", 0.0001),
        "storage_capacity_bytes": ("storageCapacityBytes", "storage.capacity_bytes", 1),
        "storage_used_bytes": ("storageUsageBytes", "storage.usage_bytes", 1),
        "iops": ("controllerNumIops", "controller_num_iops", 1),
        "io_bandwidth_bytes_per_second": ("controllerIoBandwidthKbps", "controller_io_bandwidth_kBps", 1024),
        "io_latency_us": ("controllerAvgIoLatencyUsecs", "controller_avg_io_latency_usecs", 1),
        "cpu_capacity_hz": ("cpuCapacityHz", "cpu_capacity_in_hz", 1),
        "cpu_used_hz": ("cpuUsageHz", "cpu_usage_hz", 1),
        "memory_capacity_bytes": ("memoryCapacityBytes", "memory_capacity_in_bytes", 1),
        "memory_used_bytes": ("overallMemoryUsageBytes", "memoryUsageBytes", 1),
        "health_score": ("healthCheckScore", "checkScore", 1),
        "cpu_ready_percent": ("hypervisorCpuReadyTimePpm", "hypervisor_cpu_ready_time_ppm", .0001),
        "network_received_bytes": ("hypervisorNumReceivedBytes", "hypervisor_num_received_bytes", 1),
        "network_transmitted_bytes": ("hypervisorNumTransmittedBytes", "hypervisor_num_transmitted_bytes", 1),
    }
    for dest, (v4, v2, scale) in mapping.items():
        v = latest(data.get(v4, data.get(v2)))
        if v is not None:
            out[dest] = v * scale
    for dest, keys in {
        "memory_capacity_bytes": ("memorySizeBytes", "memoryCapacityInBytes", "memory_capacity_in_bytes"),
        "cpu_capacity_hz": ("cpuCapacityHz", "cpu_capacity_in_hz"),
        "cpu_cores": ("numberOfCpuCores", "numCpuCores", "num_cpu_cores"),
    }.items():
        for key in keys:
            v = number(config.get(key))
            if v is not None:
                out[dest] = v
                break
    if number(config.get("memory_mb")) is not None:
        out["memory_capacity_bytes"] = number(config["memory_mb"]) * 1024**2
    if "memory_capacity_bytes" in out and "memory_usage_percent" in out:
        out["memory_used_bytes"] = out["memory_capacity_bytes"] * out["memory_usage_percent"] / 100
    p = percent(out.get("storage_used_bytes"), out.get("storage_capacity_bytes"))
    if p is not None:
        out["storage_usage_percent"] = p
    if "diskUsagePpm" in data:
        p = latest(data["diskUsagePpm"])
        if p is not None:
            out["storage_usage_percent"] = p / 10000
            if "storage_capacity_bytes" in out:
                out["storage_used_bytes"] = out["storage_capacity_bytes"] * p / 1000000
    return out


class PrismCollector:
    def __init__(self, config: ConnectionConfig, client=None):
        self.config = config
        self.client = client
        self.snapshot = Snapshot(version="Prism Element v2.0" if config.provider == "nutanix_pe" else config.api_version)

    async def get(self, path, params=None):
        for attempt in range(3):
            try:
                response = await self.client.get(path, params=params)
            except httpx.TimeoutException:
                raise ConnectorError("Prism request timed out; check routing and TCP 9440 policy") from None
            except httpx.TransportError:
                raise ConnectorError("Prism TLS or transport failed; verify certificate trust, hostname and routing") from None
            if response.status_code in (429, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            if response.status_code == 401:
                raise ConnectorError("Prism rejected the monitoring credential (401)")
            if response.status_code == 403:
                raise ConnectorError("Prism denied read access (403); check monitoring role and entity scope")
            if response.status_code in (404, 405, 501):
                raise Unsupported("API capability is unavailable on this Prism version")
            if response.status_code != 200:
                raise ConnectorError(f"Prism returned HTTP {response.status_code}")
            try:
                value = response.json()
                if not isinstance(value, dict):
                    raise ValueError()
                return value
            except ValueError:
                raise ConnectorError("Prism returned an invalid JSON API response") from None

    async def pages(self, path, *, v2=False, extra_params=None):
        result, seen = [], set()
        for page in range(1000):
            params = {"count": 100, "page": page + 1} if v2 else {"$limit": 100, "$page": page}
            if v2 and path.endswith("/vms"):
                params = {"length": 100, "offset": len(result)}
            params.update(extra_params or {})
            body = await self.get(path, params)
            key = "entities" if v2 else "data"
            rows = body.get(key)
            if not isinstance(rows, list):
                raise ConnectorError("Prism returned an invalid inventory page")
            for row in rows:
                rid = row.get("uuid") or row.get("extId") or row.get("id")
                if not rid or str(rid) in seen:
                    raise ConnectorError("Prism pagination returned missing or duplicate identifiers")
                seen.add(str(rid))
            result.extend(rows)
            total = body.get("metadata", {}).get("total_entities" if v2 else "totalAvailableResults")
            if total is not None and len(result) >= int(total):
                return result
            if not rows or (total is None and len(rows) < 100):
                if total is not None and len(result) < int(total):
                    raise ConnectorError("Prism inventory ended before all resources were returned")
                return result
        raise ConnectorError("Prism inventory exceeded the bounded pagination limit")

    async def optional_pages(self, path, capability, *, v2=False, extra_params=None):
        try:
            value = await self.pages(path, v2=v2, extra_params=extra_params)
            self.snapshot.capabilities[capability] = True
            return value
        except Unsupported:
            self.snapshot.capabilities[capability] = False
            self.snapshot.warnings.append(f"{capability} is unavailable on the selected API version")
            return []

    async def stats(self, path, capability):
        if self.snapshot.capabilities.get(capability) is False:
            return {}
        end = datetime.now(timezone.utc)
        try:
            body = await self.get(path, {"$startTime": (end-timedelta(minutes=10)).isoformat(),
                                         "$endTime": end.isoformat(), "$samplingInterval": 60})
            data = body.get("data", {})
            if isinstance(data, list):
                data = data[-1] if data else {}
            if not isinstance(data, dict):
                raise ConnectorError("Prism returned an invalid statistics response")
            self.snapshot.capabilities[capability] = True
            return data
        except Unsupported:
            self.snapshot.capabilities[capability] = False
            self.snapshot.warnings.append(f"{capability} is unavailable; metrics remain unknown")
            return {}

    def link(self, kind, rid):
        # API permalinks are stable and point to the affected resource; no invented UI hash routes.
        return self.config.endpoint + kind + "/" + quote(str(rid), safe="")

    def add_alerts(self, rows, path, *, v2=False):
        for a in rows:
            rid = a.get("uuid") or a.get("extId") or a.get("id")
            source = a.get("sourceEntity") or {}
            affected = a.get("affected_entities") or []
            source_id = source.get("extId") or source.get("entityId") or (affected[0].get("uuid") if affected else None)
            resource = next((r for r in self.snapshot.resources if r["external_id"] == source_id), None)
            severity = str(a.get("severity", "unknown")).lower()
            resolved = a.get("resolved", a.get("isResolved", False))
            self.snapshot.alerts.append(dict(
                id=str(rid), severity=severity, resolved=bool(resolved),
                acknowledged=bool(a.get("acknowledged", a.get("isAcknowledged", False))),
                title=str(a.get("title") or a.get("alert_title") or a.get("message") or "Prism alert"),
                resource_id=resource["id"] if resource else None,
                resource_url=resource["url"] if resource else None,
                url=self.link(path, rid),
            ))
            if resource and not resolved and severity in ("critical", "warning"):
                if resource["health"] != "critical":
                    resource["health"] = severity

    async def central(self):
        v = self.config.api_version
        base = f"/api/clustermgmt/{v}"
        clusters = await self.pages(base + "/config/clusters")
        self.snapshot.capabilities["clusters"] = True
        for c in clusters:
            cid = c["extId"]
            cid_url = quote(cid, safe="")
            stat = await self.stats(f"{base}/stats/clusters/{cid_url}", "cluster_metrics")
            self.snapshot.resource("cluster", cid, c.get("name"), metrics=metrics(stat, c),
                                   url=self.link(base + "/config/clusters", cid))
            hosts = await self.pages(f"{base}/config/clusters/{cid_url}/hosts")
            self.snapshot.capabilities["hosts"] = True
            for h in hosts:
                hid = h["extId"]
                stat = await self.stats(f"{base}/stats/clusters/{cid_url}/hosts/{quote(hid, safe='')}", "host_metrics")
                self.snapshot.resource("host", hid, h.get("hostName"), parent_id=f"cluster:{cid}",
                                       health="warning" if h.get("isDegraded") else ("healthy" if h.get("isDegraded") is False else "unknown"),
                                       metrics=metrics(stat, h), url=self.link(f"{base}/config/clusters/{cid_url}/hosts", hid))
        for hypervisor in ("ahv", "esxi"):
            vm_base = f"/api/vmm/{v}/{hypervisor}"
            vms = await self.optional_pages(vm_base + "/config/vms", hypervisor + "_vms")
            vm_stats = {}
            if vms:
                end = datetime.now(timezone.utc)
                stats = await self.optional_pages(vm_base + "/stats/vms", hypervisor + "_vm_metrics",
                    extra_params={"$startTime": (end-timedelta(minutes=10)).isoformat(),
                                  "$endTime": end.isoformat(), "$samplingInterval": 60})
                vm_stats = {s["extId"]: s for s in stats}
            for vm in vms:
                vid = vm["extId"]
                stat = vm_stats.get(vid, {})
                parent = (vm.get("host") or {}).get("extId")
                cluster = (vm.get("cluster") or {}).get("extId")
                m = metrics(stat, vm)
                if number(vm.get("numSockets")) is not None and number(vm.get("numCoresPerSocket")) is not None:
                    m["cpu_cores"] = vm["numSockets"] * vm["numCoresPerSocket"]
                self.snapshot.resource("vm", vid, vm.get("name"),
                                       parent_id=f"host:{parent}" if parent else (f"cluster:{cluster}" if cluster else None),
                                       power_state=vm.get("powerState"), metrics=m,
                                       url=self.link(vm_base + "/config/vms", vid))
        for container in await self.optional_pages(base + "/config/storage-containers", "storage"):
            rid = container["extId"]
            stat = await self.stats(base + "/stats/storage-containers/" + quote(rid, safe=""), "storage_metrics")
            cid = container.get("clusterExtId")
            self.snapshot.resource("storage", rid, container.get("name"),
                parent_id=f"cluster:{cid}" if cid else None, metrics=metrics(stat, container),
                url=self.link(base + "/config/storage-containers", rid))
        alert_path = f"/api/monitoring/{v}/serviceability/alerts"
        self.add_alerts(await self.optional_pages(alert_path, "alerts"), alert_path)

    async def element(self):
        base = "/PrismGateway/services/rest/v2.0"
        cluster_id = None
        for collection, kind in (("clusters", "cluster"), ("hosts", "host"), ("vms", "vm"), ("storage_containers", "storage")):
            rows = await self.pages(base + "/" + collection, v2=True)
            self.snapshot.capabilities[collection] = True
            for row in rows:
                rid = row["uuid"]
                if kind == "cluster":
                    cluster_id = rid
                    self.snapshot.version = f"Prism Element {row.get('version', 'unknown AOS')} / API v2.0"
                m = metrics({**row.get("stats", {}), **row.get("usage_stats", {})}, row)
                if kind == "vm" and number(row.get("num_vcpus")) is not None:
                    m["cpu_cores"] = row["num_vcpus"] * (number(row.get("num_cores_per_vcpu")) or 1)
                host = row.get("host_uuid")
                self.snapshot.resource(kind, rid, row.get("name"),
                    parent_id=f"host:{host}" if host else (f"cluster:{cluster_id}" if kind != "cluster" and cluster_id else None),
                    health={"Good": "healthy", "Critical": "critical", "Warning": "warning", "Error": "critical"}.get(
                        (row.get("health_summary") or {}).get("health_status"), "warning" if row.get("is_degraded") else "unknown"),
                    metrics=m, power_state=row.get("power_state"), url=self.link(base + "/" + collection, rid))
        path = base + "/alerts"
        self.add_alerts(await self.pages(path, v2=True), path, v2=True)
        self.snapshot.capabilities["alerts"] = True
        self.snapshot.warnings.append("Prism Element v2 may omit VM performance counters; use Prism Central v4 for AHV VM statistics. Missing counters are unknown.")

    async def collect(self):
        if self.client is None:
            auth = None if self.config.auth_type == "api_key" else (self.config.username, self.config.secret.get_secret_value())
            headers = {"Accept": "application/json"}
            if self.config.auth_type == "api_key":
                headers["X-Ntnx-Api-Key"] = self.config.secret.get_secret_value()
            async with httpx.AsyncClient(base_url=self.config.endpoint, auth=auth, headers=headers,
                    verify=tls_context(self.config.ca_pem), timeout=20, follow_redirects=False,
                    trust_env=False, limits=httpx.Limits(max_connections=4)) as client:
                self.client = client
                return await self.collect()
        if self.config.provider == "nutanix_pe":
            await self.element()
        else:
            await self.central()
        if not self.snapshot.resources:
            self.snapshot.warnings.append("No resources were visible to this credential; verify the selected endpoint and entity scope")
        return self.snapshot
