"""NetFlow classifier (NF-20 / CR-12): services/netflow_classify.py.

Three layers:
  * the review's concrete cases (request/response halves, PAT and ephemeral
    collisions, protocol-aware ports, portless protocols, ICMP type/code);
  * pure-Python properties over random tuples (a request and its response
    classify to the same service and application);
  * Python/SQL parity: the same random tuples evaluated by ClickHouse through
    ``classification_tuple_sql`` -- raw rows with addresses, operator rules
    (ranges, CIDRs, protocol and address rules, late rules), NBAR names, and
    the hourly-rollup variant built from real ``groupUniqArrayState`` source
    ports. Gated on ZENPLUS_NETFLOW_TEST_CH (e.g.
    http://default:pw@127.0.0.1:18123); works in a private database it drops.
"""
from __future__ import annotations

import os
import random
from urllib.parse import urlparse

import pytest

from app.services import netflow_classify as nc
from app.services.netflow_classify import AppRule, build_rules, classify

BUILTIN = build_rules()


def c(proto, sport, dport, **kw):
    return classify(proto, sport, dport, rules=kw.pop("rules", BUILTIN), **kw)


# ── Review cases ─────────────────────────────────────────────────────────────

def test_https_request_and_response_share_the_service():
    req, resp = c(6, 51234, 443), c(6, 443, 51234)
    assert (req.service_port, req.service, req.application) == (443, "HTTPS", nc.APP_WEB)
    assert (resp.service_port, resp.service, resp.application) == (443, "HTTPS", nc.APP_WEB)
    assert (req.direction, resp.direction) == ("request", "response")
    assert (req.basis, resp.basis) == ("dst_rule", "src_rule")
    assert req.confidence == resp.confidence == "high"


def test_pat_client_port_collision_keeps_the_server_port():
    # 443 -> 3306: both ports are known services, neither ephemeral -> lower.
    r = c(6, 443, 3306)
    assert (r.service_port, r.application, r.direction, r.confidence) == (443, nc.APP_WEB, "response", "medium")


def test_ephemeral_client_port_is_never_the_service():
    # 50000 is SAP but sits in every OS's ephemeral range.
    r = c(6, 443, 50000)
    assert (r.service_port, r.service) == (443, "HTTPS")


def test_dns_response_to_ephemeral_port():
    r = c(17, 53, 53000)
    assert (r.service_port, r.service, r.application, r.direction) == (53, "DNS", "DNS", "response")


def test_rules_are_protocol_aware():
    udp22 = c(17, 5000, 22)
    assert udp22.service != "SSH" and udp22.application != "Remote Access (SSH/RDP/VNC)"
    assert (udp22.service_port, udp22.service, udp22.confidence) == (22, "UDP/22", "low")
    assert c(6, 5000, 22).service == "SSH"
    quic = c(17, 51000, 443)
    assert (quic.service, quic.application) == ("QUIC", nc.APP_WEB)


def test_portless_protocols_are_not_applicable():
    esp = c(50, 0, 0)
    assert esp.port_applicable is False
    assert esp.service is None and esp.service_port is None
    assert (esp.application, esp.direction, esp.basis) == (nc.APP_VPN, "n/a", "protocol")
    gre = c(47, 1234, 5678)   # garbage in the port fields must not create a service
    assert gre.service_port is None and gre.application == nc.APP_VPN


def test_icmp_type_code_is_decoded_not_read_as_a_port():
    echo = c(1, 0, 8 * 256)
    assert (echo.service, echo.application, echo.direction) == ("Echo request", nc.APP_ICMP, "request")
    unreach = c(1, 0, 3 * 256 + 3)
    assert unreach.service == "Port unreachable"
    assert unreach.application == nc.APP_ICMP and unreach.application != nc.APP_SYSTEM
    assert unreach.service_port is None
    v6 = c(58, 0, 129 * 256)
    assert (v6.service, v6.direction) == ("Echo reply", "response")
    assert nc.icmp_label(58, 0) == "Type not exported"


def test_unknown_high_ports_are_honestly_unknown():
    r = c(6, 40000, 41000)
    assert (r.service_port, r.service, r.direction, r.application, r.confidence) == (
        None, None, "unknown", nc.APP_OTHER, "low")


def test_registered_port_against_ephemeral_is_a_low_confidence_inference():
    r = c(6, 51234, 8081)
    assert (r.service_port, r.service, r.basis, r.confidence) == (8081, "TCP/8081", "ephemeral", "low")


