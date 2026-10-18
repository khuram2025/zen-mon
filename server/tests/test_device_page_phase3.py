"""Device page Phase 3 contracts that need no database: template-declared
vendor tabs, alert naming from the triggering condition, the Related panel's
same-time matching and widget layout validation."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.services import alert_condition as ac
from app.services import device_insights as di
from app.services import device_layouts as dl
from app.services import device_related as dr
from app.services import device_status_machine as dsm


# ── Vendor tabs ─────────────────────────────────────────────────────────────

def _group(key, tab=..., kind="scalar"):
    g = {"key": key, "name": key.title(), "kind": kind, "metrics": [{"key": f"{key}_m", "name": "M", "type": "gauge"}]}
    if tab is not ...:
        g["tab"] = tab
    return g


def test_builtin_groups_get_their_vendor_tab():
    assert di.group_tab(_group("ipsec")) == "VPN & SD-WAN"
    assert di.group_tab(_group("sdwan")) == "VPN & SD-WAN"
    assert di.group_tab(_group("access_points")) == "Wireless"
    assert di.group_tab(_group("virtual_servers")) == "Pools & virtual servers"
    assert di.group_tab(_group("pool_members")) == "Pools & virtual servers"
    # Everything else stays in the default "<Vendor> metrics" tab; hardware has its own tab.
    for key in ("system", "cpu", "ha", "fans", "sensors", "memory"):
        assert di.group_tab(_group(key)) is None


def test_a_declared_tab_wins_and_null_opts_out():
    assert di.group_tab(_group("system", "Overview")) == "Overview"
    assert di.group_tab(_group("ipsec", "Tunnels")) == "Tunnels"
    assert di.group_tab(_group("ipsec", None)) is None
    assert di.group_tab(_group("ipsec", "  ")) is None
    assert di.group_tab(_group("ipsec", 7)) is None


def test_tab_ids_and_order():
    assert di.tab_id("VPN & SD-WAN") == "tpl-vpn-sd-wan"
    assert di.tab_id("Pools & virtual servers") == "tpl-pools-virtual-servers"
    assert di.tab_id("***") == "tpl-tab"
    groups = [{"key": "system", "tab": None}, {"key": "access_points", "tab": "Wireless"},
              {"key": "ipsec", "tab": "VPN & SD-WAN"}, {"key": "wifi", "tab": "Wireless"}]
    assert di.template_tabs(groups) == [
        {"id": "tpl-wireless", "label": "Wireless", "groups": ["access_points", "wifi"]},
        {"id": "tpl-vpn-sd-wan", "label": "VPN & SD-WAN", "groups": ["ipsec"]},
    ]


def test_shaped_groups_and_digests_carry_the_tab():
    template = [_group("system"), _group("ipsec", kind="scalar"), _group("custom", "Ops")]
    vals = [{"group_key": g["key"], "metric_key": f"{g['key']}_m", "instance": "", "series_key": "s", "label": "",
             "unit": "", "value_num": 1, "value_text": "", "updated_at": datetime.now(timezone.utc)} for g in template]
    groups, _ = di.shape_template_groups(template, vals)
    assert [g["tab"] for g in groups] == [None, "VPN & SD-WAN", "Ops"]
    assert di.group_digest(groups[1])["tab"] == "VPN & SD-WAN"
    assert [t["id"] for t in di.template_tabs(groups)] == ["tpl-vpn-sd-wan", "tpl-ops"]


# ── Alert naming (D-09) ─────────────────────────────────────────────────────

POLICY = dsm.Policy(degraded_rtt_ms=100.0, degraded_loss_pct=10.0)


def test_degraded_by_loss_is_named_after_the_loss():
    c = ac.status_condition("degraded", "up", 20.0, 33.33, POLICY)
    assert c["title"] == "Packet loss 33% (limit 10%)"
    assert (c["metric"], c["value"], c["limit"]) == ("packet_loss", 33.33, 10.0)


def test_degraded_by_latency_and_by_both():
    assert ac.status_condition("degraded", "up", 412.4, 0.0, POLICY)["title"] == "Latency 412 ms (limit 100 ms)"
    both = ac.status_condition("degraded", "up", 412.4, 12.5, POLICY)["title"]
    assert both == "Packet loss 13% (limit 10%) · latency 412 ms (limit 100 ms)"
    assert ac.status_condition("degraded", "up", 20.0, 2.5, dsm.Policy(degraded_loss_pct=2.0))["title"] == \
        "Packet loss 2.5% (limit 2%)"


def test_limits_use_the_status_machine_predicate():
    # At the limit is not over it (the canonical tolerance), so no reading is quoted.
    c = ac.status_condition("degraded", "up", 100.0, 10.0, POLICY)
    assert c["title"] == "Latency or packet loss over limits on recent checks"
    assert c["value"] is None
    # No reply: no latency is quoted.
    assert ac.status_condition("degraded", "up", None, 0.0, POLICY)["metric"] == "performance"


def test_other_transitions():
    assert ac.status_condition("down", "up", None, 100.0, POLICY)["title"] == "No response"
    assert ac.status_condition("up", "down", 3.0, 0.0, POLICY)["title"] == "Responding again"
    assert ac.status_condition("up", "degraded", 3.0, 0.0, POLICY)["title"] == "Latency and packet loss back within limits"
    assert ac.status_condition("up", "unknown", 3.0, 0.0, None)["title"] == "Up"
    assert ac.alert_message("No response", "sw", "10.0.0.1") == "No response — sw (10.0.0.1)"
    assert ac.alert_message("No response", "ap-7", None) == "No response — ap-7"


def test_device_page_titles_drop_the_repeated_host():
    assert di.alert_title("x", "Packet loss 33% (limit 10%)", "fw", "10.0.0.1") == "Packet loss 33% (limit 10%)"
    assert di.alert_title("No response — fw (10.0.0.1)", None, "fw", "10.0.0.1") == "No response"
    # Legacy rows ("<rule>: <host> (<ip>) is DEGRADED") keep the rule and the status only.
    assert di.alert_title("High Latency: fw (10.0.0.1) is DEGRADED", None, "fw", "10.0.0.1") == "High Latency · Degraded"
    assert di.alert_title("Device Down: fw (10.0.0.1) recovered — UP", None, "fw", "10.0.0.1") == "Device Down · Recovered"
    assert di.alert_title("[ZenPlus CRITICAL] Tunnel rule fired", None, "fw", "10.0.0.1") == "Tunnel rule fired"
    assert di.alert_title("High Latency: other (10.0.0.9) is DOWN", None, "fw", "10.0.0.1") == \
        "High Latency: other (10.0.0.9) is DOWN"
    assert di.alert_title(None) == "Alert"


# ── Related: same-time transitions ─────────────────────────────────────────

T0 = 1_760_000_000_000


def test_same_time_matches_within_two_minutes_only():
    own = [{"ts": T0, "old_status": "up", "new_status": "down"},
           {"ts": T0 + 3_600_000, "old_status": "down", "new_status": "up"}]
    others = [
        {"device_id": "a", "ts": T0 - 30_000, "old_status": "up", "new_status": "down"},
        {"device_id": "a", "ts": T0 + 3_600_000 + 5_000, "old_status": "down", "new_status": "up"},
        {"device_id": "b", "ts": T0 + 119_000, "old_status": "up", "new_status": "degraded"},
        {"device_id": "c", "ts": T0 + 121_000, "old_status": "up", "new_status": "down"},
    ]
    out = dr.same_time_matches(own, others)
    assert [d["device_id"] for d in out] == ["a", "b"]
    a = out[0]
    assert a["shared_transitions"] == 2 and a["closest_s"] == 5
    assert sorted(m["offset_s"] for m in a["matches"]) == [-30, 5]
    assert out[1]["matches"][0]["anchor"]["new_status"] == "down"
    assert dr.same_time_matches([], others) == []


def test_related_listing_is_worst_first():
    items = [{"hostname": "b", "status": "up"}, {"hostname": "a", "status": "up"}, {"hostname": "z", "status": "down"}]
    assert [i["hostname"] for i in dr.by_status_then_name(items)] == ["z", "a", "b"]


# ── Widget layouts ─────────────────────────────────────────────────────────

def test_template_identity_matches_the_browser_normalisation():
    assert dl.template_identity("  PALO   ALTO NETWORKS ", " Firewall ") == ("palo alto networks", "firewall")
    for vendor in ("", "unknown", "Not reported", "n/a", None):
        assert dl.template_identity(vendor, "firewall") is None
    assert dl.template_identity("Cisco", "") is None


def test_sections_and_items_are_validated():
    for ok in ("overview", "metrics", "tpl-vpn-sd-wan", "hardware", "interfaces"):
        assert dl.valid_section(ok)
    for bad in ("", "Overview", "../x", "a" * 81, "x y"):
        assert not dl.valid_section(bad)
    items = [dl.LayoutItem(id="cpu", x=11, y=0, w=6, h=4), dl.LayoutItem(id="cpu", x=0, y=2, w=4, h=4),
             dl.LayoutItem(id="gone", x=6, y=0, w=6, h=3)]
    # One entry per widget (the last wins); absent widgets keep their entry.
    assert dl.clean_items(items) == [{"id": "cpu", "x": 0, "y": 2, "w": 4, "h": 4},
                                     {"id": "gone", "x": 6, "y": 0, "w": 6, "h": 3}]
    assert dl.clean_items([dl.LayoutItem(id="w", x=11, y=0, w=6, h=4)])[0]["x"] == 6
    with pytest.raises(ValueError):
        dl.LayoutItem(id="w", x=12, y=0, w=6, h=4)
    with pytest.raises(ValueError):
        dl.LayoutWrite(scope="global", items=[])
    with pytest.raises(ValueError):
        dl.LayoutWrite(items=[{"id": f"w{i}", "x": 0, "y": 0, "w": 1, "h": 1} for i in range(101)])
