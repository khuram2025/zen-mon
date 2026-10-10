"""Disk/volume model, health findings, auto-purge decisions and forecasts."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.api.v1 import storage_management as api
from app.core.database import get_db
from app.core.security import require_admin_user
from app.main import app
from app.services import storage_layout as L
from app.services import storage_service as svc

GIB = 1024 ** 3
ROOT_LV = "/dev/ubuntu-vg/ubuntu-lv"
ROOT_DM = "/dev/mapper/ubuntu--vg-ubuntu--lv"
ALIASES = {ROOT_LV: ROOT_DM}


def resolve(path):
    return ALIASES.get(path, path)


# The .221 layout: one 100 GB disk, /data on the root LV, no data-lv.
SINGLE = {
    "lsblk": [
        {"name": "sda", "path": "/dev/sda", "size": 100 * GIB, "type": "disk",
         "model": "VMware Virtual S", "tran": "spi", "ro": False, "rota": True,
         "children": [
             {"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 1024 ** 2},
             {"name": "sda2", "path": "/dev/sda2", "type": "part", "fstype": "ext4",
              "mountpoint": "/boot", "size": 2 * GIB},
             {"name": "sda3", "path": "/dev/sda3", "type": "part", "fstype": "LVM2_member",
              "size": 98 * GIB, "children": [
                  {"name": "ubuntu--vg-ubuntu--lv", "path": ROOT_DM, "type": "lvm",
                   "fstype": "ext4", "mountpoint": "/"}]}]},
        {"name": "sr0", "path": "/dev/sr0", "type": "rom", "size": GIB},
    ],
    "pvs": [{"pv_name": "/dev/sda3", "vg_name": "ubuntu-vg", "pv_size": str(98 * GIB),
             "pv_free": "0", "pv_used": str(98 * GIB), "dev_size": str(98 * GIB),
             "pv_missing": "", "pv_uuid": "a"}],
    "vgs": [{"vg_name": "ubuntu-vg", "vg_size": str(98 * GIB), "vg_free": "0",
             "vg_extent_size": str(4 * 1024 ** 2), "pv_count": "1", "lv_count": "1",
             "vg_missing_pv_count": "0"}],
    "lvs": [{"lv_name": "ubuntu-lv", "vg_name": "ubuntu-vg", "lv_path": ROOT_LV,
             "lv_dm_path": ROOT_DM, "lv_size": str(98 * GIB), "lv_attr": "-wi-ao----", "copy_percent": "",
             "move_pv": ""}],
    "geometry": {"sda": {"size_sectors": 100 * GIB // 512, "partitions": {
        "sda1": {"number": 1, "start": 2048, "size": 2048},
        "sda2": {"number": 2, "start": 4096, "size": 2 * GIB // 512},
        "sda3": {"number": 3, "start": 4096 + 2 * GIB // 512, "size": 98 * GIB // 512 - 4096 - 34},
    }}},
    "mounts": {"/": {"target": "/", "source": ROOT_DM, "fstype": "ext4"},
               "/data": {"target": "/", "source": ROOT_DM, "fstype": "ext4"},
               "/boot": {"target": "/boot", "source": "/dev/sda2", "fstype": "ext4"}},
    "tools": {"growpart": True},
    "errors": [],
}
USAGE = {"/": {"total_bytes": 96 * GIB, "used_bytes": 72 * GIB, "free_bytes": 24 * GIB,
               "usage_percent": 75.0}}
USAGE["/data"] = USAGE["/"]


def layout(inv=None, usage=USAGE):
    return L.build_layout(copy.deepcopy(inv or SINGLE), usage, resolve=resolve)


def test_mount_source_matches_lv_dm_path_without_device_nodes():
    # CI has no /dev/mapper nodes: matching must not rely on realpath.
    lay = L.build_layout(copy.deepcopy(SINGLE), USAGE, resolve=lambda p: p)
    assert lay["data_vg"] == "ubuntu-vg" and lay["volumes"][0]["lv"] == "ubuntu-lv"


def test_single_disk_appliance_resolves_data_to_the_root_lv():
    lay = layout()
    assert lay["data_vg"] == "ubuntu-vg"
    assert lay["data_shares_os"] is True
    assert [v["lv"] for v in lay["volumes"]] == ["ubuntu-lv"]
    assert lay["volumes"][0]["roles"] == ["system", "data"]
    assert [d["name"] for d in lay["disks"]] == ["sda"]          # optical drive ignored
    sda = lay["disks"][0]
    assert sda["status"] == "system" and not sda["can_add"]
    assert sda["pvs"][0]["removal"]["action"] is None
    assert "operating system" in sda["pvs"][0]["removal"]["reason"]
    assert lay["claimable"] == [] and lay["unallocated_bytes"] == 0


def test_enlarged_virtual_disk_is_claimable():
    inv = copy.deepcopy(SINGLE)
    inv["geometry"]["sda"]["size_sectors"] = 200 * GIB // 512    # VMware disk grown to 200 GB
    lay = layout(inv)
    assert len(lay["claimable"]) == 1
    claim = lay["claimable"][0]
    assert claim["pv"] == "/dev/sda3" and 99 * GIB < claim["bytes"] <= 100 * GIB
    issues = L.storage_issues(lay, USAGE)
    assert any(i["code"] == "claimable" and i["action"] == "claim" for i in issues)


def test_partition_that_is_not_last_is_not_claimable():
    inv = copy.deepcopy(SINGLE)
    inv["geometry"]["sda"]["size_sectors"] = 200 * GIB // 512
    inv["geometry"]["sda"]["partitions"]["sda4"] = {"number": 4, "start": 190 * GIB // 512,
                                                    "size": 1024}
    assert layout(inv)["claimable"] == []


def test_new_and_previously_used_disks_are_offered_with_the_right_warning():
    inv = copy.deepcopy(SINGLE)
    inv["lsblk"].append({"name": "sdb", "path": "/dev/sdb", "size": 500 * GIB, "type": "disk"})
    inv["lsblk"].append({"name": "sdc", "path": "/dev/sdc", "size": 50 * GIB, "type": "disk",
                         "children": [{"name": "sdc1", "type": "part", "fstype": "ntfs"}]})
    inv["lsblk"].append({"name": "sdd", "path": "/dev/sdd", "size": 50 * GIB, "type": "disk",
                         "children": [{"name": "sdd1", "type": "part", "mountpoint": "/mnt/x"}]})
    disks = {d["name"]: d for d in layout(inv)["disks"]}
    assert disks["sdb"]["status"] == "unused" and disks["sdb"]["can_add"]
    assert not disks["sdb"]["needs_wipe"]
    assert disks["sdc"]["status"] == "needs_wipe" and disks["sdc"]["can_add"]
    assert "ntfs" in disks["sdc"]["detail"]
    assert disks["sdd"]["status"] == "in_use" and not disks["sdd"]["can_add"]


def _pool(sdb_used, sda_free=0):
    inv = copy.deepcopy(SINGLE)
    inv["lsblk"].append({"name": "sdb", "path": "/dev/sdb", "size": 50 * GIB, "type": "disk",
                         "fstype": "LVM2_member", "children": [
                             {"name": "ubuntu--vg-ubuntu--lv", "path": ROOT_DM, "type": "lvm",
                              "mountpoint": "/"}]})
    inv["pvs"][0]["pv_free"] = str(sda_free)
    inv["pvs"].append({"pv_name": "/dev/sdb", "vg_name": "ubuntu-vg", "pv_size": str(50 * GIB),
                       "pv_free": str(50 * GIB - sdb_used), "pv_used": str(sdb_used),
                       "dev_size": str(50 * GIB), "pv_missing": ""})
    inv["vgs"][0]["pv_count"] = "2"
    return inv


def test_disk_carrying_root_lv_extents_is_a_removable_pool_member():
    # After "add disk", lsblk shows the root LV (mounted at /) under sdb too.
    # That must not make sdb a system disk: only sda has /boot.
    disks = {d["name"]: d for d in layout(_pool(0))["disks"]}
    assert disks["sda"]["status"] == "system"
    assert disks["sdb"]["status"] == "pool" and disks["sdb"]["label"] == "Storage pool"
    assert disks["sdb"]["pvs"][0]["removal"]["action"] == "remove"


def test_removal_plan_depends_on_room_on_the_other_disks():
    pv = {d["name"]: d for d in layout(_pool(20 * GIB, sda_free=30 * GIB))["disks"]}["sdb"]["pvs"][0]
    assert pv["removal"]["action"] == "evacuate"

    pv = {d["name"]: d for d in layout(_pool(20 * GIB, sda_free=5 * GIB))["disks"]}["sdb"]["pvs"][0]
    assert pv["removal"]["action"] is None
    assert "15.0 GiB" in pv["removal"]["reason"]


def test_missing_disk_with_data_is_critical_and_empty_one_can_be_forgotten():
    inv = copy.deepcopy(SINGLE)
    inv["pvs"].append({"pv_name": "[unknown]", "vg_name": "ubuntu-vg", "pv_size": str(50 * GIB),
                       "pv_used": str(10 * GIB), "pv_missing": "missing", "pv_uuid": "zz"})
    issues = L.storage_issues(layout(inv), USAGE)
    assert issues[0]["severity"] == "critical" and issues[0]["code"] == "pv_missing"
    assert L.overall_health(issues)[0] == "critical"

    inv["pvs"][-1]["pv_used"] = "0"
    issues = L.storage_issues(layout(inv), USAGE)
    assert any(i["action"] == "remove_missing" for i in issues)


def test_shared_volume_usage_is_reported_once():
    issues = L.storage_issues(layout(), USAGE)
    usage_issues = [i for i in issues if i["code"].startswith("usage")]
    assert len(usage_issues) == 1
    assert usage_issues[0]["title"].startswith("Storage is 75%")
    assert L.overall_health(issues)[0] == "warning"


def test_pvmove_progress_is_surfaced():
    inv = _pool(10 * GIB, sda_free=30 * GIB)
    inv["lvs"].append({"lv_name": "[pvmove0]", "vg_name": "ubuntu-vg", "lv_attr": "p-C-aom---",
                       "copy_percent": "37.50", "move_pv": "/dev/sdb"})
    lay = layout(inv)
    assert lay["pvmove"] == {"pv": "/dev/sdb", "vg": "ubuntu-vg", "percent": 37.5}


def test_unavailable_helper_still_yields_a_renderable_model():
    lay = L.unavailable_layout("not installed")
    issues = L.storage_issues(lay, USAGE)
    assert issues[0]["code"] == "helper_unavailable"


# ─── Forecast ────────────────────────────────────────────────────────────────

def _table(name, cat, size, days_held, ttl=None):
    newest = datetime(2026, 10, 8)
    return {"table": name, "category": cat, "size_bytes": size,
            "oldest": (newest - timedelta(days=days_held)).isoformat(),
            "newest": newest.isoformat(), "current_ttl_days": ttl}


def test_forecast_levels_off_at_retention():
    tables = [_table("snmp", "raw_metrics", 10 * GIB, 10, ttl=30)]   # 1 GB/day, 30-day TTL
    f = svc.capacity_forecast(tables, {"categories": {}, "table_overrides": {}}, 50 * GIB)
    assert f["daily_bytes"] == GIB
    assert f["steady_state_bytes"] == 30 * GIB
    assert f["growth_remaining_bytes"] == 20 * GIB
    assert f["fits"] is True and f["days_until_full"] is None

    f = svc.capacity_forecast(tables, {"categories": {}, "table_overrides": {}}, 5 * GIB)
    assert f["fits"] is False and f["days_until_full"] == 5


def test_forecast_respects_configured_retention_and_flags_unbounded_tables():
    tables = [_table("snmp", "raw_metrics", 10 * GIB, 10, ttl=30),
              _table("traps", "events", GIB, 10)]
    f = svc.capacity_forecast(
        tables, {"categories": {"raw_metrics": 60}, "table_overrides": {}}, 500 * GIB)
    assert f["categories"]["raw_metrics"]["projected_bytes"] == 60 * GIB
    assert f["unbounded_categories"] == ["events"]
    assert f["fits"] is False


def test_forecast_ignores_tables_without_time_bounds():
    t = _table("x", "raw_metrics", GIB, 10)
    t["oldest"] = "1970-01-01T00:00:00"
    f = svc.capacity_forecast([t], {"categories": {}, "table_overrides": {}}, GIB)
    assert f["daily_bytes"] == 0 and f["current_bytes"] == GIB


# ─── API wiring ──────────────────────────────────────────────────────────────

class _DB:
    def __init__(self):
        self.statements = []

    async def execute(self, statement, params=None):
        self.statements.append((str(statement), params))

        class R:
            def first(self):
                return None

            def scalar(self):
                return 0
        return R()

    async def commit(self):
        pass

    def add(self, _obj):
        pass

    async def flush(self):
        pass


@pytest.fixture
def storage_client(monkeypatch):
    calls = []

    def fake_helper(*args, timeout=120):
        calls.append(args)
        if args[0] == "inventory":
            return L.HelperResult(0, {"ok": True, **copy.deepcopy(SINGLE)})
        if args[0] == "add-disk" and args[1] == "/dev/sda":
            return L.HelperResult(2, {"ok": False, "code": "system_disk",
                                      "error": "/dev/sda holds the operating system"})
        if args[0] == "add-disk":
            return L.HelperResult(0, {"ok": True, "vg": "ubuntu-vg", "pv_size": 50 * GIB})
        if args[0] == "extend":
            return L.HelperResult(0, {"ok": True, "vg": "ubuntu-vg", "lv": "ubuntu-lv",
                                      "added_bytes": 50 * GIB, "size_after": 148 * GIB})
        return L.HelperResult(0, {"ok": True})

    monkeypatch.setattr(L, "run_volume_helper", fake_helper)
    monkeypatch.setattr(L, "clickhouse_storage_check", lambda: {"ok": True, "message": "ok"})
    monkeypatch.setattr(L, "fs_usage", lambda m: USAGE["/"])

    async def audit(*a, **k):
        return None
    monkeypatch.setattr(api, "write_audit_log", audit)
    db = _DB()

    async def fake_db():
        yield db
    app.dependency_overrides[get_db] = fake_db
    app.dependency_overrides[require_admin_user] = lambda: type("U", (), {"username": "admin", "id": 1})()
    # No context manager: startup hooks would open connections to the default
    # (installed) databases, which tests must never touch.
    client = TestClient(app)
    yield client, calls, db
    app.dependency_overrides.pop(get_db, None)
    app.dependency_overrides.pop(require_admin_user, None)


def test_status_endpoint_reports_real_lv_and_issues(storage_client):
    client, _calls, _db = storage_client
    body = client.get("/api/v1/system/storage").json()
    assert body["vg_name"] == "ubuntu-vg" and body["lv_name"] == "ubuntu-lv"
    assert body["layout"]["data_shares_os"] is True
    assert body["health"] == "warning"
    codes = [i["code"] for i in body["issues"]]
    # No watchdog state in the isolated test directory: that is reported too.
    assert codes[:2] == ["watchdog_missing", "usage_elevated"]


def test_add_disk_allocates_and_wipe_needs_typed_confirmation(storage_client):
    client, calls, db = storage_client
    r = client.post("/api/v1/system/storage/add-disk", json={"disk": "/dev/sdb", "wipe": True})
    assert r.status_code == 400 and calls == []

    r = client.post("/api/v1/system/storage/add-disk",
                    json={"disk": "/dev/sdb", "allocate": str(10 * GIB)})
    assert r.status_code == 200, r.text
    assert calls == [("add-disk", "/dev/sdb"), ("extend", "/data", str(10 * GIB))]
    assert any("storage_events" in s for s, _ in db.statements)

    calls.clear()
    r = client.post("/api/v1/system/storage/add-disk", json={"disk": "/dev/sdb", "allocate": "none"})
    assert r.status_code == 200 and calls == [("add-disk", "/dev/sdb")]


def test_helper_rejections_become_conflicts(storage_client):
    client, _calls, _db = storage_client
    r = client.post("/api/v1/system/storage/add-disk", json={"disk": "/dev/sda"})
    assert r.status_code == 409
    assert "operating system" in r.json()["detail"]


@pytest.mark.parametrize("path,body", [
    ("/api/v1/system/storage/add-disk", {"disk": "/dev/sdb;reboot"}),
    ("/api/v1/system/storage/extend", {"mount": "/var"}),
    ("/api/v1/system/storage/evacuate", {"pv": "/dev/sdb", "confirm": "yes"}),
    ("/api/v1/system/storage/remove-disk", {"pv": "/dev/sdb"}),
])
def test_requests_are_validated_before_reaching_the_helper(storage_client, path, body):
    client, calls, _db = storage_client
    assert client.post(path, json=body).status_code in (400, 422)
    assert calls == []
