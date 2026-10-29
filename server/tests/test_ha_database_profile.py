"""Deployment guardrails; runtime behavior is exercised in disposable VMware nodes."""
import copy
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("ha_database_profile", Path(__file__).resolve().parents[2] / "appliance/ha/database_profile.py")
profile = importlib.util.module_from_spec(spec)
spec.loader.exec_module(profile)


def inventory():
    return {"cluster": "test-cell", "profile": "strict", "nodes": [
        {"name": f"node-{i}", "address": f"192.0.2.{i}", "data": True} for i in range(1, 4)]}


def passwords():
    return {"superuser": "s" * 32, "replication": "r" * 32, "rewind": "w" * 32}


def test_strict_requires_sync_copy_and_watchdog_and_tls():
    files = profile.render(inventory(), "node-1", passwords())
    patroni = files["patroni.json"]
    dcs = patroni["bootstrap"]["dcs"]
    assert dcs["synchronous_mode"] and dcs["synchronous_mode_strict"]
    assert dcs["synchronous_node_count"] == 1 and not dcs["failsafe_mode"]
    assert dcs["check_timeline"] and dcs["maximum_lag_on_failover"] == 0
    assert patroni["watchdog"] == {"mode": "required", "device": "/dev/watchdog", "safety_margin": -1}
    assert patroni["restapi"]["verify_client"] == "required"
    assert patroni["etcd3"]["protocol"] == "https"
    assert files["etcd.json"]["peer-transport-security"]["client-cert-auth"]
    assert files["etcd.json"]["client-transport-security"]["client-cert-auth"]
    assert "trust" not in " ".join(patroni["postgresql"]["pg_hba"])
    assert not files["profile.json"]["controller_ha_qualified"]
    assert not files["profile.json"]["lab_only"]
    assert patroni["postgresql"]["pgpass"] == "/var/lib/zenplus-ha/patroni/pgpass"


def test_economy_witness_has_no_database_or_passwords():
    inv = inventory()
    inv["profile"] = "economy"
    inv["nodes"][2]["data"] = False
    witness = profile.render(inv, "node-3", passwords())
    assert "patroni.json" not in witness
    assert all(p not in json.dumps(witness) for p in passwords().values())
    active = profile.render(inv, "node-1", passwords())
    assert active["profile.json"]["protection"] == "single-copy-possible"
    assert not active["patroni.json"]["bootstrap"]["dcs"]["synchronous_mode_strict"]


@pytest.mark.parametrize("change", [
    lambda x: x.update(profile="async"),
    lambda x: x.update(cluster="x\ncommand"),
    lambda x: x["nodes"].pop(),
    lambda x: x["nodes"][0].update(data=False),
    lambda x: x["nodes"][0].update(data="true"),
    lambda x: x["nodes"][0].update(address="0.0.0.0"),
    lambda x: x["nodes"][0].update(address="127.0.0.1"),
    lambda x: x["nodes"][0].update(address="224.0.0.1"),
    lambda x: x["nodes"][0].update(address="192.0.2.2"),
    lambda x: x["nodes"][0].update(name="node-2"),
    lambda x: x["nodes"][0].update(extra="ignored-dangerously"),
])
def test_reject_invalid_topology(change):
    inv = inventory()
    change(inv)
    with pytest.raises(ValueError):
        profile.render(inv, "node-1", passwords())


def test_no_member_or_weak_shared_passwords():
    with pytest.raises(ValueError):
        profile.render(inventory(), "absent", passwords())
    for creds in ({"superuser": "weak"}, dict.fromkeys(passwords(), "x" * 32)):
        with pytest.raises(ValueError):
            profile.render(inventory(), "node-1", creds)


def test_lab_downgrade_is_explicit_and_persisted():
    files = profile.render(inventory(), "node-1", passwords(), lab=True)
    assert files["patroni.json"]["watchdog"]["mode"] == "off"
    assert files["profile.json"]["lab_only"]


def test_never_overwrite_existing_cell(tmp_path):
    out = tmp_path / "new-cell"
    files = profile.render(inventory(), "node-1", passwords())
    before = copy.deepcopy(files)
    profile.write_new(out, files)
    with pytest.raises(FileExistsError):
        profile.write_new(out, files)
    assert json.loads((out / "patroni.json").read_text()) == before["patroni.json"]
