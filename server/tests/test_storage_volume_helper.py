"""The root volume helper is the security boundary for disk management.

Commands are faked: these tests assert what the helper would run as root and,
above all, what it refuses to run. The default system mirrors a single-disk
appliance where /data lives on the root LV (ubuntu-vg/ubuntu-lv) — there is
no dedicated data LV.
"""
from __future__ import annotations

import copy
import importlib.machinery
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
GIB = 1024 ** 3
EXTENT = 4 * 1024 ** 2


def load_helper():
    loader = importlib.machinery.SourceFileLoader(
        "zenplus_volume_helper", str(ROOT / "scripts" / "zenplus-volume-helper"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


ROOT_LV = "/dev/ubuntu-vg/ubuntu-lv"
ROOT_DM = "/dev/mapper/ubuntu--vg-ubuntu--lv"

BASE = {
    "lsblk": [
        {"name": "sda", "path": "/dev/sda", "size": 100 * GIB, "type": "disk", "ro": False,
         "children": [
             {"name": "sda1", "path": "/dev/sda1", "type": "part", "size": 1024 ** 2},
             {"name": "sda2", "path": "/dev/sda2", "type": "part", "fstype": "ext4",
              "mountpoint": "/boot", "size": 2 * GIB},
             {"name": "sda3", "path": "/dev/sda3", "type": "part", "fstype": "LVM2_member",
              "size": 98 * GIB, "children": [
                  {"name": "ubuntu--vg-ubuntu--lv", "path": ROOT_DM, "type": "lvm",
                   "fstype": "ext4", "mountpoint": "/"}]}]},
        {"name": "sdb", "path": "/dev/sdb", "size": 50 * GIB, "type": "disk", "ro": False},
    ],
    "pvs": [{"pv_name": "/dev/sda3", "vg_name": "ubuntu-vg", "pv_size": str(98 * GIB),
             "pv_free": "0", "pv_used": str(98 * GIB), "dev_size": str(98 * GIB),
             "pv_missing": "", "pv_uuid": "a"}],
    "vgs": [{"vg_name": "ubuntu-vg", "vg_size": str(98 * GIB), "vg_free": "0",
             "vg_extent_size": str(EXTENT), "pv_count": "1", "lv_count": "1",
             "vg_missing_pv_count": "0", "vg_attr": "wz--n-"}],
    "lvs": [{"lv_name": "ubuntu-lv", "vg_name": "ubuntu-vg", "lv_path": ROOT_LV,
             "lv_dm_path": ROOT_DM, "lv_size": str(98 * GIB), "lv_attr": "-wi-ao----",
             "segtype": "linear", "copy_percent": "", "move_pv": ""}],
    "mounts": {"/": {"target": "/", "source": ROOT_DM, "fstype": "ext4"},
               "/data": {"target": "/", "source": ROOT_DM, "fstype": "ext4"}},
    "wipefs": {},
}


class FakeSystem:
    def __init__(self, helper, state=None, fail=None):
        self.helper = helper
        self.state = copy.deepcopy(state or BASE)
        self.fail = fail or {}
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=120, check=True):
        self.calls.append(list(args))
        cmd, rc, out, err = args[0], 0, "", ""
        if cmd in ("pvs", "vgs", "lvs"):
            key = {"pvs": "pv", "vgs": "vg", "lvs": "lv"}[cmd]
            out = json.dumps({"report": [{key: self.state[cmd]}]})
        elif cmd == "lsblk":
            out = json.dumps({"blockdevices": self.state["lsblk"]})
        elif cmd == "findmnt":
            row = self.state["mounts"].get(args[-1])
            rc, out = (0, json.dumps({"filesystems": [row]})) if row else (1, "")
        elif cmd == "wipefs" and args[1] == "-J":
            sigs = self.state["wipefs"].get(args[2])
            out = json.dumps({"signatures": sigs}) if sigs else ""
        elif cmd in self.fail:
            rc, out, err = self.fail[cmd]
        result = subprocess.CompletedProcess(args, rc, out, err)
        if check and rc != 0:
            raise self.helper.Failed(f"{cmd} failed: {err or out}")
        return result

    def ran(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def mutations(self):
        readonly = {"pvs", "vgs", "lvs", "lsblk", "findmnt", "udevadm"}
        return [c for c in self.calls
                if c[0] not in readonly and not (c[0] == "wipefs" and c[1] == "-J")]


@pytest.fixture
def helper(monkeypatch, tmp_path):
    module = load_helper()
    sysfs = tmp_path / "sys"
    (sysfs / "class" / "block").mkdir(parents=True)
    monkeypatch.setattr(module, "SYS_CLASS_BLOCK", str(sysfs / "class" / "block"))
    monkeypatch.setattr(module, "LOCK_PATH", str(tmp_path / "lock"))
    monkeypatch.setattr(module, "device_exists", lambda p: True)
    aliases = {ROOT_LV: ROOT_DM}
    monkeypatch.setattr(module, "_real", lambda p: aliases.get(p, p) if p else "")
    monkeypatch.setattr(module, "tools", lambda: {"pvmove": True, "growpart": True})
    module.sysfs = sysfs
    return module


def install(helper, monkeypatch, **kwargs):
    fake = FakeSystem(helper, **kwargs)
    monkeypatch.setattr(helper, "run", fake)
    return fake


def state_with(**changes):
    state = copy.deepcopy(BASE)
    state.update(copy.deepcopy(changes))
    return state


# ─── add-disk ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["/dev/sda1", "/dev/../etc/passwd", "/dev/sdb;reboot",
                                 "sdb", "/dev/mapper/x", "/dev/sdb --force"])