def test_unmatched_well_known_port_is_system_services():
    r = c(6, 40000, 515 + 1)   # 516 has no rule
    assert (r.service_port, r.application, r.basis) == (516, nc.APP_SYSTEM, "lower_port")


def test_nbar_application_wins_and_keeps_the_port_service():
    rs = build_rules(app_names={(13 << 32) | 453: ("ms-teams", "collaboration")})
    r = classify(6, 51234, 443, app_id=(13 << 32) | 453, rules=rs)
    assert (r.application, r.category, r.basis, r.confidence) == ("ms-teams", "collaboration", "exporter", "high")
    assert (r.service_port, r.service) == (443, "HTTPS")
    # An App-ID the exporter never named falls back to ports.
    assert classify(6, 51234, 443, app_id=99, rules=rs).application == nc.APP_WEB


def test_exporter_names_fold_onto_builtin_applications():
    """Wave C item 24: an NBAR "icmp" next to the built-in "ICMP", or "ipsec"
    next to "VPN / Tunneling", listed one protocol twice in breakdowns, the
    app filter and reports. Equivalent names count as the built-in
    application; other names stay as announced, folded by case only."""
    rs = build_rules(
        [AppRule(id="1", application="YouTube", protocol=6, port_lo=7443)],
        app_names={1: ("icmp", "net-admin"), 2: ("ipsec", "layer3-over-ip"), 3: ("DNS", "net-admin"),
                   4: ("ssl", "browsing"), 5: ("youtube", "consumer-streaming"), 6: ("fastly", "browsing")},
    )
    assert {k: v[0] for k, v in rs.app_names.items()} == {
        1: nc.APP_ICMP, 2: nc.APP_VPN, 3: "DNS", 4: nc.APP_WEB, 5: "YouTube", 6: "fastly"}
    apps = nc.known_applications(rs)
    assert len({a.lower() for a in apps}) == len(apps)
    assert "fastly" in apps and not {"icmp", "ipsec", "ssl", "youtube"} & set(apps)
    r = classify(1, 0, 0x0800, app_id=1, rules=rs)
    assert (r.application, r.basis, r.category) == (nc.APP_ICMP, "exporter", "Network control")
    assert classify(50, 0, 0, app_id=2, rules=rs).application == nc.APP_VPN
    assert nc.category_for("fastly", rs) == "browsing"
    # app= accepts the exporter's own spelling and any case of a known name.
    assert nc.resolve_application("icmp", rs) == nc.APP_ICMP
    assert nc.resolve_application("IPSEC", rs) == nc.APP_VPN
    assert nc.resolve_application("web (http/https)", rs) == nc.APP_WEB
    assert nc.resolve_application("fastly", rs) == "fastly"
    assert nc.resolve_application("no-such-app", rs) is None


def test_operator_rules_override_builtins_by_priority():
    rs = build_rules([
        AppRule(id="1", application="Custom Web", protocol=6, port_lo=8000, port_hi=8100, priority=50),
        AppRule(id="2", application="Late App", port_lo=9999, priority=2000),
        AppRule(id="3", application="Intranet", port_lo=5000, cidrs=("10.50.0.0/16",)),
        AppRule(id="4", application="Backup", cidrs=("10.70.0.0/16",), priority=500),
        AppRule(id="5", application="Site Tunnel", protocol=47, cidrs=("10.60.0.0/16",)),
    ])
    assert classify(6, 51234, 8080, rules=rs).application == "Custom Web"
    assert classify(6, 51234, 9999, rules=rs).application == "Late App"
    # CIDR-restricted port rule: applies only when the service side matches.
    assert classify(6, 51234, 5000, src_ip="10.9.0.1", dst_ip="10.50.1.1", rules=rs).application == "Intranet"
    assert classify(6, 51234, 5000, src_ip="10.50.1.1", dst_ip="10.9.0.1", rules=rs).application == nc.APP_OTHER
    # ... and never when addresses are not supplied (the SQL mirror then omits it).
    assert classify(6, 51234, 5000, rules=rs).application == nc.APP_OTHER
    # An address rule (priority 500) outranks a built-in port rule (1000).
    backup = classify(6, 51234, 445, src_ip="10.70.0.9", dst_ip="10.1.1.1", rules=rs)
    assert (backup.application, backup.basis, backup.service) == ("Backup", "rule", "SMB")
    tunnel = classify(47, 0, 0, src_ip="10.60.0.1", dst_ip="192.0.2.1", rules=rs)
    assert (tunnel.application, tunnel.basis) == ("Site Tunnel", "rule")
    assert "Custom Web" in nc.known_applications(rs)


