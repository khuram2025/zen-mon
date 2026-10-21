"""Pure unit tests for app.services.map_topology (no database)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services import map_topology as topo

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)
A, B, C = "aaaaaaaa-0000-0000-0000-000000000001", "bbbbbbbb-0000-0000-0000-000000000002", "cccccccc-0000-0000-0000-000000000003"


def iface(idx, name, oper="up", up=True, speed=1_000_000_000, **extra):
    return {"if_index": idx, "if_name": name, "if_descr": extra.get("descr", name), "if_alias": extra.get("alias"),
            "if_speed": speed, "configured_speed_bps": None, "oper_status": oper,
            "last_up_at": NOW - timedelta(days=1) if up else None, "mac_address": extra.get("mac")}


def edge(local, lidx, lname, remote, rport, seen=NOW, conf=90, **extra):
    return {"local_device_id": local, "local_if_index": lidx, "local_if_name": lname,
            "remote_device_id": remote, "remote_port_id": rport, "remote_if_name": rport,
            "remote_chassis_id": extra.get("chassis"), "remote_hostname": extra.get("host"),
            "protocol": extra.get("protocol", "cdp"), "confidence": conf, "last_seen_at": seen,
            "metadata": extra.get("metadata", {})}


def test_norm_iface_handles_long_and_short_names():
    assert topo.norm_iface("GigabitEthernet1/0/50") == topo.norm_iface("Gi1/0/50") == "gi1/0/50"
    assert topo.norm_iface("TenGigabitEthernet1/1/1") == "te1/1/1"
    assert topo.norm_iface("TwentyFiveGigE1/0/1") == topo.norm_iface("Twe1/0/1")
    assert topo.norm_iface("FortyGigabitEthernet1/1/2") == "fo1/1/2"
    assert topo.norm_iface("TwoGigabitEthernet1/0/3") == "tw1/0/3"
    assert topo.norm_iface(None) == ""


def test_resolve_interface_is_exact_never_substring():
    ifs = [iface(50, "Gi1/0/50", descr="GigabitEthernet1/0/50"), iface(5, "Gi1/0/5")]
    assert topo.resolve_interface("GigabitEthernet1/0/50", ifs)["if_index"] == 50
    assert topo.resolve_interface("Gi1/0/5", ifs)["if_index"] == 5
    assert topo.resolve_interface("Gi1/0/9", ifs) is None
    assert topo.resolve_interface("ignored", ifs, if_index=5)["if_index"] == 5
    assert topo.resolve_interface("50", ifs)["if_index"] == 50  # numeric LLDP port id = ifIndex
    macs = [iface(7, "eth0", mac="00:11:22:33:44:55")]
    assert topo.resolve_interface("0011.2233.4455", macs)["if_index"] == 7


def test_speed_label_and_link_kind():
    assert topo.speed_label(1_000_000_000) == "1G"
    assert topo.speed_label(100_000_000) == "100M"
    assert topo.speed_label(2_500_000_000) == "2.5G"
    assert topo.speed_label(10_000_000_000) == "10G"
    assert topo.speed_label(None) is None
    assert topo.link_kind("Te1/0/1", "Gi1/0/1", None) == "fiber"
    assert topo.link_kind("Gi1/0/1", "Hu1/0/49", 1_000_000_000) == "fiber"
    assert topo.link_kind("Gi1/0/1", "Gi1/0/2", 10_000_000_000) == "fiber"
    assert topo.link_kind("Tw1/0/1", "Gi1/0/2", 2_500_000_000) == "ethernet"
    assert topo.link_kind("Gi1/0/1", "Gi1/0/2", 1_000_000_000) == "ethernet"


def test_both_sides_of_one_cable_are_one_member_with_canonical_names():
    ifs = {A: [iface(50, "Gi1/0/50")], B: [iface(1, "Te1/1/1", speed=10_000_000_000)]}
    edges = [edge(A, 50, "Gi1/0/50", B, "TenGigabitEthernet1/1/1"),
             edge(B, 1, "Te1/1/1", A, "GigabitEthernet1/0/50")]
    adj = topo.build_adjacencies(edges, ifs, {}, NOW)
    members = adj[frozenset((A, B))]
    assert len(members) == 1
    s = topo.summarize_pair(members, A)
    assert (s["src_interface"], s["dst_interface"]) == ("Gi1/0/50", "Te1/1/1")
    assert s["physical_links"] == 1 and s["stale"] is False and s["reasons"] == []
    assert s["speed_bps"] == 1_000_000_000
    flipped = topo.summarize_pair(members, B)
    assert (flipped["src_interface"], flipped["dst_interface"]) == ("Te1/1/1", "Gi1/0/50")
    md = topo.link_metadata(s)
    assert md["kind"] == "fiber" and md["shape"] == "straight" and md["speed"] == "1G"
    assert "members" not in md


def test_bundle_members_and_metadata():
    ifs = {A: [iface(1, "Gi1/0/1"), iface(2, "Gi1/0/2")], B: [iface(1, "Gi2/0/1"), iface(2, "Gi2/0/2")]}
    edges = [edge(A, 1, "Gi1/0/1", B, "GigabitEthernet2/0/1"), edge(A, 2, "Gi1/0/2", B, "GigabitEthernet2/0/2")]
    s = topo.summarize_pair(topo.build_adjacencies(edges, ifs, {}, NOW)[frozenset((A, B))], A)
    assert s["physical_links"] == 2
    assert sorted(m["src_interface"] for m in s["members"]) == ["Gi1/0/1", "Gi1/0/2"]
    md = topo.link_metadata(s)
    assert md["physical_links"] == 2 and len(md["members"]) == 2


def test_stale_reason_codes_are_oriented():
    ifs = {A: [iface(1, "Gi1/0/1", oper="down", up=False)], B: [iface(1, "Gi2/0/1", oper="down", up=True)]}
    old = NOW - timedelta(hours=30)
    adj = topo.build_adjacencies([edge(A, 1, "Gi1/0/1", B, "Gi2/0/1", seen=old)], ifs, {}, NOW)
    s = topo.summarize_pair(adj[frozenset((A, B))], A)
    assert s["stale"] is True
    assert s["reasons"] == ["old_evidence", "local_port_never_up", "remote_port_down"]
    assert topo.summarize_pair(adj[frozenset((A, B))], B)["reasons"] == \
        ["old_evidence", "local_port_down", "remote_port_never_up"]


def test_missing_port_only_counts_with_an_inventory():
    edges = [edge(A, 9, "Gi1/0/9", B, "Gi2/0/9")]
    with_inventory = topo.build_adjacencies(edges, {A: [iface(1, "Gi1/0/1")], B: [iface(9, "Gi2/0/9")]}, {}, NOW)
    assert topo.summarize_pair(with_inventory[frozenset((A, B))], A)["reasons"] == ["local_port_missing"]
    no_inventory = topo.build_adjacencies(edges, {}, {}, NOW)
    assert topo.summarize_pair(no_inventory[frozenset((A, B))], A)["stale"] is False


def test_a_fresh_member_hides_stale_ones():
    ifs = {A: [iface(1, "Gi1/0/1"), iface(2, "Gi1/0/2")], B: [iface(1, "Gi2/0/1"), iface(2, "Gi2/0/2")]}
    edges = [edge(A, 1, "Gi1/0/1", B, "Gi2/0/1", seen=NOW - timedelta(days=40)),
             edge(A, 2, "Gi1/0/2", B, "Gi2/0/2")]
    s = topo.summarize_pair(topo.build_adjacencies(edges, ifs, {}, NOW)[frozenset((A, B))], A)
    assert s["stale"] is False and s["physical_links"] == 1 and s["src_interface"] == "Gi1/0/2"
    assert topo.fresh_pairs(topo.build_adjacencies(edges, ifs, {}, NOW)) == {frozenset((A, B))}


def test_unmanaged_neighbours_fresh_only():
    ifs = [iface(3, "Gi1/0/3"), iface(4, "Gi1/0/4", oper="down", up=False)]
    edges = [
        edge(A, 3, "Gi1/0/3", None, "Gi0/1", chassis="aa:bb", host="edge-sw",
             metadata={"sys_descr": "Cisco IOS Software, Catalyst L3 Switch"}),
        edge(A, 3, "Gi1/0/3", None, "Gi0/2", chassis="cc:dd", host="old", seen=NOW - timedelta(days=3)),
        edge(A, 4, "Gi1/0/4", None, "Gi0/3", chassis="ee:ff", host="unused"),
    ]
    out = topo.unmanaged_neighbours(edges, A, ifs, NOW)
    assert [n["hostname"] for n in out] == ["edge-sw"]
    assert out[0]["icon"] == "switch" and out[0]["port_id"] == "Gi0/1" and out[0]["local_if"] == "Gi1/0/3"


def test_tiers_edge_core_access_and_unconnected():
    devices = [
        {"id": "fw", "hostname": "fw", "device_type": "firewall"},
        {"id": "core1", "hostname": "core1", "device_type": "switch"},
        {"id": "core2", "hostname": "core2", "device_type": "switch"},
        {"id": "acc1", "hostname": "acc1", "device_type": "switch"},
        {"id": "acc2", "hostname": "acc2", "device_type": "switch"},
        {"id": "ap1", "hostname": "ap1", "device_type": "access_point"},
        {"id": "lonely", "hostname": "lonely", "device_type": "printer"},
    ]
    pairs = [frozenset(p) for p in (("fw", "core1"), ("fw", "core2"), ("core1", "core2"), ("core1", "acc1"),
                                    ("core2", "acc1"), ("core1", "acc2"), ("core2", "acc2"), ("acc1", "ap1"))]
    tiers = topo.compute_tiers(devices, pairs)
    assert tiers == {"fw": 0, "core1": 1, "core2": 1, "acc1": 2, "acc2": 2, "ap1": 3, "lonely": None}
    pos, counts, _ = topo.tiered_layout(devices, pairs)
    assert counts == {"edge": 1, "core": 2, "tier2": 2, "tier3": 1, "unconnected": 1}
    assert pos["fw"][1] == round(300 / 2400 * 100, 2)
    assert pos["core1"][1] == round(750 / 2400 * 100, 2)
    assert pos["fw"][1] < pos["core1"][1] < pos["acc1"][1] < pos["ap1"][1] < pos["lonely"][1]
    assert pos["core1"][0] < pos["core2"][0]
    for x, y in pos.values():
        assert 2 <= x <= 98 and 2 <= y <= 98


def test_large_layout_wraps_and_fits_the_canvas():
    devices = [{"id": "core", "hostname": "core", "device_type": "switch"}] + [
        {"id": f"a{i:02d}", "hostname": f"a{i:02d}", "device_type": "switch"} for i in range(40)]
    pairs = [frozenset(("core", f"a{i:02d}")) for i in range(40)]
    pos, counts, _ = topo.tiered_layout(devices, pairs)
    assert counts == {"core": 1, "tier2": 40}
    assert len({y for _, y in pos.values()}) == 4  # core row + 3 wrapped sub-rows
    assert all(2 <= x <= 98 and 2 <= y <= 98 for x, y in pos.values())


def test_ring_positions_below_or_above():
    below = topo.ring_positions(50, 20, 3)
    assert len(below) == 3 and all(y > 20 for _, y in below)
    above = topo.ring_positions(50, 95, 2)
    assert all(y < 95 for _, y in above)
    assert len(topo.ring_positions(50, 20, 25)) == 25
    assert topo.ring_positions(50, 50, 0) == []


def _ep(dev, host):
    return {"kind": "device", "device_id": dev, "hostname": host}


def test_verify_link_codes():
    ifs = {A: [iface(1, "Gi1/0/1"), iface(2, "Gi1/0/2", oper="down", up=True),
               iface(3, "Gi1/0/3", oper="down", up=False), iface(4, "Gi1/0/4")],
           B: [iface(1, "Gi2/0/1"), iface(4, "Gi2/0/4")],
           C: [iface(1, "Gi3/0/1")]}
    edges = [edge(A, 1, "Gi1/0/1", B, "Gi2/0/1"),
             edge(A, 4, "Gi1/0/4", C, "Gi3/0/1"),
             edge(B, 4, "Gi2/0/4", A, "Gi1/0/9", seen=NOW - timedelta(days=5))]
    ev = topo.evidence_index(edges, ifs, {A: "a", B: "b", C: "c"}, NOW)

    def codes(src_if, dst_if, annotation=False, dst=None):
        return [(i["code"], i["severity"]) for i in topo.verify_link(
            "L", annotation, [(_ep(A, "a"), src_if), (dst or _ep(B, "b"), dst_if)], ifs, ev, NOW)]

    assert codes("Gi1/0/1", "GigabitEthernet2/0/1") == []
    assert codes(None, None) == [("unbound", "warning")]
    assert codes("Gi1/0/1", None) == [("half_bound", "warning")]
    assert codes("Gi1/0/2", "Gi2/0/1") == [("port_down", "critical")]
    assert codes("Gi1/0/3", "Gi2/0/1") == [("port_never_up", "warning")]
    assert codes("Gi9/9/9", "Gi2/0/1") == [("port_not_found", "warning")]
    assert codes("Gi1/0/4", "Gi2/0/4") == [("contradicted", "warning")]
    shape = {"kind": "shape", "device_id": None, "hostname": "cloud"}
    assert codes(None, None, annotation=True, dst=shape) == [("unbound", "info")]
    assert codes("Gi1/0/1", None, annotation=True, dst=shape) == []
    no_ev = topo.evidence_index([], ifs, {}, NOW)
    assert [i["code"] for i in topo.verify_link("L", False, [(_ep(A, "a"), "Gi1/0/1"), (_ep(B, "b"), "Gi2/0/1")],
                                                ifs, no_ev, NOW)] == ["no_evidence"]
    old_only = topo.evidence_index([edge(A, 1, "Gi1/0/1", B, "Gi2/0/1", seen=NOW - timedelta(days=3))], ifs, {}, NOW)
    assert [i["code"] for i in topo.verify_link("L", False, [(_ep(A, "a"), "Gi1/0/1"), (_ep(B, "b"), "Gi2/0/1")],
                                                ifs, old_only, NOW)] == ["stale_adjacency"]


def test_short_iface_abbreviates_unresolved_names():
    assert topo.short_iface("GigabitEthernet1/0/52") == "Gi1/0/52"
    assert topo.short_iface("TenGigabitEthernet1/1/1") == "Te1/1/1"
    assert topo.short_iface("FastEthernet0/1") == "Fa0/1"
    assert topo.short_iface("Port-channel10") == "Po10"
    assert topo.short_iface("TwentyFiveGigE1/0/1") == "Twe1/0/1"
    assert topo.short_iface("HundredGigE1/0/49") == "Hu1/0/49"
    assert topo.short_iface("GigabitEthernet 1/0/3") == "Gi1/0/3"
    assert topo.short_iface("Gi1/0/52") == "Gi1/0/52"
    assert topo.short_iface("port1") == "port1"
    assert topo.short_iface("Ethernet-Uplink") == "Ethernet-Uplink"
    assert topo.short_iface(None) is None


def test_unresolved_remote_port_is_abbreviated():
    ifs = {A: [iface(1, "Gi1/0/1")], B: [iface(9, "Gi2/0/9")]}
    adj = topo.build_adjacencies([edge(A, 1, "Gi1/0/1", B, "GigabitEthernet2/0/52")], ifs, {}, NOW)
    s = topo.summarize_pair(adj[frozenset((A, B))], A)
    assert s["dst_interface"] == "Gi2/0/52" and s["reasons"] == ["remote_port_missing"]
    out = topo.unmanaged_neighbours([edge(A, 1, "Gi1/0/1", None, "TenGigabitEthernet0/1", chassis="x", host="h")],
                                    A, ifs[A], NOW)
    assert out[0]["port_id"] == "Te0/1"


def _px(pos):
    return pos[0] / 100 * topo.CANVAS_W, pos[1] / 100 * topo.CANVAS_H


def test_free_positions_avoid_nodes_shapes_and_each_other():
    import math
    centre = (50.0, 20.0)
    straight_down = (50.0, round((480 + 320) / 2400 * 100, 2))
    assert topo.free_positions(*centre, 1) == [straight_down]
    blocker_node = straight_down
    blocker_shape = (44.0, 40.0, 12.0, 10.0)        # covers the ring below a little further out
    zone = (30.0, 10.0, 40.0, 60.0)                 # contains the centre: not an obstacle
    out = topo.free_positions(*centre, 6, [centre, blocker_node], [blocker_shape, zone])
    assert len(out) == 6
    sx0, sy0 = 44 / 100 * 4000 - 60, 40 / 100 * 2400 - 60
    sx1, sy1 = 56 / 100 * 4000 + 60, 50 / 100 * 2400 + 60
    for p in out:
        x, y = _px(p)
        assert math.hypot(x - 2000, y - 1.0 * straight_down[1] / 100 * 2400) >= 150
        assert math.hypot(x - 2000, y - 480) >= 150
        assert not (sx0 <= x <= sx1 and sy0 <= y <= sy1)
        assert 2 <= p[0] <= 98 and 2 <= p[1] <= 98
    for i, p in enumerate(out):
        for q in out[i + 1:]:
            assert math.hypot(*(a - b for a, b in zip(_px(p), _px(q)))) >= 150
    assert sum(1 for p in out if p[1] > centre[1]) >= 4  # lower half preferred


def test_free_positions_clamp_near_the_edge():
    out = topo.free_positions(97.0, 97.0, 4)
    assert len(out) == 4 and all(2 <= x <= 98 and 2 <= y <= 98 for x, y in out)
