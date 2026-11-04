"""Controller-derived readings of managed children: index/hex decoding,
staleness, offline blanking, availability coverage and the fan-out rows
(app.services.controller_children). No database, no ClickHouse."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services import controller_children as cc

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=timezone.utc)


def _oid_str(value: str) -> str:
    return ".".join([str(len(value))] + [str(ord(c)) for c in value])


def test_decode_index_layouts():
    ap = f"1.{_oid_str('FP231FTF22000001')}"
    assert cc.decode_index(ap, ["int", "str"]) == [1, "FP231FTF22000001"]
    assert cc.decode_index(ap + ".2", ["int", "str", "int"]) == [1, "FP231FTF22000001", 2]
    # Station index {vdom, ifIndex, mac}: length-prefixed and fixed MAC forms.
    assert cc.decode_index("1.25.6.0.9.15.170.187.204", ["int", "int", "mac"]) == [1, 25, "00:09:0f:aa:bb:cc"]
    assert cc.decode_index("1.25.0.9.15.170.187.204", ["int", "int", "mac"]) == [1, 25, "00:09:0f:aa:bb:cc"]
    # A suffix that does not fit the layout is rejected, never half-decoded.
    assert cc.decode_index(ap + ".2.9", ["int", "str", "int"]) is None
    assert cc.decode_index("1.40.65", ["int", "str"]) is None
    assert cc.decode_index(None, ["int"]) is None


def test_hex_columns_decode_to_ip_and_mac():
    assert cc.hex_to_ip("0a150507") == "10.21.5.7"
    assert cc.hex_to_ip("10.21.0.13") == "10.21.0.13"          # IpAddress columns arrive as text
    assert cc.hex_to_ip("00000000") is None                    # unspecified is not an address
    assert cc.hex_to_ip("zz") is None
    assert cc.hex_to_mac("00090faabbcc") == "00:09:0f:aa:bb:cc"
    assert cc.hex_to_mac("000000000000") is None
    assert cc.display_hex("0a150507") == "10.21.5.7"
    assert cc.display_hex("00090faabbcc") == "00:09:0f:aa:bb:cc"


AP_GROUP = {
    "key": "access_points", "kind": "table", "interval_seconds": 0,
    "metrics": [{"key": "fgt_ap_status", "type": "enum", "labels": {"1": {"text": "Offline", "sev": "crit"},
                                                                    "2": {"text": "Online", "sev": "ok"}}}],
    "children": {"status_key": "fgt_ap_status", "status_map": {"2": "up", "1": "down", "0": "degraded"},
                 "cpu_key": "fgt_ap_cpu", "memory_key": "fgt_ap_mem", "uptime_key": "fgt_ap_uptime",
                 "clients_key": "fgt_ap_clients", "state_key": "fgt_ap_status", "ip_key": "fgt_ap_ip",
                 "mac_key": "fgt_ap_mac", "serial_from_index": {"layout": ["int", "str"], "part": 1}},
}


def _ctx(cells: dict, measured_at: datetime, oid: str | None = None) -> cc.ChildContext:
    ctx = cc.ChildContext(
        child={"id": "c1", "managed_source": "access_points", "managed_instance": "x1", "serial_number": None,
               "model": None, "os_version": None, "device_type": "access_point"},
        controller={"id": "p1", "hostname": "SNIH-FGT", "status": "up", "snmp_poll_interval": 60},
        groups=[AP_GROUP], group=AP_GROUP, spec=AP_GROUP["children"], instance_oid=oid, measured_at=measured_at)
    for key, (num, txt) in cells.items():
        ctx.cells[key] = cc.Cell(num, txt, measured_at, cc.series_key(key, "x1"))
    return ctx


def test_readings_carry_controller_time_and_staleness():
    oid = f"1.{_oid_str('FP231FTF22000001')}"
    ctx = _ctx({"fgt_ap_status": (2, ""), "fgt_ap_cpu": (7, ""), "fgt_ap_mem": (41, ""),
                "fgt_ap_uptime": (86400.5, ""), "fgt_ap_clients": (12, ""),
                "fgt_ap_ip": (None, "0a150507"), "fgt_ap_mac": (None, "00090faabbcc")}, NOW - timedelta(seconds=50), oid)
    out = cc.readings(ctx, NOW)
    r = out["readings"]
    assert (r["cpu"], r["memory"], r["uptime_s"], r["clients"]) == (7, 41, 86400.5, 12)
    assert (r["serial"], r["ip"], r["mac"]) == ("FP231FTF22000001", "10.21.5.7", "00:09:0f:aa:bb:cc")
    assert r["state"]["text"] == "Online" and r["state"]["status"] == "up"
    assert out["controller"]["hostname"] == "SNIH-FGT"
    assert out["stale"] is False and out["stale_after_s"] == 600 and out["age_s"] == 50

    old = _ctx({"fgt_ap_status": (2, ""), "fgt_ap_cpu": (7, "")}, NOW - timedelta(minutes=11))
    assert cc.readings(old, NOW)["stale"] is True


def test_offline_child_blanks_gauges_instead_of_zero():
    ctx = _ctx({"fgt_ap_status": (1, ""), "fgt_ap_cpu": (0, ""), "fgt_ap_mem": (0, ""), "fgt_ap_clients": (0, "")},
               NOW - timedelta(seconds=30))
    r = cc.readings(ctx, NOW)["readings"]
    assert r["offline"] is True and r["offline_text"] == cc.OFFLINE_TEXT
    assert (r["cpu"], r["memory"], r["clients"]) == (None, None, None)


def test_fanout_writes_fresh_readings_once():
    ctx = _ctx({"fgt_ap_status": (2, ""), "fgt_ap_cpu": (7, ""), "fgt_ap_mem": (41, ""), "fgt_ap_uptime": (100, "")},
               NOW - timedelta(seconds=20))
    last: dict = {}
    rows = cc.fanout_rows([ctx], last, NOW)
    assert sorted(r[1] for r in rows) == ["cpu", "memory", "uptime"]
    assert all(r[0] == "c1" and r[5] == "controller:p1" for r in rows)
    assert cc.fanout_rows([ctx], last, NOW) == []                       # same measurement: nothing new
    stale = _ctx({"fgt_ap_status": (2, ""), "fgt_ap_cpu": (7, "")}, NOW - timedelta(minutes=30))
    assert cc.fanout_rows([stale], {}, NOW) == []                       # a controller outage leaves a gap
    offline = _ctx({"fgt_ap_status": (1, ""), "fgt_ap_cpu": (0, "")}, NOW - timedelta(seconds=20))
    assert cc.fanout_rows([offline], {}, NOW) == []


def test_availability_needs_coverage_and_skips_maintenance():
    since, until = NOW - timedelta(hours=1), NOW
    start = int(since.timestamp()) // 300 * 300
    full = {start + i * 300: (5, 5) for i in range(12)}
    full[start] = (0, 5)                                                 # one bucket down
    res = cc.availability_from_buckets(full, since, until)
    assert res["sufficient"] and res["pct"] == round(55 / 60 * 100, 2) and res["coverage_pct"] == 100.0

    half = {b: v for i, (b, v) in enumerate(sorted(full.items())) if i % 2 == 0}
    short = cc.availability_from_buckets(half, since, until)
    assert short["sufficient"] is False and short["pct"] is None and short["coverage_pct"] == 50.0

    # The missing half was planned maintenance: excluded from the expectation too.
    windows = [(datetime.fromtimestamp(b, tz=timezone.utc), datetime.fromtimestamp(b + 300, tz=timezone.utc))
               for i, b in enumerate(sorted(full)) if i % 2 == 1]
    assert cc.availability_from_buckets(half, since, until, windows)["sufficient"] is True
    assert cc.availability_from_buckets({}, since, until)["pct"] is None


def test_status_codes_count_degraded_as_available():
    available, known = cc.status_codes(AP_GROUP["children"])
    assert sorted(available) == [0.0, 2.0] and sorted(known) == [0.0, 1.0, 2.0]


def test_hidden_groups_and_insights_skip_them():
    from app.services.device_insights import shape_template_groups
    groups = [AP_GROUP, {"key": "fap_clients", "kind": "table", "display": "children", "metrics": []}]
    assert cc.hidden_group_keys(groups) == ["fap_clients"]
    shaped, _ = shape_template_groups(groups, [])
    assert [g["key"] for g in shaped] == ["access_points"]


def test_hex_metric_display_on_the_controller_page():
    from app.services.device_insights import metric_display
    assert metric_display({"format": "hex"}, None, "0a150507") == "10.21.5.7"


def test_switch_join_time_is_only_shown_as_an_epoch():
    assert cc._joined_at(cc.Cell(None, "1761900000", None, None)).startswith("2025-")
    assert cc._joined_at(cc.Cell(None, "86400", None, None)) is None
