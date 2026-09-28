from app.services.discovery_identify import identify


def _snmp_identity(sys_object_id: str, sys_descr: str) -> dict:
    return identify(
        {
            "snmp": [
                {
                    "responsive": True,
                    "data": {
                        "sys_object_id": sys_object_id,
                        "sys_descr": sys_descr,
                        "sys_name": "test-switch",
                    },
                }
            ]
        },
        ["snmp"],
    )


def test_identifies_aruba_aos_cx_as_switch():
    result = _snmp_identity(
        "1.3.6.1.4.1.47196.4.1.1.1.325",
        "Aruba JL659A 6300M running ArubaOS-CX FL.10.12",
    )
    assert result["vendor"] == "Aruba"
    assert result["device_type"] == "switch"
    assert result["os"] == "AOS-CX"


def test_identifies_dell_os10_before_generic_dell_server_tree():
    result = _snmp_identity(
        "1.3.6.1.4.1.674.11000.5000.100.2.1.19",
        "Dell EMC Networking OS10 Enterprise",
    )
    assert result["vendor"] == "Dell Technologies"
    assert result["device_type"] == "switch"
    assert result["os"] == "OS10"


def test_catalyst_with_symbolic_oid_is_a_cisco_switch():
    # ZP-04 / ZP-05: net-snmp without MIBs prints iso.3.6…; a 2960X must still
    # resolve to Cisco and be classed as a switch, not "other"/"router".
    result = _snmp_identity(
        "iso.3.6.1.4.1.9.1.1208",
        "Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(4)E8, RELEASE SOFTWARE (fc2)",
    )
    assert result["vendor"] == "Cisco"
    assert result["device_type"] == "switch"
    assert result["sys_object_id"] == ".1.3.6.1.4.1.9.1.1208"


def test_cisco_isr_stays_a_router():
    result = _snmp_identity(
        ".1.3.6.1.4.1.9.1.1041",
        "Cisco IOS Software, C2900 Software (C2900-UNIVERSALK9-M), Version 15.7(3)M5",
    )
    assert result["device_type"] == "router"


def test_cisco_asa_stays_a_firewall():
    result = _snmp_identity(
        "1.3.6.1.4.1.9.1.2313",
        "Cisco Adaptive Security Appliance Version 9.16(4)",
    )
    assert result["device_type"] == "firewall"
