import pytest
from app.services.udt_gateway_collection import (
    canonical_mac,
    snmp_cells,
    fortinet_switches,
    fortinet_ports,
    fortinet_mac_cache,
    fortinet_mac_table,
    parse_arp,
)


@pytest.mark.parametrize(
    "raw",
    ["AA:BB:CC:DD:EE:02", "aabb.ccdd.ee02", "aabb-ccdd-ee02", "AA-BB-CC-DD-EE-02"],
)
def test_mac_normalization(raw):
    assert canonical_mac(raw) == "aa:bb:cc:dd:ee:02"


@pytest.mark.parametrize(
    "raw",
    [
        "00:00:00:00:00:00",
        "ff:ff:ff:ff:ff:ff",
        "01:00:5e:00:00:01",
        "not-a-mac",
        "aa:bb:cc:dd:ee",
    ],
)
def test_non_endpoint_macs_rejected(raw):
    assert canonical_mac(raw) is None


def test_fortinet_full_index_and_local_ports():
    switches = fortinet_switches(
        {
            "serial": {"1.9.23000001": "SAMPLE001", "1.9.23000002": "SAMPLE002"},
            "name": {"1.9.23000001": "Core"},
            "status": {"1.9.23000001": "1"},
        }
    )
    ports = fortinet_ports(
        {
            "name": {
                "1.9.23000001.1": "port1",
                "1.9.23000002.1": "port1",
                "1.9.9999.1": "port1",
                "1.9.23000001.0": "internal",
            },
            "status": {"1.9.23000001.1": "1"},
            "pvid": {"1.9.23000001.1": "24"},
        },
        switches,
    )
    assert len(ports) == 2 and ports[0]["serial"] != ports[1]["serial"]
    assert ports[0]["port_number"] == ports[1]["port_number"] == 1
    assert ports[0]["pvid"] == 24 and ports[1]["oper_status"] == "unknown"


def test_snmp_unsupported_and_wrong_root_not_data():
    assert (
        snmp_cells(
            [
                ".1.2.3.4 = No Such Object available on this agent at this OID",
                '.1.2.4.5 = STRING: "wrong"',
            ],
            "1.2.3",
        )
        == {}
    )
    assert snmp_cells(['.1.2.3.1.9.2300 = STRING: "Core"'], "1.2.3") == {
        "1.9.2300": "Core"
    }


def test_cache_requires_switch_vlan_port_and_deduplicates():
    data = fortinet_mac_cache(
        """1 4 aa:bb:cc:dd:ee:02
managed-switch: SAMPLE001 vfid: 0
24 0 aa:bb:cc:dd:ee:04
24 4 aa:bb:cc:dd:ee:02
24 4 AA:BB:CC:DD:EE:02
24 5 ff:ff:ff:ff:ff:ff
4095 6 aa:bb:cc:dd:ee:06"""
    )
    assert data == [
        {
            "serial": "SAMPLE001",
            "port_number": 4,
            "vlan_id": 24,
            "mac": "aa:bb:cc:dd:ee:02",
            "is_direct": False,
        }
    ]


def test_mac_table_uses_physical_port_and_skips_trunk_and_internal():
    rows = fortinet_mac_table(
        """Managed Switch : Core 0
MAC: aa:bb:cc:dd:ee:02 VLAN: 24 Port: port4(port-id 4)
MAC: aa:bb:cc:dd:ee:02 VLAN: 24 Trunk: uplink(trunk-id 0)
MAC: aa:bb:cc:dd:ee:04 VLAN: 4094 Port: internal(port-id 52)
Managed Switch : Unknown 0
MAC: aa:bb:cc:dd:ee:06 VLAN: 24 Port: port4(port-id 4)""",
        [{"serial": "SAMPLE001", "hostname": "Core"}],
    )
    assert (
        len(rows) == 1
        and rows[0]["serial"] == "SAMPLE001"
        and rows[0]["if_name"] == "port4"
        and not rows[0]["is_direct"]
    )


@pytest.mark.parametrize(
    "platform,line,interface",
    [
        ("fortinet", "192.0.2.10 0 aa:bb:cc:dd:ee:02 internal", "internal"),
        (
            "paloalto_panos",
            "ethernet1/4 192.0.2.10 aa:bb:cc:dd:ee:02 ethernet1/4 c 1200",
            "ethernet1/4",
        ),
        ("cisco_ios", "Internet 192.0.2.10 0 aabb.ccdd.ee02 ARPA Vlan24", "Vlan24"),
        ("cisco_xe", "Internet 192.0.2.10 0 aabb.ccdd.ee02 ARPA Vlan24", "Vlan24"),
        ("cisco_nxos", "192.0.2.10 00:00:10 aabb.ccdd.ee02 Vlan24", "Vlan24"),
        ("cisco_asa", "inside 192.0.2.10 aabb.ccdd.ee02 5", "inside"),
        ("arista_eos", "192.0.2.10 0 aabb.ccdd.ee02 Vlan24", "Vlan24"),
        ("juniper_junos", "aa:bb:cc:dd:ee:02 192.0.2.10 ge-0/0/1.0 none", "ge-0/0/1.0"),
        ("aruba_aoscx", "192.0.2.10 aa:bb:cc:dd:ee:02 vlan24 1/1/4 reachable", "vlan24"),
        ("aruba_osswitch", "192.0.2.10 aa:bb:cc:dd:ee:02 dynamic 24", "24"),
        ("huawei", "192.0.2.10 aabb-ccdd-ee02 20 D-0 GE1/0/1", "GE1/0/1"),
        ("hp_comware", "192.0.2.10 aabb-ccdd-ee02 24 GE1/0/1 20 D", "GE1/0/1"),
    ],
)
def test_gateway_vendor_arp_formats(platform, line, interface):
    assert parse_arp(line, platform) == [
        {"ip": "192.0.2.10", "mac": "aa:bb:cc:dd:ee:02", "interface": interface}
    ]


