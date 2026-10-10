"""Disk and volume layout for Settings -> Storage -> Disks & Volumes.

Facts come from the root-owned volume helper
(/usr/local/sbin/zenplus-volume-helper, see scripts/zenplus-volume-helper);
``build_layout`` turns them into the model the dashboard renders: every disk
with its role, the volume group behind /data, the logical volumes mounted at
/ and /data, space that can be claimed after a virtual disk was enlarged,
missing disks, data moves in progress and, per disk, whether it can be
removed safely. ``storage_issues`` derives the actionable health list.

Nothing here assumes LV names: the data volume is whatever LV backs /data,
which on single-disk appliances is the root LV.
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Optional

VOLUME_HELPER = "/usr/local/sbin/zenplus-volume-helper"
DATA_MOUNT = "/data"
SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", DATA_MOUNT)

GIB = 1024 ** 3
# Below this, partition alignment and LVM metadata rounding are noise.
CLAIMABLE_MIN_BYTES = 256 * 1024 ** 2
# GPT keeps a backup header in the last 33 sectors of a disk.
GPT_BACKUP_SECTORS = 34
SECTOR = 512


def human_size(n: float) -> str:
    """Binary units labelled as such, matching the Storage page."""
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


# ─── Helper invocation ───────────────────────────────────────────────────────

class HelperResult:
    def __init__(self, returncode: int, payload: dict[str, Any]):
        self.returncode = returncode
        self.payload = payload

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and bool(self.payload.get("ok"))

    @property
    def rejected(self) -> bool:
        return self.returncode == 2

    @property
    def error(self) -> str:
        return str(self.payload.get("error") or "storage helper failed")

    @property
    def code(self) -> str:
        return str(self.payload.get("code") or "failed")


def run_volume_helper(*args: str, timeout: int = 120) -> HelperResult:
    """Run a helper verb via sudo (blocking: call through asyncio.to_thread)."""
    try:
        proc = subprocess.run(
            ["sudo", "-n", VOLUME_HELPER, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return HelperResult(1, {"ok": False, "code": "timeout",
                                "error": f"storage operation timed out after {timeout}s"})
    except OSError as exc:
        return HelperResult(1, {"ok": False, "code": "unavailable", "error": str(exc)})
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1]) if proc.stdout.strip() else {}
    except (ValueError, IndexError):
        payload = {}
    if not payload:
        detail = (proc.stderr or proc.stdout).strip()
        if "password is required" in detail or "not allowed" in detail or "no such file" in detail.lower():
            payload = {"ok": False, "code": "unavailable",
                       "error": "Disk management is not installed on this appliance. "
                                "Run: sudo bash /opt/zenplus/scripts/setup-storage.sh"}
        else:
            payload = {"ok": False, "code": "failed", "error": detail[-600:] or "no output"}
    return HelperResult(proc.returncode, payload)


# ─── Filesystem usage ────────────────────────────────────────────────────────

def fs_usage(path: str) -> Optional[dict[str, Any]]:
    try:
        st = os.statvfs(path)
    except OSError:
        return None
    total = st.f_blocks * st.f_frsize
    free = st.f_bavail * st.f_frsize
    used = total - st.f_bfree * st.f_frsize
    # Percent as df reports it: used / (used + available), so root-reserved
    # blocks count as unavailable rather than silently free.
    denominator = used + free
    return {
        "total_bytes": total,
        "used_bytes": used,
        "free_bytes": free,
        "usage_percent": round(used / denominator * 100, 1) if denominator else 0.0,
    }


def shares_filesystem(a: str, b: str) -> bool:
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


# ─── Layout derivation (pure) ────────────────────────────────────────────────

def _int(value: Any) -> int:
    try:
        return int(float(str(value).strip() or 0))
    except (TypeError, ValueError):
        return 0


def _flag(value: Any) -> bool:
    return value is True or str(value).strip().lower() in ("1", "true")


def _walk(devices: list[dict]):
    for dev in devices:
        yield dev
        yield from _walk(dev.get("children") or [])


def _is_system_mount(mountpoint: Optional[str]) -> bool:
    mountpoint = mountpoint or ""
    return mountpoint in SYSTEM_MOUNTS or mountpoint.startswith("/boot") or mountpoint == "[SWAP]"


def _real(path: str, resolve) -> str:
    return resolve(path) if path else ""


def _claimable_partition_bytes(geometry: dict, disk: str, part: str) -> int:
    """Unpartitioned space after ``part`` when it is the last partition."""
    info = geometry.get(disk) or {}
    parts = info.get("partitions") or {}
    target = parts.get(part)
    if not target or not info.get("size_sectors"):
        return 0
    end = target["start"] + target["size"]
    if any(p["start"] > target["start"] for p in parts.values()):
        return 0
    free_sectors = info["size_sectors"] - end - GPT_BACKUP_SECTORS
    return max(0, free_sectors * SECTOR)


def build_layout(inv: dict, usage: dict[str, Optional[dict]],
                 resolve=os.path.realpath) -> dict[str, Any]:
    """Derive the disk/volume model from helper ``inventory`` facts.

    ``usage`` maps mount point -> fs_usage(); ``resolve`` canonicalizes device
    paths (injected so tests need no real /dev).
    """
    devices = [d for d in inv.get("lsblk") or [] if d.get("type") == "disk"]
    pvs = inv.get("pvs") or []
    vgs = inv.get("vgs") or []
    lvs = inv.get("lvs") or []
    geometry = inv.get("geometry") or {}
    mounts = inv.get("mounts") or {}

    # LV behind each managed mount.
    # findmnt reports /dev/mapper/<vg>-<lv>; LVM reports both that (lv_dm_path)
    # and /dev/<vg>/<lv>. Match either literally, then via the resolved node.
    lv_by_path: dict[str, dict] = {}
    for lv in lvs:
        for key in ("lv_path", "lv_dm_path"):
            if lv.get(key):
                lv_by_path.setdefault(lv[key], lv)
                lv_by_path.setdefault(_real(lv[key], resolve), lv)
    volumes: dict[str, dict] = {}
    for mount in ("/", DATA_MOUNT):
        row = mounts.get(mount)
        if not row:
            continue
        source = row.get("source", "")
        lv = lv_by_path.get(source) or lv_by_path.get(_real(source, resolve))
        key = (lv or {}).get("lv_path") or row.get("source") or mount
        vol = volumes.setdefault(key, {
            "vg": (lv or {}).get("vg_name"),
            "lv": (lv or {}).get("lv_name"),
            "path": (lv or {}).get("lv_path") or row.get("source"),
            "size_bytes": _int((lv or {}).get("lv_size")),
            "on_lvm": lv is not None,
            "fstype": row.get("fstype"),
            "mount_point": row.get("target"),
            "roles": [],
            "usage": usage.get(row.get("target") or mount),
        })
        role = "system" if mount == "/" else "data"
        if role not in vol["roles"]:
            vol["roles"].append(role)
    data_volume = next((v for v in volumes.values() if "data" in v["roles"]), None)
    data_vg_name = (data_volume or {}).get("vg") if (data_volume or {}).get("on_lvm") else None

    vg_rows = []
    for vg in vgs:
        vg_rows.append({
            "name": vg.get("vg_name"),
            "size_bytes": _int(vg.get("vg_size")),
            "free_bytes": _int(vg.get("vg_free")),
            "extent_bytes": _int(vg.get("vg_extent_size")),
            "pv_count": _int(vg.get("pv_count")),
            "missing_pv_count": _int(vg.get("vg_missing_pv_count")),
            "is_data": vg.get("vg_name") == data_vg_name,
        })
    data_vg = next((v for v in vg_rows if v["is_data"]), None)

    pvmove = next((lv for lv in lvs if (lv.get("lv_attr") or "").startswith("p")), None)
    moving = None
    if pvmove:
        moving = {"pv": pvmove.get("move_pv"), "vg": pvmove.get("vg_name"),
                  "percent": round(float(pvmove.get("copy_percent") or 0), 1)}

    # A disk is a system disk when an OS partition is mounted from it directly
    # (/boot, swap, a plain /). Disks that only carry LVM extents of the root
    # or data LV are pool members and can be removed once their data moves.
    system_names = {
        dev.get("name") for dev in devices
        if any(n.get("type") in ("disk", "part") and _is_system_mount(n.get("mountpoint"))
               for n in _walk([dev]))
    }
    data_pv_rows = [p for p in pvs if data_vg_name and p.get("vg_name") == data_vg_name
                    and not p.get("pv_missing")]

    def pv_view(pv: dict, disk_name: str, is_partition: bool, on_system_disk: bool) -> dict:
        size = _int(pv.get("pv_size"))
        used = _int(pv.get("pv_used"))
        free = _int(pv.get("pv_free"))
        in_data = bool(data_vg_name) and pv.get("vg_name") == data_vg_name
        part_name = os.path.basename(pv.get("pv_name", ""))
        claimable = 0
        if in_data:
            if is_partition:
                claimable = _claimable_partition_bytes(geometry, disk_name, part_name)
            else:
                claimable = max(0, _int(pv.get("dev_size")) - size)
            if claimable < CLAIMABLE_MIN_BYTES:
                claimable = 0
        removal: dict[str, Any] = {"action": None, "reason": None}
        if in_data:
            others_free = sum(_int(p.get("pv_free")) for p in data_pv_rows
                              if p.get("pv_name") != pv.get("pv_name"))
            if on_system_disk:
                removal["reason"] = "This disk holds the operating system and cannot be removed."
            elif (data_vg or {}).get("pv_count", 0) <= 1:
                removal["reason"] = "This is the only disk in the storage pool."
            elif moving and moving.get("pv") == pv.get("pv_name"):
                removal["reason"] = f"Moving data off this disk ({moving['percent']}% done)."
            elif used == 0:
                removal["action"] = "remove"
            elif others_free >= used:
                removal["action"] = "evacuate"
                removal["reason"] = (f"{human_size(used)} of data will move to the other disks "
                                     "while the appliance keeps running.")
            else:
                removal["reason"] = (
                    f"{human_size(used)} of data is stored on this disk but the other disks have "
                    f"only {human_size(others_free)} unallocated. Add a disk of at least "
                    f"{human_size(used - others_free)} (without allocating it) to make room.")
        return {
            "path": pv.get("pv_name"),
            "vg": pv.get("vg_name") or None,
            "in_data_vg": in_data,
            "size_bytes": size,
            "used_bytes": used,
            "free_bytes": free,
            "claimable_bytes": claimable,
            "removal": removal,
        }

    pv_by_name = {p.get("pv_name"): p for p in pvs if not p.get("pv_missing")}
    disks = []
    for dev in devices:
        name = dev.get("name")
        path = dev.get("path") or f"/dev/{name}"
        tree = list(_walk([dev]))
        own_pvs = []
        for node in tree:
            node_path = node.get("path") or f"/dev/{node.get('name')}"
            if node_path in pv_by_name:
                own_pvs.append(pv_view(pv_by_name[node_path], name,
                                       node is not dev, name in system_names))
        partitions = [
            {"name": c.get("name"), "path": c.get("path") or f"/dev/{c.get('name')}",
             "size_bytes": _int(c.get("size")), "fstype": c.get("fstype"),
             "mountpoint": c.get("mountpoint")}
            for c in dev.get("children") or [] if c.get("type") == "part"
        ]
        foreign_vg = next((p["vg"] for p in own_pvs if p["vg"] and not p["in_data_vg"]), None)
        orphan_pv = any(not p["vg"] for p in own_pvs)
        if name in system_names:
            status, label = "system", "System disk"
        elif any(p["in_data_vg"] for p in own_pvs):
            status, label = "pool", "Storage pool"
        elif _flag(dev.get("ro")):
            status, label = "read_only", "Read-only"
        elif foreign_vg:
            status, label = "foreign", f"Volume group {foreign_vg}"
        elif any(n.get("mountpoint") for n in tree):
            status, label = "in_use", "In use"
        elif not dev.get("children") and (not dev.get("fstype") or orphan_pv):
            status, label = "unused", "Unused"
        else:
            status, label = "needs_wipe", "Contains data"
        detail = None
        if status == "needs_wipe":
            kinds = sorted({c.get("fstype") for c in tree if c.get("fstype")})
            detail = ("Has " + (", ".join(kinds) + " data" if kinds else "a partition table")
                      + ". Adding it erases everything on the disk.")
        elif status == "unused" and orphan_pv:
            detail = "Initialized for LVM but not part of any volume group."
        elif status == "foreign":
            detail = (f"Belongs to volume group {foreign_vg}, which this appliance does not use. "
                      "Remove it from that group before reusing the disk.")
        disks.append({
            "name": name,
            "path": path,
            "size_bytes": _int(dev.get("size")),
            "model": (dev.get("model") or "").strip() or None,
            "serial": (dev.get("serial") or "").strip() or None,
            "transport": dev.get("tran"),
            "rotational": _flag(dev.get("rota")),
            "read_only": _flag(dev.get("ro")),
            "status": status,
            "label": label,
            "detail": detail,
            "can_add": status in ("unused", "needs_wipe") and bool(data_vg_name),
            "needs_wipe": status == "needs_wipe",
            "pvs": own_pvs,
            "partitions": partitions,
        })

    missing = [
        {"uuid": p.get("pv_uuid"), "vg": p.get("vg_name"),
         "size_bytes": _int(p.get("pv_size")), "used_bytes": _int(p.get("pv_used"))}
        for p in pvs if p.get("pv_missing")
    ]
    claimable = [
        {"pv": pv["path"], "disk": d["name"], "bytes": pv["claimable_bytes"]}
        for d in disks for pv in d["pvs"] if pv["claimable_bytes"] > 0
    ]
    tools = inv.get("tools") or {}
    return {
        "available": True,
        "errors": inv.get("errors") or [],
        "data_vg": data_vg_name,
        "data_volume": data_volume,
        "data_shares_os": bool(data_volume and "system" in data_volume["roles"]),
        "volumes": list(volumes.values()),
        "volume_groups": vg_rows,
        "disks": disks,
        "missing_pvs": missing,
        "claimable": claimable,
        "pvmove": moving,
        "unallocated_bytes": (data_vg or {}).get("free_bytes", 0),
        "extent_bytes": (data_vg or {}).get("extent_bytes", 0),
        "tools": tools,
    }


def unavailable_layout(error: str) -> dict[str, Any]:
    return {
        "available": False, "errors": [error], "data_vg": None, "data_volume": None,
        "data_shares_os": False, "volumes": [], "volume_groups": [], "disks": [],
        "missing_pvs": [], "claimable": [], "pvmove": None, "unallocated_bytes": 0,
        "extent_bytes": 0, "tools": {},
    }


# ─── Health ──────────────────────────────────────────────────────────────────

SEVERITY_ORDER = {"critical": 0, "warning": 1, "notice": 2, "info": 3}


def storage_issues(layout: dict, usage: dict[str, Optional[dict]],
                   clickhouse_check: Optional[dict] = None) -> list[dict]:
    """Actionable findings, most severe first."""
    issues: list[dict] = []

    def add(severity, code, title, detail=None, action=None):
        issues.append({"severity": severity, "code": code, "title": title,
                       "detail": detail, "action": action})

    if not layout.get("available"):
        add("warning", "helper_unavailable", "Disk management is unavailable",
            "; ".join(layout.get("errors") or []) or None)

    for m in layout.get("missing_pvs") or []:
        if m["used_bytes"] > 0:
            add("critical", "pv_missing",
                f"A {human_size(m['size_bytes'])} disk is missing from volume group {m['vg']}",
                f"It held {human_size(m['used_bytes'])} of data. Reattach the disk to the VM "
                "and rescan; until then the affected data cannot be read.", "rescan")
        else:
            add("warning", "pv_missing_empty",
                f"An empty {human_size(m['size_bytes'])} disk is missing from volume group {m['vg']}",
                "It held no data. Forget it to return the pool to a clean state.",
                "remove_missing")

    seen = set()
    for mount, label in ((DATA_MOUNT, "Data volume"), ("/", "System volume")):
        u = usage.get(mount)
        if not u:
            continue
        key = (u["total_bytes"], u["used_bytes"])
        if key in seen:
            continue
        seen.add(key)
        pct = u["usage_percent"]
        name = "Storage" if layout.get("data_shares_os") else label
        if pct >= 95:
            add("critical", "usage_critical", f"{name} is {pct:.0f}% full",
                "Ingest stops when the disk fills. Free space or expand storage now.", "expand")
        elif pct >= 85:
            add("warning", "usage_high", f"{name} is {pct:.0f}% full",
                "Expand storage or shorten retention soon.", "expand")
        elif pct >= 75:
            add("notice", "usage_elevated", f"{name} is {pct:.0f}% full",
                "Plan for expansion.", "expand")

    if layout.get("pvmove"):
        mv = layout["pvmove"]
        add("info", "pvmove", f"Moving data off {mv['pv']} ({mv['percent']}%)",
            "The disk can be removed once the move completes.")
    for c in layout.get("claimable") or []:
        add("info", "claimable", f"{human_size(c['bytes'])} can be claimed on {c['pv']}",
            "The virtual disk was enlarged. Claim the space to add it to the storage pool.",
            "claim")
    unallocated = layout.get("unallocated_bytes") or 0
    if unallocated >= GIB:
        add("info", "unallocated", f"{human_size(unallocated)} unallocated in the storage pool",
            "Allocate it to the data volume, or keep it as reserve.", "extend")
    for d in layout.get("disks") or []:
        if d.get("can_add"):
            add("info", "new_disk", f"New disk {d['path']} ({human_size(d['size_bytes'])}) detected",
                d.get("detail") or "Add it to the storage pool to expand capacity.", "add")
    if clickhouse_check and not clickhouse_check.get("ok"):
        add("warning", "clickhouse_drift", "ClickHouse storage layout has drifted",
            clickhouse_check.get("message"))

    issues.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    return issues


def overall_health(issues: list[dict]) -> tuple[str, str]:
    """Legacy health/health_message pair for older clients."""
    for i in issues:
        if i["severity"] == "critical":
            return "critical", i["title"]
    for i in issues:
        if i["severity"] in ("warning", "notice"):
            return "warning", i["title"]
    return "ok", "Storage is healthy."


# ─── ClickHouse placement ────────────────────────────────────────────────────

CLICKHOUSE_HOST_PATH = "/data/clickhouse"
CLICKHOUSE_CONTAINER_PATH = "/var/lib/clickhouse"
COMPOSE_FILE = "/opt/zenplus/docker-compose.yml"


def clickhouse_storage_check() -> dict[str, Any]:
    """Verify ClickHouse data lives on /data and the bind mount is intact.

    Catches drift from the documented appliance layout: /data/clickhouse must
    exist on the /data filesystem (so it benefits when that volume grows),
    docker-compose must bind-mount it to /var/lib/clickhouse, and ClickHouse
    must report that path. ClickHouse reachability is optional: the storage
    page must still render when the database is down.
    """
    result: dict[str, Any] = {
        "host_path": CLICKHOUSE_HOST_PATH,
        "container_path": CLICKHOUSE_CONTAINER_PATH,
        "host_path_exists": os.path.isdir(CLICKHOUSE_HOST_PATH),
        "on_data_volume": False,
        "bind_mount_ok": False,
        "clickhouse_reported_path": "",
        "ok": False,
        "message": "",
    }
    if result["host_path_exists"]:
        result["on_data_volume"] = shares_filesystem(CLICKHOUSE_HOST_PATH, DATA_MOUNT)
    try:
        with open(COMPOSE_FILE, "r", encoding="utf-8") as f:
            result["bind_mount_ok"] = f"{CLICKHOUSE_HOST_PATH}:{CLICKHOUSE_CONTAINER_PATH}" in f.read()
    except OSError:
        pass
    try:
        import httpx

        r = httpx.get(
            "http://127.0.0.1:8123/",
            params={
                "query": "SELECT path FROM system.disks WHERE name = 'default' FORMAT TSV",
                "user": os.environ.get("CLICKHOUSE_USER", "default"),
                "password": os.environ.get("CLICKHOUSE_PASSWORD", ""),
            },
            timeout=3,
        )
        if r.status_code == 200:
            result["clickhouse_reported_path"] = r.text.strip()
    except Exception:
        pass
    reported = result["clickhouse_reported_path"]
    reported_ok = (not reported) or reported.rstrip("/") == CLICKHOUSE_CONTAINER_PATH
    result["ok"] = bool(result["host_path_exists"] and result["on_data_volume"]
                        and result["bind_mount_ok"] and reported_ok)
    if result["ok"]:
        result["message"] = ("ClickHouse data is stored on the /data volume and grows "
                             "automatically when that volume is extended.")
    else:
        issues = []
        if not result["host_path_exists"]:
            issues.append(f"{CLICKHOUSE_HOST_PATH} does not exist")
        elif not result["on_data_volume"]:
            issues.append(f"{CLICKHOUSE_HOST_PATH} is not on the /data volume")
        if not result["bind_mount_ok"]:
            issues.append("docker-compose is missing the expected ClickHouse bind mount")
        if reported and not reported_ok:
            issues.append(f"ClickHouse reports its data path as '{reported}' "
                          f"(expected '{CLICKHOUSE_CONTAINER_PATH}/')")
        result["message"] = "ClickHouse storage configuration has drifted: " + "; ".join(issues)
    return result


# ─── Watchdog findings ───────────────────────────────────────────────────────

def watchdog_issues(wd: dict) -> list[dict]:
    """Findings from the free-space watchdog state (see storage_watchdog)."""
    issues: list[dict] = []

    def add(severity, code, title, detail=None, action=None):
        issues.append({"severity": severity, "code": code, "title": title,
                       "detail": detail, "action": action})

    if not wd.get("installed"):
        add("warning", "watchdog_missing", "Free-space watchdog is not installed",
            "Low-space protection only runs inside the API every 5 minutes and stops when "
            "the API or PostgreSQL is down. Re-run the storage setup (applied on upgrade).")
        return issues
    if not wd.get("alive"):
        add("warning", "watchdog_stale",
            f"Free-space watchdog has not reported for {wd.get('heartbeat_age_s', 0) // 60} min",
            "Check: systemctl status zenplus-storage-watchdog")
    if wd.get("ingest_paused"):
        add("critical", "ingest_paused", "Telemetry ingestion is paused: disk critically low",
            "Host agent and APM uploads are held in the agents' spools until space returns. "
            "Free space or expand storage.", "expand")
    for fs in wd.get("filesystems") or []:
        if fs.get("exhausted"):
            add("critical" if fs.get("state") == "critical" else "warning", "purge_exhausted",
                f"No more data can be purged on {fs.get('mount')} within policy",
                "Everything eligible was already removed; the remaining space is protected "
                "data or other files. Expand storage or shorten retention.", "expand")
        if fs.get("state") == "growth" and fs.get("runway_hours") is not None:
            rate = fs.get("growth_bytes_per_hour") or 0
            add("warning", "fast_growth",
                f"Disk filling fast: full in about {fs['runway_hours']:.1f} h at {human_size(rate)}/h",
                "Diagnostic logs are being shed. Check the fastest-growing tables on the Overview.")
    err = wd.get("last_error")
    if err and wd.get("heartbeat") and float(wd["heartbeat"]) - float(err.get("at") or 0) < 900:
        add("warning", "watchdog_error", "Free-space watchdog reported an error", err.get("message"))
    issues.sort(key=lambda i: SEVERITY_ORDER[i["severity"]])
    return issues
