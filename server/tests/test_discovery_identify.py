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