def test_bad_neighbors_not_linked():
    assert (
        parse_arp(
            "ethernet1/4 192.0.2.10 aa:bb:cc:dd:ee:02 incomplete i 1", "paloalto_panos"
        )
        == []
    )
    assert parse_arp("127.0.0.1 0 aa:bb:cc:dd:ee:02 internal", "fortinet") == []
    assert parse_arp("999.0.0.1 0 aa:bb:cc:dd:ee:02 internal", "fortinet") == []


def test_cache_aliases_age_and_interface_are_preserved():
    switches = [{"serial": "SAMPLE001", "hostname": "Core"}]
    rows = fortinet_mac_cache(
        """managed-switch: Core vfid: 0
24 4 aa:bb:cc:dd:ee:02 31 port4
24 5 aa:bb:cc:dd:ee:04 901 port5
managed-switch: Unknown vfid: 0
24 4 aa:bb:cc:dd:ee:06 31 port4""",
        switches,
    )
    assert (
        len(rows) == 1
        and rows[0]["serial"] == "SAMPLE001"
        and rows[0]["if_name"] == "port4"
    )


def test_nxos_flags_arista_routed_interface_junos_irb():
    assert (
        parse_arp("192.0.2.10 00:01:10 aabb.ccdd.ee02 Vlan24 D", "cisco_nxos")[0][
            "interface"
        ]
        == "Vlan24"
    )
    assert (
        parse_arp("192.0.2.10 0 aabb.ccdd.ee02 Vlan24, Ethernet4", "arista_eos")[0][
            "interface"
        ]
        == "Vlan24"
    )
    assert (
        parse_arp(
            "aa:bb:cc:dd:ee:02 192.0.2.10 irb.24[ge-0/0/1.0] none", "juniper_junos"
        )[0]["interface"]
        == "irb.24"
    )


def test_controller_context_selection_and_cache_fallback():
    from app.services.udt_gateway_collection import managed_mac_snapshot
    from app.services.ncm_collection import CaptureError

    calls = []

    def read(command, prompt=None):
        calls.append(command)
        if command.endswith("mac-table"):
            raise CaptureError("Denied", "privilege")
        if command.endswith("mac-cache show"):
            return "managed-switch: Core vfid: 0\n24 4 aa:bb:cc:dd:ee:02 31 port4"
        return ""

    rows = managed_mac_snapshot(
        read,
        [{"serial": "SAMPLE001", "hostname": "Core", "vdom": "root"}],
        "Firewall #",
    )
    assert len(rows) == 1
    assert calls == [
        "config global",
        "execute enter root",
        "diagnose switch-controller switch-info mac-table",
        "diagnose switch-controller mac-cache show",
        "end",
    ]
    assert not any(command.startswith(("set ", "edit ", "clear ")) for command in calls)


def test_unrecognized_mac_response_is_incomplete_not_empty():
    from app.services.udt_gateway_collection import managed_mac_snapshot
    from app.services.ncm_collection import CaptureError

    with pytest.raises(CaptureError):
        managed_mac_snapshot(
            lambda *args: "unexpected firmware response",
            [{"serial": "SAMPLE001", "hostname": "Core"}],
            "Firewall #",
        )


def test_invalid_context_is_not_sent_to_gateway():
    from app.services.udt_gateway_collection import managed_mac_snapshot
    from app.services.ncm_collection import CaptureError

    calls = []
    with pytest.raises(CaptureError):
        managed_mac_snapshot(
            lambda *args: calls.append(args),
            [{"serial": "SAMPLE001", "hostname": "Core", "vdom": "root\nset anything"}],
            "Firewall #",
        )
    assert not calls


def test_ad_query_drains_oldest_batch_and_filters_failed_authentication():
    from app.services.udt_ad_service import _PS_TEMPLATE, UDT_AD_ADVISORY_LOCK
    from app.services.managed_device_service import CHILD_SYNC_ADVISORY_LOCK

    script = _PS_TEMPLATE.format(since_iso="2026-10-11T00:00:00Z", max_events=500)
    assert "-Oldest -MaxEvents 500" in script
    assert "['Status']" in script and "Cursor=$true" in script
    assert UDT_AD_ADVISORY_LOCK != CHILD_SYNC_ADVISORY_LOCK


def test_huawei_vpn_and_spaced_interface_not_guessed_from_last_token():
    rows = parse_arp("192.0.2.10 aabb-ccdd-ee02 20 D-0 GE 1/0/1 tenant", "huawei")
    assert rows == [
        {"ip": "192.0.2.10", "mac": "aa:bb:cc:dd:ee:02", "interface": "GE1/0/1"}
    ]