def test_invalid_operator_rules_are_ignored():
    rs = build_rules([
        AppRule(id="a", application="", port_lo=80),
        AppRule(id="b", application="PortsOnESP", protocol=50, port_lo=80),
        AppRule(id="c", application="BadCidr", port_lo=80, cidrs=("not-a-cidr",)),
        AppRule(id="d", application="Nothing"),
        AppRule(id="e", application="Inverted", port_lo=90, port_hi=80),
    ])
    assert rs.db_rules == 0
    assert classify(6, 51234, 80, rules=rs).application == nc.APP_WEB


def test_ephemeral_threshold_is_configurable():
    windows = build_rules(ephemeral_min=49152)
    # 40000 is ephemeral on Linux, registered on Windows.
    assert classify(6, 40000, 50001, rules=windows).service_port == 40000
    assert c(6, 40000, 50001).service_port is None


def test_service_name_and_rollup_src_port():
    assert nc.service_name(6, 443) == "HTTPS"
    assert nc.service_name(17, 443) == "QUIC"
    assert nc.service_name(17, 8125) == "UDP/8125"
    assert nc.service_name(1, 3 * 256 + 1) == "Host unreachable"
    assert nc.service_name(50, 0) is None
    assert nc.rollup_src_port([443]) == 443
    assert nc.rollup_src_port([443, 443]) == 443
    assert nc.rollup_src_port([443, 8443]) == 0
    assert nc.rollup_src_port([]) == 0


def test_known_applications_cover_legacy_bucket_names():
    apps = set(nc.known_applications(BUILTIN))
    for legacy in ("Web (HTTP/HTTPS)", "DNS", "VPN / Tunneling", "ICMP", "Database",
                   "System Services", "Other", "Network Mgmt"):
        assert legacy in apps


# ── Pure-Python properties ───────────────────────────────────────────────────

PROTOS = [6, 17, 1, 58, 50, 47, 89, 132, 0, 255, 33, 136]
PORTS = [0, 20, 22, 53, 80, 123, 443, 445, 3306, 3389, 5000, 8080, 8081, 9999, 32767, 32768,
         40000, 49151, 49152, 50000, 51234, 53000, 65535, 1023, 1024, 2048, 771]


def _rand_tuple(rng):
    p = rng.choice(PROTOS)
    s = rng.choice(PORTS) if rng.random() < 0.7 else rng.randint(0, 65535)
    d = rng.choice(PORTS) if rng.random() < 0.7 else rng.randint(0, 65535)
    return p, s, d


def test_property_request_and_response_halves_agree():
    rng = random.Random(20260930)
    for _ in range(20000):
        p, s, d = _rand_tuple(rng)
        if s == d:
            continue
        a, b = c(p, s, d), c(p, d, s)
        assert a.service_port == b.service_port, (p, s, d)
        assert a.application == b.application, (p, s, d)
        assert a.confidence == b.confidence, (p, s, d)
        flip = {"request": "response", "response": "request"}
        if a.port_applicable:
            assert b.direction == flip.get(a.direction, a.direction), (p, s, d)


def test_property_service_port_is_one_of_the_ports():
    rng = random.Random(7)
    for _ in range(20000):
        p, s, d = _rand_tuple(rng)
        r = c(p, s, d)
        if not r.port_applicable:
            assert r.service_port is None
        elif r.service_port is not None:
            assert r.service_port in (s, d) and r.service_port != 0


# ── Python/SQL parity against ClickHouse ─────────────────────────────────────

CH_URL = os.environ.get("ZENPLUS_NETFLOW_TEST_CH")
needs_ch = pytest.mark.skipif(not CH_URL, reason="set ZENPLUS_NETFLOW_TEST_CH to run SQL parity")

V4_POOL = ["10.50.1.1", "10.60.0.7", "10.70.0.9", "10.9.0.1", "192.0.2.1", "198.51.100.3"]
V6_POOL = ["2001:db8:1::5", "2001:db8:2::9", "fd00::1"]
NBAR = {(13 << 32) | 80: ("http", "browsing"), (13 << 32) | 453: ("ms-teams", "collaboration"),
        (20 << 32) | 7: ("it's-quoted\\app 100%", "odd")}