def test_add_disk_rejects_anything_but_whole_disk_paths(helper, monkeypatch, bad):
    fake = install(helper, monkeypatch)
    with pytest.raises(helper.Rejected):
        helper.verb_add_disk([bad])
    assert fake.mutations() == []


def test_add_disk_joins_the_vg_behind_data_not_a_hardcoded_lv(helper, monkeypatch):
    fake = install(helper, monkeypatch)
    result = helper.verb_add_disk(["/dev/sdb"])
    assert fake.mutations() == [["pvcreate", "-y", "/dev/sdb"],
                                ["vgextend", "ubuntu-vg", "/dev/sdb"]]
    assert result["vg"] == "ubuntu-vg"
    assert not any("data-lv" in " ".join(c) for c in fake.calls)


def test_add_disk_refuses_the_system_disk(helper, monkeypatch):
    fake = install(helper, monkeypatch)
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_add_disk(["/dev/sda"])
    assert exc.value.code == "system_disk"
    assert fake.mutations() == []


def test_add_disk_requires_explicit_wipe_for_existing_data(helper, monkeypatch):
    state = state_with(wipefs={"/dev/sdb": [{"type": "xfs"}]})
    fake = install(helper, monkeypatch, state=state)
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_add_disk(["/dev/sdb"])
    assert exc.value.code == "needs_wipe"
    assert "xfs" in str(exc.value)
    assert fake.mutations() == []

    fake = install(helper, monkeypatch, state=state)
    helper.verb_add_disk(["/dev/sdb", "--wipe"])
    assert fake.mutations() == [["wipefs", "-a", "/dev/sdb"], ["pvcreate", "-y", "/dev/sdb"],
                                ["vgextend", "ubuntu-vg", "/dev/sdb"]]


def test_add_disk_with_partitions_needs_wipe_and_refuses_mounted_partitions(helper, monkeypatch):
    state = copy.deepcopy(BASE)
    state["lsblk"][1]["children"] = [{"name": "sdb1", "path": "/dev/sdb1", "type": "part"}]
    install(helper, monkeypatch, state=state)
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_add_disk(["/dev/sdb"])
    assert exc.value.code == "needs_wipe"

    state["lsblk"][1]["children"][0]["mountpoint"] = "/mnt/old"
    fake = install(helper, monkeypatch, state=state)
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_add_disk(["/dev/sdb", "--wipe"])
    assert exc.value.code in ("in_use", "system_disk")
    assert fake.mutations() == []


