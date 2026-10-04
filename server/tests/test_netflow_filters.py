"""NetFlow filter model (contract §8): parsing and validation, compilation per
table (golden SQL fragments and parameter binding), visibility injection
(NF-04) and the legacy scope adapters. The opt-in ClickHouse layer executes the
compiled predicates of every table against the real v2 schema.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.api.v1.netflow import common as C  # noqa: E402
from app.api.v1.netflow import filters as F  # noqa: E402
from app.api.v1.netflow import sql as S  # noqa: E402
from app.api.v1.netflow.access import FlowVisibility  # noqa: E402
from app.api.v1.netflow.common import NetflowError, QueryMeta, _META  # noqa: E402
from app.services import netflow_groups as G  # noqa: E402


@pytest.fixture(autouse=True)
def groups():
    G.set_groups_for_tests([
        G.Group(id="g1", name="DC A", cidrs=(G.parse_network("10.20.0.0/16"), G.parse_network("2001:db8:20::/48"))),
    ])
    yield
    G.set_groups_for_tests([])


@pytest.fixture
def meta():
    m = QueryMeta()
    token = _META.set(m)
    yield m
    _META.reset(token)


def _bad(query: dict) -> NetflowError:
    with pytest.raises(NetflowError) as exc:
        F.parse_flow_filter(query)
    assert exc.value.status_code == 400
    assert isinstance(exc.value.detail, str)
    return exc.value


VIS_ALL = F.UNRESTRICTED
VIS_SCOPED = FlowVisibility(restricted=True, exporters=frozenset({"10.9.0.1", "2001:db8:ffff::1"}),
                            user_id="u1", permissions=frozenset({"netflow.view"}))
VIS_NONE = FlowVisibility(restricted=True, exporters=frozenset(), user_id="u2", permissions=frozenset({"netflow.view"}))


# ── Parsing ───────────────────────────────────────────────────────────────────

FULL = {
    "exporter": "10.9.0.1", "iface": "70000@10.9.0.1", "iface_dir": "in", "talker": "10.0.0.0/8",
    "src": "2001:db8::/32", "dst": "192.0.2.5", "not_talker": "8.8.8.8", "not_src": "fe80::/10",
    "not_dst": "1.1.1.0/24", "protocol": "0", "port": "0", "src_port": "0", "dst_port": "443",
    "dst_ports": "443,80,443", "service_port": "0", "app": "DNS", "dscp": "0", "netclass": "Unique local (IPv6)",
    "tcpflag": "no_flags", "hour": "0", "dow": "7", "tz": "Europe/Berlin", "group": "DC A", "asn": "0",
    "ip_version": "6", "direction": "egress", "min_bytes": "0", "max_bytes": "0", "fw_event": "denied",
    "count": "edge",
}


def test_every_key_parses_and_zero_is_a_value():
    f = F.parse_flow_filter(FULL)
    assert f.protocol == 0 and f.port == 0 and f.src_port == 0 and f.dscp == 0 and f.hour == 0
    assert f.asn == 0 and f.min_bytes == 0 and f.max_bytes == 0 and f.service_port == 0
    assert f.iface == F.IfaceRef(70000, "10.9.0.1") and f.iface_dir == "in"
    assert f.dst_ports == (80, 443)
    assert f.dst.is_host and f.dst.text == "192.0.2.5"
    assert f.active_keys() == frozenset(F.FLOW_FILTER_KEYS) - {"tz"}
    canonical = f.canonical()
    assert canonical["protocol"] == "0" and canonical["dst_ports"] == "80,443" and canonical["tz"] == "Europe/Berlin"
    assert canonical["iface"] == "70000@10.9.0.1" and canonical["talker"] == "10.0.0.0/8"
    # The canonical form parses back to the same filter.
    assert F.parse_flow_filter(canonical) == f


def test_empty_values_are_absent_and_defaults_are_not_active():
    f = F.parse_flow_filter({"exporter": "", "protocol": " ", "iface_dir": "out", "count": "all", "tz": "UTC"})
    assert f == F.FlowFilter(iface_dir="out") and f.active_keys() == frozenset() and f.canonical() == {}


def test_read_aliases_and_conflicts():
    assert F.parse_flow_filter({"proto": "17", "dscp_eq": "46"}).canonical() == {"protocol": "17", "dscp": "46"}
    assert F.parse_flow_filter({"proto": "6", "protocol": "6"}).protocol == 6
    err = _bad({"proto": "6", "protocol": "17"})
    assert set(err.fields) == {"protocol"}


def test_repeated_key_with_different_values_is_rejected():
    from starlette.datastructures import QueryParams

    err = _bad(QueryParams("port=1&port=2"))
    assert "port" in err.fields
    assert F.parse_flow_filter(QueryParams("port=1&port=1")).port == 1


@pytest.mark.parametrize("key,value", [
    ("exporter", "not-an-ip"), ("iface", "-1"), ("iface", "4294967296"), ("iface", "1@nope"), ("iface_dir", "up"),
    ("talker", "10.0.0.0/33"), ("src", "host"), ("protocol", "256"), ("protocol", "6.0"), ("port", "65536"),
    ("port", "-1"), ("port", "1e3"), ("src_port", "99999999999999999999999"), ("dst_port", "70000"),
    ("dst_ports", ",".join(str(p) for p in range(33))), ("dst_ports", "80,x"), ("service_port", "65536"),
    ("app", "NoSuchApp"), ("dscp", "64"), ("netclass", "Bogus"), ("tcpflag", "bogus"), ("hour", "24"),
    ("dow", "0"), ("dow", "8"), ("group", "NoSuchGroup"), ("asn", "4294967296"), ("ip_version", "5"),
    ("direction", "sideways"), ("min_bytes", "-5"), ("fw_event", "exploded"), ("count", "unique"),
])
def test_unknown_or_out_of_range_values_are_400(key, value):
    err = _bad({key: value})
    assert err.code == "invalid_filter"
    assert set(err.fields) == {key}


def test_all_problems_are_reported_together():
    err = _bad({"dst_port": "70000", "dscp": "99", "app": "Nope", "min_bytes": "5", "max_bytes": "4"})
    assert set(err.fields) == {"dst_port", "dscp", "app", "max_bytes"}
    assert err.detail.count(";") == 3


def test_bad_time_zone_is_a_window_error():
    err = _bad({"tz": "Nowhere/Land"})
    assert err.code == "invalid_window"


def test_ipv4_mapped_input_is_normalised():
    f = F.parse_flow_filter({"exporter": "::ffff:10.9.0.1", "talker": "::ffff:10.0.0.0/104"})
    assert f.exporter == "10.9.0.1" and f.talker.text == "10.0.0.0/8"


def test_without_clears_dimensions():
    f = F.parse_flow_filter({"iface": "3", "iface_dir": "out", "dscp": "0", "dst_ports": "53"})
    g = f.without("iface", "dscp", "dst_ports")
    assert g.active_keys() == frozenset() and g.iface_dir == "any"


# ── Compilation: golden fragments and binding ─────────────────────────────────

def test_raw_gets_the_volume_guard_and_rollups_do_not():
    sql, params = F.compile_filter(F.FlowFilter(), C.RAW, VIS_ALL)
    assert sql == S.VOLUME_GUARD and params == {}
    assert F.compile_filter(F.FlowFilter(), C.RAW, VIS_ALL, volume_guard=False) == ("1", {})
    assert F.compile_filter(F.FlowFilter(), C.CONVERSATIONS_1H, VIS_ALL) == ("1", {})


def test_exporter_predicates():
    sql, params = F.compile_filter(F.parse_flow_filter({"exporter": "10.9.0.1"}), C.CONVERSATIONS_1H, VIS_ALL)
    assert sql == "(exporter_ip6 = toIPv6('::') AND exporter_ip = toIPv4(%(nf_exporter)s))"
    assert params == {"nf_exporter": "10.9.0.1"}
    sql, params = F.compile_filter(F.parse_flow_filter({"exporter": "2001:db8:ffff::1"}), C.CONVERSATIONS_1H, VIS_ALL)
    assert sql == f"({S.EXPORTER} = toIPv6(%(nf_exporter)s))" and params == {"nf_exporter": "2001:db8:ffff::1"}
    # flow_traffic_5m has no exporter_ip6 column.
    sql, _ = F.compile_filter(F.parse_flow_filter({"exporter": "10.9.0.1"}), C.TRAFFIC_5M, VIS_ALL)
    assert sql == "exporter_ip = toIPv4(%(nf_exporter)s)"


def test_address_predicates_use_native_ipv4_and_unified_ipv6():
    f = F.parse_flow_filter({"talker": "10.0.0.0/8", "dst": "2001:db8::/32", "not_src": "192.0.2.7"})
    sql, params = F.compile_filter(f, C.CONVERSATIONS_1H, VIS_ALL)
    # IPv4 networks require an IPv4 flow (both IPv6 twins '::', NF-32).
    v4 = "(src_addr6 = toIPv6('::') AND dst_addr6 = toIPv6('::'))"
    assert S.IS_V4_FLOW == v4
    assert (f"(({v4} AND src_addr BETWEEN toIPv4(%(nf_talker_lo)s) AND toIPv4(%(nf_talker_hi)s)) OR "
            f"({v4} AND dst_addr BETWEEN toIPv4(%(nf_talker_lo)s) AND toIPv4(%(nf_talker_hi)s)))") in sql
    assert f"({S.DST} BETWEEN toIPv6(%(nf_dst_lo)s) AND toIPv6(%(nf_dst_hi)s))" in sql
    assert f"NOT ({v4} AND src_addr = toIPv4(%(nf_not_src)s))" in sql
    assert params == {
        "nf_talker_lo": "10.0.0.0", "nf_talker_hi": "10.255.255.255",
        "nf_dst_lo": "2001:db8::", "nf_dst_hi": "2001:db8:ffff:ffff:ffff:ffff:ffff:ffff",
        "nf_not_src": "192.0.2.7",
    }
    assert "toString(" not in sql


def test_iface_per_table_and_direction():
    f = F.parse_flow_filter({"iface": "70000@10.9.0.1", "iface_dir": "out"})
    sql, params = F.compile_filter(f, C.RAW, VIS_ALL)
    assert "output_if = %(nf_iface)s" in sql and "input_if" not in sql
    assert "(exporter_ip6 = toIPv6('::') AND exporter_ip = toIPv4(%(nf_iface_exporter)s))" in sql
    assert params["nf_iface"] == 70000 and params["nf_iface_exporter"] == "10.9.0.1"
    sql, _ = F.compile_filter(f, C.INTERFACE_5M, VIS_ALL)
    assert "(ifindex = %(nf_iface)s AND direction = 1)" in sql
    sql, _ = F.compile_filter(F.parse_flow_filter({"iface": "0"}), C.RAW, VIS_ALL)
    assert "(input_if = %(nf_iface)s OR output_if = %(nf_iface)s)" in sql


def test_tcp_flags_distinguish_exported_zero_from_unknown():
    sql, _ = F.compile_filter(F.parse_flow_filter({"tcpflag": "no_flags"}), C.RAW, VIS_ALL)
    assert sql.endswith("(protocol = 6 AND tcp_flags = 0 AND bitTest(fields_present, 0))")
    sql, _ = F.compile_filter(F.parse_flow_filter({"tcpflag": "no_flags"}), C.QOS_1H, VIS_ALL)
    assert sql == "(protocol = 6 AND tcp_flags = 0 AND flags_known = 1)"
    sql, _ = F.compile_filter(F.parse_flow_filter({"tcpflag": "syn_only"}), C.QOS_1H, VIS_ALL)
    assert sql == "(protocol = 6 AND bitAnd(tcp_flags, 2) != 0 AND bitAnd(tcp_flags, 16) = 0)"


def test_scalar_keys_bind_parameters():
    f = F.parse_flow_filter({
        "protocol": "0", "port": "0", "dst_ports": "53,443", "dscp": "0", "hour": "0", "dow": "1",
        "tz": "Asia/Tokyo", "asn": "0", "ip_version": "4", "direction": "ingress", "min_bytes": "0",
        "max_bytes": "10", "fw_event": "teardown",
    })
    sql, params = F.compile_filter(f, C.RAW, VIS_ALL)
    for fragment in (
        "protocol = %(nf_protocol)s", "(src_port = %(nf_port)s OR dst_port = %(nf_port)s)",
        "dst_port IN %(nf_dst_ports)s", "bitShiftRight(tos, 2) = %(nf_dscp)s",
        "toHour(timestamp, %(nf_tz)s) = %(nf_hour)s", "toDayOfWeek(timestamp, 0, %(nf_tz)s) = %(nf_dow)s",
        "(src_as = %(nf_asn)s OR dst_as = %(nf_asn)s)", "ip_version = %(nf_ip_version)s",
        "flow_direction = %(nf_direction)s", "flow_records.bytes >= %(nf_min_bytes)s",
        "flow_records.bytes <= %(nf_max_bytes)s", "fw_event = %(nf_fw_event)s",
    ):
        assert fragment in sql, fragment
    assert params == {
        "nf_protocol": 0, "nf_port": 0, "nf_dst_ports": (53, 443), "nf_dscp": 0, "nf_tz": "Asia/Tokyo",
        "nf_hour": 0, "nf_dow": 1, "nf_asn": 0, "nf_ip_version": 4, "nf_direction": 0, "nf_min_bytes": 0,
        "nf_max_bytes": 10, "nf_fw_event": 2,
    }


def test_app_service_port_group_and_netclass_use_shared_definitions():
    from app.services import netflow_classify

    f = F.parse_flow_filter({"app": "DNS", "service_port": "53"})
    raw, params = F.compile_filter(f, C.RAW, VIS_ALL)
    exprs = netflow_classify.classification_sql("src_port", "dst_port", "protocol", app_id_expr="app_id",
                                                src_addr="src_addr", dst_addr="dst_addr")
    assert f"{exprs['application']} = %(nf_app)s" in raw and f"{exprs['service_port']} = %(nf_service_port)s" in raw
    assert params["nf_app"] == "DNS" and params["nf_service_port"] == 53
    rollup, _ = F.compile_filter(f, C.CONVERSATIONS_1H, VIS_ALL)
    assert netflow_classify.ROLLUP_SRC_PORT_SQL in rollup
    sql, _ = F.compile_filter(F.parse_flow_filter({"group": "DC A"}), C.CONVERSATIONS_1H, VIS_ALL)
    assert sql == G.group_predicate("DC A", "src_addr", "dst_addr")
    sql, _ = F.compile_filter(F.parse_flow_filter({"netclass": "Link-local"}), C.RAW, VIS_ALL)
    assert "169.254.0.0" in sql and "fe80::" in sql
    public, _ = F.compile_filter(F.parse_flow_filter({"netclass": "Public"}), C.CONVERSATIONS_1H, VIS_ALL)
    assert public.startswith("(NOT (") and "OR NOT (" in public


def test_visibility_is_always_applied(meta):
    sql, params = F.compile_filter(F.FlowFilter(), C.CONVERSATIONS_1H, VIS_SCOPED)
    assert sql == ("((exporter_ip6 = toIPv6('::') AND exporter_ip IN %(nf_vis_v4)s) OR exporter_ip6 IN %(nf_vis_v6)s)")
    assert params == {"nf_vis_v4": ["10.9.0.1"], "nf_vis_v6": ["2001:db8:ffff::1"]}
    assert meta.restricted
    # An empty visible set matches nothing - never an unfiltered query.
    sql, _ = F.compile_filter(F.FlowFilter(), C.RAW, VIS_NONE)
    assert sql.startswith("0 AND ")
    # Tables without exporter_ip6 cannot hold IPv6 exporters.
    sql, params = F.compile_filter(F.FlowFilter(), C.TRAFFIC_5M, VIS_SCOPED)
    assert sql == "(exporter_ip IN %(nf_vis_v4)s)" and "nf_vis_v6" not in params
    with pytest.raises(TypeError):
        F.compile_filter(F.FlowFilter(), C.RAW, None)


def test_drop_removes_a_dimension_and_is_disclosed(meta):
    f = F.parse_flow_filter({"dscp": "46", "protocol": "17"})
    sql, params = F.compile_filter(f, C.QOS_1H, VIS_ALL, drop={"dscp"})
    assert "tos" not in sql and "nf_dscp" not in params
    assert meta.dropped == {"dscp"}


def test_capability_map():
    f = F.parse_flow_filter({"dscp": "0"})
    assert not F.table_supports(f, C.CONVERSATIONS_1H) and F.table_supports(f, C.QOS_1H)
    assert F.table_supports(f, C.CONVERSATIONS_1H, drop={"dscp"})
    with pytest.raises(F.UnsupportedFilter) as exc:
        F.compile_filter(F.parse_flow_filter({"src_port": "1", "iface": "2"}), C.CONVERSATIONS_1H, VIS_ALL)
    assert exc.value.keys == ("iface", "src_port")
    assert F.table_supports(F.parse_flow_filter({"hour": "3", "tz": "Asia/Kolkata"}), C.INTERFACE_5M)
    assert not F.table_supports(F.parse_flow_filter({"hour": "3", "tz": "Asia/Kolkata"}), C.QOS_1H)
    assert not F.table_supports(F.parse_flow_filter({"app": "DNS"}), C.QOS_1H)


def test_count_edge_policy():
    policy = F.CountingPolicy(excluded=("::ffff:10.9.0.9",), ingress_only=("::ffff:10.9.0.1",), egress_only=())
    f = F.FlowFilter(count="edge", counting=policy)
    sql, params = F.compile_filter(f, C.RAW, VIS_ALL)
    assert f"{S.EXPORTER} NOT IN %(nf_count_excluded)s" in sql
    assert f"NOT ({S.EXPORTER} IN %(nf_count_ingress)s AND flow_direction = 1)" in sql
    assert params["nf_count_excluded"] == ("::ffff:10.9.0.9",)
    assert not F.table_supports(f, C.CONVERSATIONS_1H)       # needs flow_direction
    roles_only = F.FlowFilter(count="edge", counting=F.CountingPolicy(excluded=("::ffff:10.9.0.9",)))
    assert F.table_supports(roles_only, C.CONVERSATIONS_1H)
    with pytest.raises(ValueError):
        F.compile_filter(F.FlowFilter(count="edge"), C.RAW, VIS_ALL)


# ── Scope, drops and capability (the legacy scope-dict adapters are gone) ────

def test_compile_applies_every_filter_visibility_and_the_guard():
    f = F.parse_flow_filter({"exporter": "10.9.0.1", "src": "10.0.0.0/8", "port": "22", "dscp": "0"})
    sql, params = F.compile_filter(f, C.RAW, VIS_SCOPED)
    assert S.VOLUME_GUARD in sql
    assert "nf_vis_v4" in params and params["nf_port"] == 22 and params["nf_dscp"] == 0 and "nf_src_lo" in params
    # A widget dropping its own dimension keeps the scope and every other filter.
    sql, params = F.compile_filter(f, C.RAW, VIS_SCOPED, frozenset({"exporter", "dscp"}))
    assert "nf_exporter" not in params and "nf_dscp" not in params and "nf_vis_v4" in params
    assert F.compile_filter(F.parse_flow_filter({"protocol": "6"}), C.RAW, VIS_ALL) == (
        f"{S.VOLUME_GUARD} AND protocol = %(nf_protocol)s", {"nf_protocol": 6})


def test_rollup_capability_map_decides_eligibility():
    # Keys the hourly conversation rollup cannot answer force raw (NF-17 planner).
    for query in ({"src_port": "5"}, {"asn": "1"}, {"iface": "2"}, {"dscp": "1"}, {"min_bytes": "1"}):
        assert not F.table_supports(F.parse_flow_filter(query), C.CONVERSATIONS_1H), query
    assert F.table_supports(F.parse_flow_filter({"dscp": "1"}), C.QOS_1H)
    assert not F.table_supports(F.parse_flow_filter({"talker": "10.0.0.1"}), C.QOS_1H)


def test_exporter_filter_validates_and_scopes():
    F.require_filter_exporters_visible(F.parse_flow_filter({"exporter": "10.9.0.1"}), VIS_SCOPED)
    with pytest.raises(HTTPException) as exc:
        F.require_filter_exporters_visible(F.parse_flow_filter({"exporter": "10.9.0.2"}), VIS_SCOPED)
    assert exc.value.status_code == 404
    with pytest.raises(NetflowError) as exc:
        F.parse_flow_filter({"exporter": "bogus"})
    assert exc.value.status_code == 400


# ── Every compiled predicate runs on the real schema (opt-in ClickHouse) ──────

from netflow_scratch_ch import flow_db, scratch_client  # noqa: E402


def test_compiled_predicates_execute_on_every_table(monkeypatch):
    client = scratch_client()
    with flow_db(client) as db:
        monkeypatch.setattr(C, "CH_DB", db)
        full = F.parse_flow_filter({k: v for k, v in FULL.items() if k != "count"})
        from dataclasses import replace

        full = replace(full, count="edge", counting=F.CountingPolicy(
            excluded=("::ffff:10.9.0.9",), ingress_only=("::ffff:10.9.0.1",), egress_only=("2001:db8:ffff::1",)))
        for vis in (VIS_ALL, VIS_SCOPED, VIS_NONE):
            for name, caps in S.TABLES.items():
                drop = full.active_keys() - caps.supports
                drop |= caps.unsupported(full, drop)
                where, params = F.compile_filter(full, name, vis, drop)
                rows = client.query(f"SELECT count() FROM {caps.ref} WHERE {where}", parameters=params).result_rows
                assert rows == [(0,)], name
        # The network-class expression agrees with the filter on real addresses.
        client.command(
            f"INSERT INTO {db}.flow_records (timestamp, received_at, src_addr, dst_addr, src_addr6, dst_addr6, bytes) VALUES "
            "(now(), now(), '10.1.2.3', '8.8.8.8', '::', '::', 1), "
            "(now(), now(), '0.0.0.0', '0.0.0.0', 'fe80::1', 'fd00::5', 1), "
            "(now(), now(), '198.51.100.7', '203.0.113.1', '::', '::', 1), "
            "(now(), now(), '0.0.0.0', '0.0.0.0', '2001:db8::1', '2001:4860::8888', 1)"
        )
        got = client.query(f"SELECT {S.netclass_expr('src')}, {S.netclass_expr('dst')} FROM {db}.flow_records "
                           f"ORDER BY src_addr6, src_addr").result_rows
        assert got == [("Private 10/8", "Public"), ("Documentation / special-use", "Documentation / special-use"),
                       ("Documentation / special-use", "Public"), ("Link-local", "Unique local (IPv6)")]
        # The filter and the breakdown expression agree: documentation space is
        # never counted as Public (B2 note).
        for name, want in (("Public", 2), ("Documentation / special-use", 2)):
            where, params = F.compile_filter(F.parse_flow_filter({"netclass": name}), C.RAW, VIS_ALL)
            n = client.query(f"SELECT count() FROM {db}.flow_records WHERE {where}", parameters=params).result_rows[0][0]
            assert n == want, name
