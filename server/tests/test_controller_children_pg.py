"""Regression: a FortiGate that promotes its FortiSwitches and FortiAPs gives
each child controller-derived readings on the inventory and detail pages
(SNIH, 1.24.48: blank CPU/memory, no uptime, "Uptime (24h)" mixing a
duration with a percentage).

Real schema on a disposable Postgres with every migration (migrate-171
included) and the builtin Fortinet FortiGate template; ClickHouse is faked.
Nothing here reaches an installed database or a real device."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from inventory_fleet import api_client
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures)

NOW = datetime.now(timezone.utc).replace(microsecond=0)
FGT = uuid.uuid4()
SW_SERIAL = "S248EPTF21000111"
SW2_SERIAL = "S248EPTF21000222"
AP_SERIAL = "FP231FTF22000001"
AP2_SERIAL = "FP231FTF22000002"
AP_PROFILE = "FAP231F-default"


def _s(value: str) -> str:
    return ".".join([str(len(value))] + [str(ord(c)) for c in value])


AP_OID = f"1.{_s(AP_SERIAL)}"
AP2_OID = f"1.{_s(AP2_SERIAL)}"
CLIENT_MAC = [0x00, 0x09, 0x0F, 0xAA, 0xBB, 0xCC]
AP_MAC_HEX = "00090f112233"


def _rows():
    """(group, instance, oid, metric, num, text, label) as the poller writes them."""
    fresh = []
    for inst, oid, serial, name, status, cpu, mem in (
            ("x5a000001", "1.1.1", SW_SERIAL, "SNIH-FSW-ASW01", 1, 23, 61),
            ("x5a000002", "1.1.2", SW2_SERIAL, "SNIH-FSW-ASW02", 1, 11, 40)):
        for metric, num, txt in (("fgt_sw_status", status, ""), ("fgt_sw_auth", 2, ""), ("fgt_sw_cpu", cpu, ""),
                                 ("fgt_sw_mem", mem, ""), ("fgt_sw_version", None, "S248EP-v7.4.4-build0799"),
                                 ("fgt_sw_serial", None, serial), ("fgt_sw_ip", None, "10.255.1.2")):
            fresh.append(("fortiswitch", inst, oid, metric, num, txt, name))
    for inst, oid, name, status, cpu, mem in (("x6b000001", AP_OID, "AP01", 2, 7, 41),
                                              ("x6b000002", AP2_OID, "AP02", 1, 0, 0)):
        for metric, num, txt in (("fgt_ap_status", status, ""), ("fgt_ap_cpu", cpu, ""), ("fgt_ap_mem", mem, ""),
                                 ("fgt_ap_clients", 3 if status == 2 else 0, ""), ("fgt_ap_model", None, "FAP-231F"),
                                 ("fgt_ap_uptime", 266400.0 if status == 2 else 0, ""),
                                 ("fgt_ap_session_uptime", 7200.0, ""),
                                 ("fgt_ap_sw_version", None, "FP231F-v7.4.5-build0761"),
                                 ("fgt_ap_profile", None, AP_PROFILE),
                                 ("fgt_ap_ip", None, "0a150507" if status == 2 else "0a150508"),
                                 ("fgt_ap_mac", None, AP_MAC_HEX if status == 2 else "00090f445566"),
                                 ("fgt_ap_rx_bps", 125000.0, ""), ("fgt_ap_tx_bps", 980000.0, "")):
            fresh.append(("access_points", inst, oid, metric, num, txt, name))
    # Ports: two on ASW01 (one with PoE draw), one on ASW02.
    for inst, serial, name, up, poe_w in (("x7c000001", SW_SERIAL, "port1", 1, 6.2),
                                          ("x7c000002", SW_SERIAL, "port2", 0, None),
                                          ("x7c000003", SW2_SERIAL, "port1", 1, None)):
        fresh += [("fsw_ports", inst, f"1.1.9.{inst[-1]}", m, n, t, name) for m, n, t in (
            ("fgt_swp_serial", None, serial), ("fgt_swp_status", up, ""), ("fgt_swp_speed", None, "1000full"),
            ("fgt_swp_native_vlan", None, "10"), ("fgt_swp_allowed_vlans", None, "10,20,30"),
            ("fgt_swp_poe_capable", None, "1"), ("fgt_swp_poe_status", None, "1"),
            ("fgt_swp_poe_state", None, "Delivering Power" if poe_w else "Searching"))]
        if poe_w is not None:
            fresh.append(("fsw_ports", inst, f"1.1.9.{inst[-1]}", "fgt_swp_poe_power", poe_w, "", name))
    for rid, channel, clients in ((1, 6, 1), (2, 44, 2)):
        oid = f"{AP_OID}.{rid}"
        fresh += [("fap_radios", f"x8d00000{rid}", oid, m, n, t, oid) for m, n, t in (
            ("fgt_apr_mode", 3, ""), ("fgt_apr_channel", channel, ""), ("fgt_apr_power", 17, ""),
            ("fgt_apr_clients", clients, ""), ("fgt_apr_bssid", None, f"00090f1122{rid:02x}"))]
        fresh += [("fap_ssids", f"x9e00000{rid}", f"{oid}.{24 + rid}", m, n, "", "SNIH-Staff") for m, n in (
            ("fgt_vap_clients", clients), ("fgt_vap_rx_bps", 1000.0 * rid))]
        fresh += [("fap_radio_profiles", f"xaf00000{rid}", f"1.{_s(AP_PROFILE)}.{rid}", m, None, t, "") for m, t in (
            ("fgt_apf_band", "13" if rid == 1 else "14"), ("fgt_apf_width", "1" if rid == 1 else "3"))]
    mac = ".".join(str(b) for b in CLIENT_MAC)
    fresh += [("fap_clients", "xb0000001", f"1.26.6.{mac}", m, None, t, "") for m, t in (
        ("fgt_sta_wtp", AP_SERIAL), ("fgt_sta_radio", "2"), ("fgt_sta_ip", "0a1e0063"), ("fgt_sta_host", "nurse-tab-07"),
        ("fgt_sta_user", "j.doe"), ("fgt_sta_signal", "-58"), ("fgt_sta_noise", "-95"), ("fgt_sta_channel", "44"))]
    return fresh


@pytest.fixture(scope="module")
def world(pg_db_module):
    db = pg_db_module
    profile = db.scalar("SELECT id FROM device_profiles WHERE builtin AND name = 'Fortinet FortiGate'")
    assert profile is not None, "builtin Fortinet template missing from the migration set"
    db.execute("INSERT INTO devices (id, hostname, ip_address, tags, status, ping_enabled, ping_interval, snmp_enabled,"
               " snmp_poll_interval, device_type, vendor, model, profile_id, promote_managed)"
               " VALUES ($1, 'SNIH-FGT201F', '10.21.0.1'::inet, '[\"snih\"]'::jsonb, 'up', true, 60, true, 60,"
               " 'firewall', 'Fortinet', 'FortiGate-201F', $2, true)", FGT, profile)
    for group, inst, oid, metric, num, txt, label in _rows():
        db.execute("INSERT INTO device_template_values (device_id, group_key, metric_key, instance, series_key, label,"
                   " value_num, value_text, updated_at, instance_oid)"
                   " VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)",
                   FGT, group, metric, inst, f"tpl_{metric}_{inst}", label, num, txt, NOW - timedelta(seconds=40), oid)
    return db


def _session(db):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.pool import NullPool
    engine = create_async_engine(db.url, poolclass=NullPool)
    return engine, AsyncSession(engine, expire_on_commit=False)


async def _sync(db):
    from app.services.managed_device_service import sync_all_controllers
    engine, session = _session(db)
    try:
        async with session:
            summary = await sync_all_controllers(session, only_controller=FGT)
            await session.commit()
    finally:
        await engine.dispose()
    return summary


@pytest.fixture(scope="module")
def promoted(world):
    asyncio.run(_sync(world))
    # Device Tracker sees AP01's MAC directly on ASW01 port1 (controller-collected MAC table).
    sw = uuid.uuid4()
    world.execute("INSERT INTO udt_managed_switches (id, controller_id, serial, hostname, instance)"
                  " VALUES ($1, $2, $3, 'SNIH-FSW-ASW01', 'x5a000001')", sw, FGT, SW_SERIAL)
    world.execute("INSERT INTO udt_managed_ports (switch_id, port_number, if_name, oper_status) VALUES ($1, 1, 'port1', 'up')", sw)
    ep = uuid.uuid4()
    world.execute("INSERT INTO udt_endpoints (id, mac) VALUES ($1, '00:09:0f:11:22:33')", ep)
    world.execute("INSERT INTO udt_managed_locations (endpoint_id, switch_id, port_number, is_direct) VALUES ($1, $2, 1, true)",
                  ep, sw)
    ids = {r[0]: r[1] for r in world.query("SELECT hostname, id FROM devices WHERE managed_by_device_id = $1", FGT)}
    return ids


@pytest.fixture(autouse=True)
def fake_clickhouse(monkeypatch):
    from app.core import database
    from app.services import controller_children, metric_service
    empty = lambda: SimpleNamespace(query=lambda *a, **k: SimpleNamespace(result_rows=[]))  # noqa: E731
    monkeypatch.setattr(database, "get_clickhouse_client", empty)
    monkeypatch.setattr(metric_service, "get_clickhouse_client", empty, raising=False)
    monkeypatch.setattr(controller_children, "_ch_rows", lambda sql, params: [])


def test_migration_extends_the_builtin_fortinet_template(world):
    groups = world.scalar("SELECT oid_groups FROM device_profiles WHERE builtin AND name = 'Fortinet FortiGate'")
    by_key = {g["key"]: g for g in groups}
    for key in ("fsw_ports", "fap_radios", "fap_ssids", "fap_radio_profiles", "fap_clients"):
        assert by_key[key]["display"] == "children", key
    ap = by_key["access_points"]
    assert {"cpu_key": "fgt_ap_cpu", "uptime_key": "fgt_ap_uptime"}.items() <= ap["children"].items()
    assert ap["children"]["status_key"] == "fgt_ap_status"                      # the 069 mapping is kept
    assert {"fgt_ap_uptime", "fgt_ap_ip", "fgt_ap_mac"} <= {m["key"] for m in ap["metrics"]}
    assert by_key["fortiswitch"]["children"]["serial_key"] == "fgt_sw_serial"
    # Client identity never becomes a ClickHouse series.
    assert all(m["type"] == "string" for m in by_key["fap_clients"]["metrics"])
    assert world.scalar("SELECT count(*) FROM information_schema.columns WHERE table_name = 'device_template_values'"
                        " AND column_name = 'instance_oid'") == 1


def test_promotion_creates_children_with_controller_identity(promoted, world):
    rows = {r[0]: r for r in world.query(
        "SELECT hostname, poll_mode, ping_enabled, snmp_enabled, serial_number, host(managed_ip), device_type, status"
        " FROM devices WHERE managed_by_device_id = $1", FGT)}
    assert set(rows) == {"SNIH-FSW-ASW01", "SNIH-FSW-ASW02", "AP01", "AP02"}
    sw, ap = rows["SNIH-FSW-ASW01"], rows["AP01"]
    assert sw[1:5] == ("via_controller", False, False, SW_SERIAL) and sw[5] == "10.255.1.2"
    assert ap[4] == AP_SERIAL and ap[5] == "10.21.5.7" and ap[6] == "access_point"   # serial from the row index
    assert world.scalar("SELECT count(*) FROM topology_dependencies WHERE parent_device_id = $1"
                        " AND dependency_type = 'controller' AND suppress_alerts", FGT) == 4
    # Promotion uses no license seat: the enforced node count leaves children out.
    from app.services.license_capacity import NODE_COUNT_SQL
    assert "IS DISTINCT FROM 'via_controller'" in NODE_COUNT_SQL


def test_switch_detail_reads_controller_values(promoted, world):
    sw = promoted["SNIH-FSW-ASW01"]
    with api_client(world, role="t_cc_view", permissions=["devices.view"]) as c:
        body = c.get(f"/api/v1/devices/{sw}/controller-readings").json()
    assert body["managed"] is True and body["kind"] == "switch"
    assert body["controller"]["hostname"] == "SNIH-FGT201F"
    assert body["stale"] is False and body["measured_at"]
    r = body["readings"]
    assert (r["cpu"], r["memory"], r["serial"]) == (23, 61, SW_SERIAL)
    assert r["authorization"]["text"] == "Authorized" and r["firmware"] == "S248EP-v7.4.4-build0799"
    assert r["uptime_s"] is None                       # the MIB has no switch boot time…
    assert "boot_uptime" in {u["metric"] for u in body["not_supported"]}   # …and says so
    panel = body["panel"]
    assert [p["name"] for p in panel["ports"]] == ["port1", "port2"]          # only this switch's ports
    assert panel["summary"] == {"ports": 2, "up": 1, "poe_capable": 2, "poe_draw_w": 6.2, "poe_budget_w": None}
    port1 = panel["ports"][0]
    assert port1["speed"] == "1000full" and port1["allowed_vlans"] == "10,20,30" and port1["poe"]["draw_w"] == 6.2
    assert [d["hostname"] for d in port1["connected_devices"]] == ["AP01"]   # via Device Tracker MAC table
    assert {"port_traffic", "lldp", "lag", "poe_budget"} <= {u["metric"] for u in body["not_supported"]}


def test_ap_detail_reads_radios_ssids_and_guards_client_identity(promoted, world):
    ap = promoted["AP01"]
    with api_client(world, role="t_cc_view", permissions=["devices.view"]) as c:
        body = c.get(f"/api/v1/devices/{ap}/controller-readings").json()
    r = body["readings"]
    assert (r["cpu"], r["memory"], r["clients"], r["uptime_s"]) == (7, 41, 3, 266400.0)
    assert r["state"]["text"] == "Online" and r["ip"] == "10.21.5.7"
    panel = body["panel"]
    radios = {x["radio_id"]: x for x in panel["radios"]}
    assert (radios[1]["band"], radios[1]["channel"], radios[1]["width"]) == ("2.4 GHz", 6, "20 MHz")
    assert (radios[2]["band"], radios[2]["phy"], radios[2]["width"]) == ("5 GHz", "802.11ax/ac/n/a", "80 MHz")
    assert radios[2]["signal"]["avg_dbm"] == -58
    assert {(s["ssid"], s["radio_id"]) for s in panel["ssids"]} == {("SNIH-Staff", 1), ("SNIH-Staff", 2)}
    assert panel["clients_restricted"] is True
    assert panel["clients"] and "mac" not in panel["clients"][0] and "user" not in panel["clients"][0]
    assert panel["uplink"]["switch_ports"][0]["switch"]["hostname"] == "SNIH-FSW-ASW01"

    with api_client(world, role="t_cc_udt", permissions=["devices.view", "udt.view"]) as c:
        client = c.get(f"/api/v1/devices/{ap}/controller-readings").json()["panel"]["clients"][0]
    assert (client["mac"], client["ip"], client["host"], client["ssid"]) == (
        "00:09:0f:aa:bb:cc", "10.30.0.99", "nurse-tab-07", "SNIH-Staff")
    assert "user" not in client                                            # user names need udt.view_users
    with api_client(world, role="t_cc_users", permissions=["devices.view", "udt.view", "udt.view_users"]) as c:
        assert c.get(f"/api/v1/devices/{ap}/controller-readings").json()["panel"]["clients"][0]["user"] == "j.doe"


def test_offline_ap_reports_no_readings_instead_of_zero(promoted, world):
    with api_client(world, role="t_cc_view", permissions=["devices.view"]) as c:
        body = c.get(f"/api/v1/devices/{promoted['AP02']}/controller-readings").json()
    r = body["readings"]
    assert r["offline"] is True and (r["cpu"], r["memory"], r["clients"], r["uptime_s"]) == (None, None, None, None)
    assert body["panel"]["radios"] == []


def test_fanout_ties_readings_to_each_child(promoted, world, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool
    from app.services import managed_device_service as mds
    engine = create_async_engine(world.url, poolclass=NullPool)
    monkeypatch.setattr(mds, "AsyncSessionLocal", async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(mds, "_latest_fanout", lambda ids: {})
    written: list = []
    monkeypatch.setattr(mds, "_insert_fanout", lambda rows: written.extend(rows))
    mds._FANOUT_LAST.clear()
    try:
        assert asyncio.run(mds.fanout_child_readings()) == len(written) > 0
        assert asyncio.run(mds.fanout_child_readings()) == 0                # nothing new: nothing written twice
    finally:
        asyncio.run(engine.dispose())
        mds._FANOUT_LAST.clear()
    by_child: dict = {}
    for did, key, value, _unit, _ts, poller in written:
        assert poller == f"controller:{FGT}"
        by_child.setdefault(did, {})[key] = value
    assert by_child[str(promoted["SNIH-FSW-ASW01"])] == {"cpu": 23, "memory": 61}
    assert by_child[str(promoted["AP01"])] == {"cpu": 7, "memory": 41, "uptime": 266400.0}
    assert str(promoted["AP02"]) not in by_child                           # offline: no fake zeros


def test_inventory_list_marks_controller_readings_and_availability(promoted, world, monkeypatch):
    from app.core import database
    from app.services import controller_children
    sw, ap = str(promoted["SNIH-FSW-ASW01"]), str(promoted["AP01"])
    recent = (NOW - timedelta(seconds=40)).replace(tzinfo=None)
    metric_rows = [(sw, "cpu", 23.0, recent), (sw, "memory", 61.0, recent), (ap, "uptime", 266400.0, recent)]
    fake = SimpleNamespace(query=lambda sql, parameters=None: SimpleNamespace(
        result_rows=metric_rows if "INTERVAL 15 MINUTE" in sql else []))
    monkeypatch.setattr(database, "get_clickhouse_client", lambda: fake)

    since = NOW - timedelta(hours=24)

    def status_rows(sql, params):
        start = int(since.timestamp()) // 300 * 300
        rows = []
        for i in range(288):
            b = start + i * 300
            rows.append((f"tpl_fgt_sw_status_x5a000001", b, [1.0] if i else [0.0]))   # one 5-min bucket down
            if i % 2 == 0:
                rows.append((f"tpl_fgt_ap_status_x6b000001", b, [2.0]))          # only half the window observed
        return rows
    monkeypatch.setattr(controller_children, "_ch_rows", status_rows)

    with api_client(world, role="t_cc_view", permissions=["devices.view"]) as c:
        metrics = c.get("/api/v1/devices/current-metrics").json()["devices"]
        stats = c.get("/api/v1/devices/dashboard/uptime-stats", params={"hours": 24}).json()
    assert metrics[sw]["cpu"] == 23 and metrics[sw]["_source"] == "controller"
    assert metrics[sw]["_controller_id"] == str(FGT) and metrics[sw]["_stale"] is False
    assert metrics[ap]["uptime"] == 266400.0
    assert stats["controller"][sw]["sufficient"] is True and stats["devices"][sw] == round(287 / 288 * 100, 2)
    # Half the window observed: insufficient history, no percentage invented.
    assert stats["controller"][ap]["sufficient"] is False and ap not in stats["devices"]
    assert 45 <= stats["controller"][ap]["coverage_pct"] <= 55


def test_related_panel_links_controller_peers_and_neighbours(promoted, world):
    with api_client(world, role="t_cc_view", permissions=["devices.view"]) as c:
        ap = c.get(f"/api/v1/devices/{promoted['AP01']}/related").json()
        sw = c.get(f"/api/v1/devices/{promoted['SNIH-FSW-ASW01']}/related").json()
    assert ap["parent"]["hostname"] == "SNIH-FGT201F" and ap["parent"]["model"] == "FortiGate-201F"
    assert ap["peers"]["total"] == 3
    assert {p["hostname"] for p in ap["peers"]["items"]} == {"SNIH-FSW-ASW01", "SNIH-FSW-ASW02", "AP02"}
    assert all(p["observed_via"] == "controller" for p in ap["peers"]["items"])
    assert [(n["hostname"], n["direction"], n["port"]) for n in ap["controller_neighbours"]] == [
        ("SNIH-FSW-ASW01", "upstream", "port1")]
    assert [(n["hostname"], n["direction"]) for n in sw["controller_neighbours"]] == [("AP01", "downstream")]
    assert [d["dependency_type"] for d in ap["dependencies"]["upstream"]] == ["controller"]


def test_controller_page_hides_child_source_groups(promoted, world):
    with api_client(world, role="t_cc_view", permissions=["devices.view"]) as c:
        insights = c.get(f"/api/v1/devices/{FGT}/template-insights").json()
        summary = c.get(f"/api/v1/devices/{FGT}/summary").json()
        direct = c.get(f"/api/v1/devices/{FGT}/controller-readings").json()
    keys = {g["key"] for g in insights["groups"]}
    assert {"access_points", "fortiswitch"} <= keys
    assert not keys & {"fsw_ports", "fap_radios", "fap_ssids", "fap_radio_profiles", "fap_clients"}
    assert not {g["key"] for g in summary["health"]["groups"]} & {"fsw_ports", "fap_clients"}
    ap_rows = next(g for g in insights["groups"] if g["key"] == "access_points")["rows"]
    ip_text = {r["label"]: r["cells"]["fgt_ap_ip"]["text"] for r in ap_rows}
    assert ip_text["AP01"] == "10.21.5.7"                                  # hex InetAddress shown as an IP
    assert direct == {"device_id": str(FGT), "managed": False}


def test_child_history_comes_from_the_controller_series(promoted, world, monkeypatch):
    from app.services import controller_children
    seen: dict = {}

    def rows(sql, params):
        seen.update(params)
        b = int((NOW - timedelta(hours=1)).timestamp()) // 300 * 300
        return [("tpl_fgt_sw_cpu_x5a000001", b, 22.0), ("tpl_fgt_swp_poe_power_x7c000001", b, 6.0)]
    monkeypatch.setattr(controller_children, "_ch_rows", rows)
    with api_client(world, role="t_cc_view", permissions=["devices.view"]) as c:
        body = c.get(f"/api/v1/devices/{promoted['SNIH-FSW-ASW01']}/controller-history", params={"hours": 24}).json()
    assert seen["cid"] == str(FGT)                                         # the controller's own series
    series = {s["key"]: s["points"] for s in body["series"]}
    assert series["cpu"][0]["value"] == 22.0 and series["poe_draw_w"][0]["value"] == 6.0
    assert "tpl_fgt_swp_poe_power_x7c000003" not in seen["keys"]           # ASW02's port is not ASW01's history


def test_child_cpu_rule_is_evaluated_for_the_child(promoted, world):
    from app.services.network_alert_service import _snmp_devices
    engine, session = _session(world)

    async def go():
        try:
            async with session:
                return await _snmp_devices(session)
        finally:
            await engine.dispose()
    devices = asyncio.run(go())
    ap = devices[str(promoted["AP01"])]
    assert ap["poll_interval"] == 60 and ap["ip"] == "10.21.5.7"
    assert str(FGT) in devices