def test_add_disk_refuses_disk_in_another_volume_group(helper, monkeypatch):
    state = copy.deepcopy(BASE)
    state["pvs"].append({"pv_name": "/dev/sdb", "vg_name": "othervg", "pv_size": "1",
                         "pv_free": "1", "pv_used": "0", "dev_size": "1", "pv_missing": ""})
    fake = install(helper, monkeypatch, state=state)
    with pytest.raises(helper.Rejected):
        helper.verb_add_disk(["/dev/sdb", "--wipe"])
    assert fake.mutations() == []


def test_add_disk_reuses_orphan_pv_without_wiping(helper, monkeypatch):
    state = state_with(wipefs={"/dev/sdb": [{"type": "LVM2_member"}]})
    state["lsblk"][1]["fstype"] = "LVM2_member"
    state["pvs"].append({"pv_name": "/dev/sdb", "vg_name": "", "pv_size": str(50 * GIB),
                         "pv_free": str(50 * GIB), "pv_used": "0", "dev_size": str(50 * GIB),
                         "pv_missing": ""})
    fake = install(helper, monkeypatch, state=state)
    helper.verb_add_disk(["/dev/sdb"])
    assert fake.mutations() == [["vgextend", "ubuntu-vg", "/dev/sdb"]]


def test_add_disk_rolls_back_pv_when_vgextend_fails(helper, monkeypatch):
    fake = install(helper, monkeypatch, fail={"vgextend": (5, "", "metadata write failed")})
    with pytest.raises(helper.Failed):
        helper.verb_add_disk(["/dev/sdb"])
    assert fake.mutations()[-1] == ["pvremove", "-y", "/dev/sdb"]


def test_add_disk_refuses_disk_with_holders(helper, monkeypatch):
    holders = helper.sysfs / "class" / "block" / "sdb" / "holders"
    holders.mkdir(parents=True)
    (holders / "md0").write_text("")
    fake = install(helper, monkeypatch)
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_add_disk(["/dev/sdb"])
    assert exc.value.code == "in_use"
    assert fake.mutations() == []


# ─── extend ──────────────────────────────────────────────────────────────────

def _with_free(free, fstype="ext4"):
    state = copy.deepcopy(BASE)
    state["vgs"][0]["vg_free"] = str(free)
    for m in state["mounts"].values():
        m["fstype"] = fstype
    return state


def test_extend_all_grows_the_root_lv_backing_data_and_its_filesystem(helper, monkeypatch):
    fake = install(helper, monkeypatch, state=_with_free(10 * GIB))
    result = helper.verb_extend(["/data", "all"])
    extents = 10 * GIB // EXTENT
    assert fake.mutations() == [["lvextend", "-l", f"+{extents}", "ubuntu-vg/ubuntu-lv"],
                                ["resize2fs", ROOT_LV]]
    assert result["added_bytes"] == extents * EXTENT
    assert result["lv"] == "ubuntu-lv"


def test_extend_partial_rounds_down_to_whole_extents(helper, monkeypatch):
    fake = install(helper, monkeypatch, state=_with_free(10 * GIB))
    helper.verb_extend(["/data", str(GIB + EXTENT - 1)])
    assert fake.ran("lvextend") == [["lvextend", "-l", f"+{GIB // EXTENT}", "ubuntu-vg/ubuntu-lv"]]


def test_extend_uses_xfs_growfs_on_xfs(helper, monkeypatch):
    fake = install(helper, monkeypatch, state=_with_free(GIB, "xfs"))
    helper.verb_extend(["/data", "all"])
    assert fake.mutations()[-1] == ["xfs_growfs", "/"]