RULES = [
    AppRule(id="1", application="Custom Web", protocol=6, port_lo=8000, port_hi=8100, priority=50),
    AppRule(id="2", application="Late App", port_lo=9999, priority=2000),
    AppRule(id="3", application="Intranet", port_lo=5000, cidrs=("10.50.0.0/16",)),
    AppRule(id="4", application="Backup", cidrs=("10.70.0.0/16",), priority=500),
    AppRule(id="5", application="Site Tunnel", protocol=47, cidrs=("10.60.0.0/16",)),
    AppRule(id="6", application="Partner Portal", protocol=6, port_lo=443,
            cidrs=("2001:db8:1::/48",), priority=10),
    AppRule(id="7", application="All UDP DMZ", protocol=17, cidrs=("198.51.100.0/24",), priority=100),
]


@pytest.fixture(scope="module")
def ch():
    import clickhouse_connect

    u = urlparse(CH_URL)
    client = clickhouse_connect.get_client(host=u.hostname, port=u.port or 8123,
                                           username=u.username or "default", password=u.password or "")
    db = f"nf_classify_test_{os.getpid()}"
    client.command(f"CREATE DATABASE IF NOT EXISTS {db}")
    yield client, db
    client.command(f"DROP DATABASE IF EXISTS {db}")


def _rows(n, seed):
    rng = random.Random(seed)
    rows = []
    for i in range(n):
        p, s, d = _rand_tuple(rng)
        v6 = rng.random() < 0.25
        src = rng.choice(V6_POOL if v6 else V4_POOL)
        dst = rng.choice(V6_POOL if v6 else V4_POOL)
        app_id = rng.choice([0, 0, 0, 99, *NBAR])
        rows.append((i, p, s, d, app_id, src, dst, v6))
    return rows


def _load(client, db, rows):
    client.command(f"DROP TABLE IF EXISTS {db}.flows")
    client.command(f"""CREATE TABLE {db}.flows (
        i UInt32, protocol UInt8, src_port UInt16, dst_port UInt16, app_id UInt64,
        src_addr IPv4, src_addr6 IPv6, dst_addr IPv4, dst_addr6 IPv6) ENGINE = Memory""")
    data = [
        (i, p, s, d, a,
         "0.0.0.0" if v6 else src, src if v6 else "::",
         "0.0.0.0" if v6 else dst, dst if v6 else "::")
        for i, p, s, d, a, src, dst, v6 in rows
    ]
    client.insert(f"{db}.flows", data, column_names=[
        "i", "protocol", "src_port", "dst_port", "app_id", "src_addr", "src_addr6", "dst_addr", "dst_addr6"])


# Both ClickHouse query analyzers: the new one is the default since 24.3, the
# old one is stricter about aliases (see netflow_classify's SQL notes).
ANALYZERS = pytest.mark.parametrize("analyzer", [1, 0], ids=["new-analyzer", "old-analyzer"])


def _st(analyzer):
    return {"allow_experimental_analyzer": analyzer}


def _expected(r):
    return [r.service_port or 0, r.direction, r.application, r.basis, r.confidence]


@needs_ch
@ANALYZERS
@pytest.mark.parametrize("ephemeral_min", [32768, 49152])
def test_sql_matches_python_on_raw_rows(ch, ephemeral_min, analyzer):
    client, db = ch
    rs = build_rules(RULES, NBAR, ephemeral_min)
    rows = _rows(15000, seed=ephemeral_min)
    _load(client, db, rows)
    tup = nc.classification_tuple_sql(src_addr="src_addr", dst_addr="dst_addr", rules=rs)
    # Bound parameters make the client %-format the query text: the generated
    # SQL (and its literals) must survive that.
    got = client.query(f"SELECT i, {tup} FROM {db}.flows WHERE i >= %(lo)s ORDER BY i",
                       parameters={"lo": 0}, settings=_st(analyzer)).result_rows
    mismatches = []
    for (i, p, s, d, a, src, dst, _v6), (gi, t) in zip(rows, got):
        exp = _expected(classify(p, s, d, app_id=a, src_ip=src, dst_ip=dst, rules=rs))
        if list(t) != exp:
            mismatches.append(((p, s, d, a, src, dst), list(t), exp))
    assert not mismatches, mismatches[:5]


@needs_ch
@ANALYZERS
def test_sql_matches_python_without_addresses_or_app_id(ch, analyzer):
    """Builders called without address columns skip CIDR rules, exactly like
    ``classify`` without addresses; ``app_id_expr=None`` disables NBAR."""
    client, db = ch
    rs = build_rules(RULES, NBAR)
    rows = _rows(8000, seed=11)
    _load(client, db, rows)
    tup = nc.classification_tuple_sql(app_id_expr=None, rules=rs)
    got = client.query(f"SELECT i, {tup} FROM {db}.flows ORDER BY i", settings=_st(analyzer)).result_rows
    for (i, p, s, d, _a, _src, _dst, _v6), (gi, t) in zip(rows, got):
        assert list(t) == _expected(classify(p, s, d, rules=rs)), (p, s, d)