@pytest.mark.parametrize("mount,amount,code", [
    ("/data", str(20 * GIB), "no_space"),
    ("/data", "all", "no_space"),
    ("/var", "all", "rejected"),
    ("/data", "-5", "rejected"),
])
def test_extend_rejections_change_nothing(helper, monkeypatch, mount, amount, code):
    free = 10 * GIB if amount != "all" else 0
    fake = install(helper, monkeypatch, state=_with_free(free))
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_extend([mount, amount])
    assert exc.value.code == code
    assert fake.mutations() == []


def test_mutations_wait_for_a_running_pvmove(helper, monkeypatch):
    state = _with_free(10 * GIB)
    state["lvs"].append({"lv_name": "[pvmove0]", "vg_name": "ubuntu-vg", "lv_attr": "p-C-aom---",
                         "copy_percent": "42.00", "move_pv": "/dev/sdb"})
    fake = install(helper, monkeypatch, state=state)
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_extend(["/data", "all"])
    assert exc.value.code == "busy"
    assert fake.mutations() == []


# ─── grow-pv ─────────────────────────────────────────────────────────────────

def _partition_sysfs(helper):
    devices = helper.sysfs / "devices" / "sda"
    part = devices / "sda3"
    part.mkdir(parents=True)
    (part / "partition").write_text("3\n")
    (helper.sysfs / "class" / "block" / "sda3").symlink_to(part)


def test_grow_pv_grows_the_partition_before_the_pv(helper, monkeypatch):
    _partition_sysfs(helper)
    fake = install(helper, monkeypatch)
    result = helper.verb_grow_pv(["/dev/sda3"])
    assert fake.mutations() == [["growpart", "/dev/sda", "3"], ["pvresize", "/dev/sda3"]]
    assert result["partition_grown"] is True


def test_grow_pv_tolerates_growpart_nochange(helper, monkeypatch):
    _partition_sysfs(helper)
    fake = install(helper, monkeypatch,
                   fail={"growpart": (1, "NOCHANGE: partition 3 is size 1. it cannot be grown", "")})
    result = helper.verb_grow_pv(["/dev/sda3"])
    assert fake.ran("pvresize") == [["pvresize", "/dev/sda3"]]
    assert result["partition_grown"] is False


def test_grow_pv_refuses_pvs_outside_the_data_vg(helper, monkeypatch):
    fake = install(helper, monkeypatch)
    with pytest.raises(helper.Rejected):
        helper.verb_grow_pv(["/dev/sdb"])
    assert fake.mutations() == []


# ─── evacuate / remove ───────────────────────────────────────────────────────

def _two_disk_pool(sdb_used, sda_free):
    state = copy.deepcopy(BASE)
    state["lsblk"][1]["children"] = [{"name": "ubuntu--vg-ubuntu--lv", "type": "lvm",
                                      "mountpoint": "/"}]
    state["pvs"][0]["pv_free"] = str(sda_free)
    state["pvs"].append({"pv_name": "/dev/sdb", "vg_name": "ubuntu-vg",
                         "pv_size": str(50 * GIB), "pv_free": str(50 * GIB - sdb_used),
                         "pv_used": str(sdb_used), "dev_size": str(50 * GIB), "pv_missing": ""})
    state["vgs"][0]["pv_count"] = "2"
    return state


def test_evacuate_requires_room_on_the_other_disks(helper, monkeypatch):
    fake = install(helper, monkeypatch, state=_two_disk_pool(20 * GIB, 5 * GIB))
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_evacuate(["/dev/sdb"])
    assert exc.value.code == "no_space"
    assert fake.mutations() == []

    fake = install(helper, monkeypatch, state=_two_disk_pool(20 * GIB, 30 * GIB))
    result = helper.verb_evacuate(["/dev/sdb"])
    assert fake.mutations() == [["pvmove", "-b", "/dev/sdb"]]
    assert result["bytes_to_move"] == 20 * GIB