@needs_ch
@ANALYZERS
def test_individual_expressions_and_filter_partition(ch, analyzer):
    """Each ``classification_sql`` element equals its tuple element when all
    are combined in one query (shared aliases), and the ``app=`` predicates of
    all known applications partition the rows."""
    client, db = ch
    rs = build_rules(RULES, NBAR)
    rows = _rows(6000, seed=5)
    _load(client, db, rows)
    kw = dict(src_addr="src_addr", dst_addr="dst_addr", rules=rs)
    exprs = nc.classification_sql(**kw)
    tup = nc.classification_tuple_sql(**kw)
    cols = ", ".join(exprs[k] for k in ("service_port", "direction", "application", "basis", "confidence"))
    diff = client.query(f"SELECT countIf(tuple({cols}) != {tup}) FROM {db}.flows",
                        settings=_st(analyzer)).result_rows[0][0]
    assert diff == 0
    # Filter, group and select with the same expressions in one statement.
    grouped = client.query(
        f"SELECT {exprs['application']} AS app, {exprs['service_port']} AS sp, count() FROM {db}.flows "
        f"WHERE {exprs['application']} != %(skip)s GROUP BY app, sp",
        parameters={"skip": "no such app"}, settings=_st(analyzer),
    ).result_rows
    assert sum(r[2] for r in grouped) == len(rows)
    total = 0
    for app in nc.known_applications(rs):
        total += client.query(
            f"SELECT count() FROM {db}.flows WHERE {nc.application_sql(**kw)} = %(app)s",
            parameters={"app": app}, settings=_st(analyzer),
        ).result_rows[0][0]
    assert total == len(rows)


@needs_ch
@ANALYZERS
def test_rollup_variant_uses_single_source_port_state(ch, analyzer):
    client, db = ch
    rs = build_rules(RULES, NBAR)
    rows = _rows(12000, seed=3)
    _load(client, db, rows)
    client.command(f"DROP TABLE IF EXISTS {db}.conv")
    client.command(f"""CREATE TABLE {db}.conv (
        k UInt32, protocol UInt8, dst_port UInt16, app_id UInt64,
        src_addr IPv4, src_addr6 IPv6, dst_addr IPv4, dst_addr6 IPv6,
        src_ports AggregateFunction(groupUniqArray(10), UInt16)) ENGINE = Memory""")
    # Key on (i mod 4000, protocol, dst_port, ...) so some keys collect one
    # source port and others several, like the hourly rollup.
    client.command(f"""INSERT INTO {db}.conv
        SELECT i % 4000 AS k, protocol, dst_port, app_id, src_addr, src_addr6, dst_addr, dst_addr6,
               groupUniqArrayState(10)(src_port)
        FROM {db}.flows GROUP BY k, protocol, dst_port, app_id, src_addr, src_addr6, dst_addr, dst_addr6""")
    tup = nc.classification_tuple_sql(nc.ROLLUP_SRC_PORT_SQL, "dst_port", "protocol",
                                      src_addr="src_addr", dst_addr="dst_addr", rules=rs)
    got = client.query(
        f"""SELECT protocol, dst_port, app_id,
                   replaceRegexpOne(toString(if(src_addr6 = toIPv6('::'), IPv4ToIPv6(src_addr), src_addr6)), '^::ffff:', ''),
                   replaceRegexpOne(toString(if(dst_addr6 = toIPv6('::'), IPv4ToIPv6(dst_addr), dst_addr6)), '^::ffff:', ''),
                   finalizeAggregation(src_ports), {tup}
            FROM {db}.conv""", settings=_st(analyzer)
    ).result_rows
    assert any(len(r[5]) > 1 for r in got) and any(len(r[5]) == 1 for r in got)
    for p, d, a, src, dst, ports, t in got:
        exp = classify(p, nc.rollup_src_port(ports), d, app_id=a, src_ip=src, dst_ip=dst, rules=rs)
        assert list(t) == _expected(exp), (p, ports, d, a, src, dst)


def test_builtin_sql_stays_compact():
    """The let-bound SQL must stay small enough to embed several times in a
    query (max_query_size is 256 KiB by default)."""
    rs = build_rules(RULES, NBAR)
    assert len(nc.classification_tuple_sql(src_addr="src_addr", dst_addr="dst_addr", rules=rs)) < 20000