def test_remove_disk_only_detaches_empty_non_last_pvs(helper, monkeypatch):
    fake = install(helper, monkeypatch, state=_two_disk_pool(GIB, 30 * GIB))
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_remove_disk(["/dev/sdb"])
    assert exc.value.code == "not_empty"

    fake = install(helper, monkeypatch)
    with pytest.raises(helper.Rejected) as exc:
        helper.verb_remove_disk(["/dev/sda3"])
    assert exc.value.code == "last_pv"
    assert fake.mutations() == []

    fake = install(helper, monkeypatch, state=_two_disk_pool(0, 0))
    helper.verb_remove_disk(["/dev/sdb"])
    assert fake.mutations() == [["vgreduce", "ubuntu-vg", "/dev/sdb"],
                                ["pvremove", "-y", "/dev/sdb"]]


def test_remove_missing_never_forces(helper, monkeypatch):
    state = copy.deepcopy(BASE)
    state["vgs"][0]["vg_missing_pv_count"] = "1"
    fake = install(helper, monkeypatch, state=state)
    helper.verb_remove_missing([])
    assert fake.mutations() == [["vgreduce", "--removemissing", "ubuntu-vg"]]
    assert not any("--force" in c or "-f" in c for c in fake.mutations())


# ─── entry point ─────────────────────────────────────────────────────────────

def test_main_requires_root_and_reports_json(helper, monkeypatch, capsys):
    monkeypatch.setattr(helper.os, "geteuid", lambda: 1000)
    assert helper.main(["helper", "inventory"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "not_root"
    assert helper.main(["helper", "format-disk"]) == 2
    assert json.loads(capsys.readouterr().out)["code"] == "usage"


def test_main_maps_rejections_and_failures_to_exit_codes(helper, monkeypatch, capsys):
    monkeypatch.setattr(helper.os, "geteuid", lambda: 0)
    install(helper, monkeypatch)
    assert helper.main(["helper", "add-disk", "/dev/sda"]) == 2
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["code"] == "system_disk"

    install(helper, monkeypatch, fail={"pvcreate": (5, "", "boom")})
    assert helper.main(["helper", "add-disk", "/dev/sdb"]) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["code"] == "failed" and "boom" in out["error"]

    install(helper, monkeypatch)
    assert helper.main(["helper", "add-disk", "/dev/sdb"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is True and out["vg"] == "ubuntu-vg"


def test_inventory_survives_lvm_errors(helper, monkeypatch):
    fake = install(helper, monkeypatch, fail={"pvs": (5, "", "Couldn't find device")})
    # pvs is faked as a report command, so force a failure through run itself.
    original = fake.__call__

    def failing(args, timeout=120, check=True):
        if args[0] == "pvs":
            raise helper.Failed("pvs failed: Couldn't find device")
        return original(args, timeout, check)

    monkeypatch.setattr(helper, "run", failing)
    monkeypatch.setattr(helper, "geometry", lambda: {})
    facts = helper.inventory()
    assert facts["pvs"] == [] and facts["vgs"]
    assert any("pvs" in e for e in facts["errors"])
    assert facts["mounts"]["/data"]["source"] == ROOT_DM


def test_expand_storage_cli_delegates_to_the_volume_helper():
    script = (ROOT / "bin" / "expand-storage.sh").read_text()
    assert "/usr/local/sbin/zenplus-volume-helper" in script
    assert "data-lv" not in script
    assert "pvcreate" not in script and "lvextend" not in script


def test_setup_storage_installs_and_grants_the_volume_helper():
    setup = (ROOT / "scripts" / "setup-storage.sh").read_text()
    assert "/usr/local/sbin/zenplus-volume-helper" in setup
    assert "NOPASSWD: /usr/local/sbin/zenplus-volume-helper *" in setup


def test_pool_disk_carrying_root_lv_extents_is_not_a_system_disk(helper):
    devices = copy.deepcopy(BASE["lsblk"])
    devices[1]["children"] = [{"name": "ubuntu--vg-ubuntu--lv", "type": "lvm", "mountpoint": "/"}]
    assert helper.system_disks(devices) == {"sda"}
