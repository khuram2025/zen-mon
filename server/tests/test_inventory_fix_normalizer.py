"""Phase 1 fix round, task be-c: the rule packs and the normaliser (findings UP-01 / UI-01 / DT-02, UP-09,
UP-11, UP-14, UP-15, UP-16, UI-03 / DT-08 on the pack side).

Pure tests pin what each rule pack returns for the titles the reviewers produced. The database tests prove
that the revision bump they need (RULEPACK_REV 1 -> 2) re-keys stored values without writing history.
"""
from __future__ import annotations

import contextlib
import importlib.util
import itertools
import json
import logging
import shutil
import sys

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.services import inventory_normalize as inv
from app.services import inventory_service as svc
from app.services.inventory_normalize import canonical_vendor, normalize_device, normalize_server, normalize_software
from pg_fixture import pg_cluster, pg_db, pg_db_module  # noqa: F401  (fixtures are used by name)

SOFTWARE_PACK = json.loads((inv._DATA_DIR / "software_rules.json").read_text(encoding="utf-8"))


@pytest.fixture
def load_variant(tmp_path):
    """Import a private copy of the engine next to edited copies of the packs; returns the module."""
    counter = itertools.count()

    def load(edit=None):
        root = tmp_path / f"variant{next(counter)}"
        (root / "services").mkdir(parents=True)
        (root / "data" / "inventory").mkdir(parents=True)
        shutil.copy(inv.__file__, root / "services" / "inventory_normalize.py")
        for name in inv.PACK_FILES:
            data = json.loads((inv._DATA_DIR / name).read_text(encoding="utf-8"))
            if edit is not None:
                edit(name, data)
            (root / "data" / "inventory" / name).write_text(json.dumps(data), encoding="utf-8")
        modname = f"inventory_normalize_fix_variant_{next(counter)}"
        spec = importlib.util.spec_from_file_location(modname, root / "services" / "inventory_normalize.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        logging.disable(logging.CRITICAL)
        try:
            spec.loader.exec_module(module)
        finally:
            logging.disable(logging.NOTSET)
            sys.modules.pop(modname, None)
        return module

    return load


def cat(name, publisher=None, source=None):
    return normalize_software(name, publisher, None, source=source).category


# ════════════════════════════════════════════════════════════════════
# UP-01 / UI-01 / DT-02: endpoint protection is its own category
# ════════════════════════════════════════════════════════════════════

# AV, EPP and EDR engines and sensors: the only titles that may satisfy "has endpoint protection".
ENDPOINT_PROTECTION = [
    # the 17 the old 'security' category already held
    ("CrowdStrike Windows Sensor", None), ("Sentinel Agent", None), ("Sophos Endpoint Agent", None),
    ("Trend Micro Deep Security Agent", None), ("Trend Micro Apex One Security Agent", None),
    ("Symantec Endpoint Protection", None), ("McAfee Endpoint Security Platform", None),
    ("ESET Endpoint Antivirus", None), ("Kaspersky Endpoint Security for Windows", None),
    ("Bitdefender Endpoint Security Tools", None), ("VMware Carbon Black Cloud Sensor 64-bit", None),
    ("Cortex XDR", None), ("Cisco Secure Endpoint", None), ("Malwarebytes version 4.6.8.311", None),
    ("Microsoft Defender for Endpoint", None), ("Check Point Endpoint Security", None),
    ("clamav-daemon", "dpkg"),
    # the 18 the reviewer found in 'other'
    ("Avast Free Antivirus", "Avast Software s.r.o."), ("AVG Antivirus", "AVG Technologies"),
    ("Webroot SecureAnywhere", None), ("CylancePROTECT", None), ("Cylance Unified Agent", "BlackBerry"),
    ("Norton Security", None), ("Panda Adaptive Defense 360", None), ("McAfee VirusScan Enterprise", None),
    ("Kaspersky Security for Windows Server", None), ("Microsoft Security Client", None),
    ("System Center Endpoint Protection", None), ("Trend Micro OfficeScan Client", None),
    ("Sophos Network Threat Protection", None), ("Elastic Endpoint", None), ("FortiEDR Collector", None),
    ("Bitdefender Antivirus Free", None), ("Microsoft Defender Antivirus", None), ("Windows Defender", None),
    # Linux sensors
    ("falcon-sensor", "rpm"), ("mdatp", "dpkg"), ("sentinelagent", "rpm"),
]

# Security tooling that is not an endpoint-protection engine. It stays in 'security' (or, for the product's
# own agent, leaves it) and must never satisfy the coverage question.
NOT_ENDPOINT_PROTECTION_SECURITY = [
    ("FortiClient", None), ("McAfee Agent", None), ("Trellix Agent", None), ("ESET Management Agent", None),
    ("Kaspersky Security Center Network Agent", None), ("Wazuh Agent", None), ("Qualys Cloud Security Agent", None),
    ("Nessus Agent (x64)", None), ("Rapid7 Insight Agent", None), ("Zscaler", None), ("Netskope Client", None),
    ("Cisco Umbrella Roaming Client", None), ("Sysmon64", None), ("KeePass Password Safe 2.56", None),
    ("Bitwarden", None), ("ESET PROTECT", None), ("Carbon Black App Control Agent", None),
    ("Malwarebytes Management Console", None), ("fail2ban", "dpkg"), ("wazuh-agent", "rpm"),
]


@pytest.mark.parametrize(("title", "source"), ENDPOINT_PROTECTION, ids=lambda v: str(v))
def test_UP01_antivirus_and_edr_titles_are_endpoint_protection(title, source):
    got = normalize_software(title, None, source=source)
    assert got.category == "endpoint-protection", (title, got.category, got.rule_id)
    assert got.status == "normalized"


@pytest.mark.parametrize(("title", "source"), NOT_ENDPOINT_PROTECTION_SECURITY, ids=lambda v: str(v))
def test_UP01_other_security_tooling_never_counts_as_endpoint_protection(title, source):
    assert cat(title, source=source) == "security", title


def test_UP01_the_zenplus_agent_is_not_security_and_not_endpoint_protection():
    """The agent lists itself in every server's software list, so 'security' made the question vacuous."""
    for publisher in (None, "Zentryc LLC", "Zentryc"):
        got = normalize_software("ZenPlus Agent", publisher, "1.5.4", source="hklm")
        assert got.rule_id == "zenplus-agent"
        assert got.category not in ("security", "endpoint-protection")
        assert got.category == "monitoring"
    assert cat("ZenPlus Agent 1.5.4") == "monitoring"


def test_UP01_endpoint_protection_is_a_declared_category_and_every_rule_uses_a_declared_one():
    assert "endpoint-protection" in inv.CATEGORIES
    assert "endpoint-protection" in SOFTWARE_PACK["categories"]
    assert set(SOFTWARE_PACK["categories"]) == set(inv.CATEGORIES)
    used = {r["category"] for r in SOFTWARE_PACK["rules"] + SOFTWARE_PACK["noise_rules"]}
    assert "endpoint-protection" in used and "security" in used
    # The category fits the software table's column and the API validates against this tuple.
    assert max(len(c) for c in inv.CATEGORIES) <= 32


def test_UP01_every_endpoint_protection_rule_is_an_engine_or_sensor():
    """A guard against the category growing back into 'security': management agents, scanners, SSE and VPN
    clients, password managers and telemetry tools are rejected by name."""
    banned = ("agent", "management", "console", "scan", "vpn", "umbrella", "zscaler", "netskope", "keepass",
              "bitwarden", "sysmon", "wazuh", "forticlient", "nessus", "qualys", "rapid7", "zenplus", "app control")
    allowed_with_agent = {"Sentinel", "Cylance Unified Agent", "Huntress Agent", "Datto EDR", "Sophos Endpoint",
                          "Trend Micro Apex One Security Agent", "Trend Micro Deep Security Agent",
                          "SentinelOne Agent", "Cybereason Sensor", "FortiEDR Collector",
                          "Webroot SecureAnywhere", "Cisco Secure Endpoint", "Cortex XDR Agent"}
    for rule in SOFTWARE_PACK["rules"]:
        if rule["category"] != "endpoint-protection":
            continue
        product = rule["product"]
        low = product.lower()
        for word in banned:
            if word in low:
                assert any(a in product for a in allowed_with_agent), (rule["id"], product, word)


def test_UP01_a_revision_bump_accompanies_the_pack_change():
    assert inv.RULEPACK_REV >= 2


# ════════════════════════════════════════════════════════════════════
# UP-09: the Visual C++ redistributable is one product per side-by-side family
# ════════════════════════════════════════════════════════════════════

VCPP_FAMILIES = {
    "2005": ["Microsoft Visual C++ 2005 Redistributable (x64)", "Microsoft Visual C++ 2005 Redistributable"],
    "2008": ["Microsoft Visual C++ 2008 Redistributable - x64 9.0.30729.6161",
             "Microsoft Visual C++ 2008 Redistributable - x86 9.0.30729.4148"],
    "2010": ["Microsoft Visual C++ 2010  x86 Redistributable - 10.0.40219", "Microsoft Visual C++ 2010 x64 Redistributable - 10.0.40219"],
    "2012": ["Microsoft Visual C++ 2012 x64 Minimum Runtime - 11.0.61030", "Microsoft Visual C++ 2012 x64 Additional Runtime - 11.0.61030",
             "Microsoft Visual C++ 2012 Redistributable (x86) - 11.0.61030"],
    "2013": ["Microsoft Visual C++ 2013 Redistributable (x64) - 12.0.40664", "Microsoft Visual C++ 2013 x86 Minimum Runtime - 12.0.40664"],
    "2015-2022": ["Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.44.35211",
                  "Microsoft Visual C++ 2015-2019 Redistributable (x86) - 14.29.30133",
                  "Microsoft Visual C++ 2017 Redistributable (x64) - 14.16.27033",
                  "Microsoft Visual C++ 2019 X64 Minimum Runtime - 14.29.30133",
                  "Microsoft Visual C++ 2022 X64 Additional Runtime - 14.38.33135",
                  "Microsoft Visual C++ v14 Redistributable (x64) - 14.29.30133"],
}


def test_UP09_each_side_by_side_family_has_its_own_key_and_the_14_family_is_one_key():
    keys = {}
    for family, titles in VCPP_FAMILIES.items():
        got = {normalize_software(t, "Microsoft Corporation").name_key for t in titles}
        assert len(got) == 1, (family, got)
        keys[family] = got.pop()
        for t in titles:
            k = normalize_software(t, "Microsoft Corporation")
            assert k.category == "runtime" and k.status == "normalized" and k.publisher_key == "microsoft", t
    assert len(set(keys.values())) == len(VCPP_FAMILIES), keys
    assert "microsoft-visual-c-plus-plus-redistributable" not in keys.values()
    assert keys["2015-2022"] == "microsoft-visual-c-plus-plus-2015-2022-redistributable"


def test_UP09_a_year_outside_the_known_families_keeps_the_generic_key_and_runtime_category():
    got = normalize_software("Microsoft Visual C++ 2003 Redistributable", "Microsoft Corporation")
    assert (got.name_key, got.category) == ("microsoft-visual-c-plus-plus-redistributable", "runtime")
    # And a security update of a redistributable is still an update, not a runtime.
    assert cat("Security Update for Microsoft Visual C++ 2008 Redistributable") == "update"


def test_UP09_product_names_normalise_to_their_own_key_again():
    for family, titles in VCPP_FAMILIES.items():
        first = normalize_software(titles[0], "Microsoft Corporation")
        again = normalize_software(first.product_name, "Microsoft Corporation")
        assert again.name_key == first.name_key and again.category == "runtime", family


# ════════════════════════════════════════════════════════════════════
# UP-14: the driver noise rule no longer hides database client drivers
# ════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("title", [
    "Oracle ODBC Driver", "Oracle Instant Client 19.3", "PostgreSQL ODBC Driver", "psqlODBC_x64",
    "Microsoft JDBC Driver 12.2 for SQL Server", "IBM Db2 ODBC Driver", "MariaDB ODBC Driver 3.1",
    "Teradata ODBC Driver 17.20", "Microsoft ODBC Driver 17 for SQL Server", "Microsoft OLE DB Driver 19 for SQL Server",
])
def test_UP14_database_client_drivers_are_database_not_driver(title):
    assert cat(title) == "database", title


@pytest.mark.parametrize("title", [
    "Some Vendor ODBC Driver 3.1", "Contoso JDBC Driver 4.0", "Driver Booster 11", "Driver Easy 5.8", "DriverPack Solution",
    "Snappy Driver Installer",
])
def test_UP14_unknown_database_drivers_and_driver_utilities_are_not_hidden_as_drivers(title):
    assert cat(title) != "driver", title


@pytest.mark.parametrize("title", [
    "NVIDIA Graphics Driver 551.23", "Realtek High Definition Audio Driver", "Synaptics Pointing Device Driver",
    "Intel(R) Wireless Bluetooth(R)", "Windows Driver Package - Intel (e1dexpress) Net (11/16/2019 12.19.1.37)",
])
def test_UP14_hardware_drivers_stay_drivers(title):
    assert cat(title) == "driver", title


# ════════════════════════════════════════════════════════════════════
# UP-15: operating systems outside the first tables
# ════════════════════════════════════════════════════════════════════

OS_CASES = [
    # name, os_version, kernel, expected (family, product, release, version)
    ("Microsoft Hyper-V Server 2019", None, "10.0.17763.5696 Build 17763.5696", ("windows", "Windows Server", "2019", "10.0.17763.5696")),
    ("Microsoft Hyper-V Server 2012 R2", None, "6.3.9600 Build 9600", ("windows", "Windows Server", "2012 R2", "6.3.9600")),
    ("Microsoft Azure Stack HCI", "23H2", "10.0.25398.1000 Build 25398.1000", ("windows", "Azure Stack HCI", "23H2", "10.0.25398.1000")),
    ("centos", "8", "4.18.0-553.el8.x86_64", ("linux", "CentOS Stream", "8", "8")),
    ("photon", "4.0", "5.10.78-8.ph4", ("linux", "Photon OS", "4.0", "4.0")),
    ("mariner", "2.0", "5.15.148.2-2.cm2", ("linux", "CBL-Mariner", "2.0", "2.0")),
    ("azurelinux", "3.0", "6.6.64.2-3.azl3", ("linux", "Azure Linux", "3.0", "3.0")),
    ("cloudlinux", "8.9", "4.18.0-513.el8.x86_64", ("linux", "CloudLinux", "8", "8.9")),
    ("scientific", "7.9", "3.10.0-1160.el7.x86_64", ("linux", "Scientific Linux", "7", "7.9")),
    ("xenserver", "8.2", "4.19.0+1", ("linux", "XenServer", "8.2", "8.2")),
    ("pop", "22.04", "6.6.10-76060610-generic", ("linux", "Pop!_OS", "22.04", "22.04")),
    ("flatcar", "3815.2.2", "6.1.90-flatcar", ("linux", "Flatcar Container Linux", "3815", "3815.2.2")),
    ("VMware ESXi 8.0.2", None, None, ("other", "VMware ESXi", "8.0", "8.0.2")),
    ("VMware ESXi", "8.0.2", None, ("other", "VMware ESXi", "8.0", "8.0.2")),
    ("VMware ESXi 7.0 Update 3", None, None, ("other", "VMware ESXi", "7.0", "7.0.3")),
    ("VMware ESXi 8.0.2 build-22380479", None, None, ("other", "VMware ESXi", "8.0", "8.0.2")),
    ("TrueNAS-SCALE-24.04.0", None, None, ("linux", "TrueNAS", "24.04", "24.04.0")),
    ("Proxmox VE 8.2", None, None, ("linux", "Proxmox VE", "8", "8.2")),
    # unchanged behaviour: CentOS Linux 8 and 7, a plain Ubuntu, a plain Windows Server
    ("centos", "8", "4.18.0-348.7.1.el8_5.x86_64", ("linux", "CentOS", "8", "8")),
    ("centos", "7.9.2009", "3.10.0-1160.el7.x86_64", ("linux", "CentOS", "7", "7.9.2009")),
    ("Ubuntu 22.04.4 LTS", None, None, ("linux", "Ubuntu", "22.04", "22.04.4")),
    ("Microsoft Windows Server 2019 Standard", "1809", "10.0.17763.5696 Build 17763.5696", ("windows", "Windows Server", "2019", "10.0.17763.5696")),
]


@pytest.mark.parametrize(("name", "osv", "kernel", "expected"), OS_CASES, ids=[c[0] + "|" + str(c[1]) for c in OS_CASES])
def test_UP15_os_names_outside_the_first_tables_resolve_to_one_product(name, osv, kernel, expected):
    i = normalize_server(name, osv, kernel)
    assert (i.os_family, i.os_product, i.os_release, i.os_version) == expected


def test_UP15_hyper_v_server_keeps_its_edition_and_azure_stack_hci_has_none():
    assert normalize_server("Microsoft Hyper-V Server 2019", None, "10.0.17763.5696 Build 17763.5696").os_edition == "Hyper-V Server"
    assert normalize_server("Microsoft Azure Stack HCI", "23H2", "10.0.25398.1000 Build 25398.1000").os_edition is None


def test_UP15_truenas_core_is_freebsd_based_and_scale_is_linux():
    core = normalize_server("TrueNAS-13.0-U5.3")
    assert (core.os_family, core.os_product) == ("bsd", "TrueNAS CORE")
    assert normalize_server("TrueNAS-SCALE-24.04.0").os_family == "linux"


def test_UP15_esxi_builds_do_not_create_a_product_each():
    products = {normalize_server(n).os_product for n in (
        "VMware ESXi 8.0.2", "VMware ESXi 8.0.3", "VMware ESXi 7.0 Update 3", "VMware ESXi 6.7.0 build-17700523")}
    assert products == {"VMware ESXi"}


@pytest.mark.parametrize(("name", "product", "version"), [
    ("Oracle Solaris 11.4", "Oracle Solaris", "11.4"), ("AIX 7.2", "AIX", "7.2"), ("HP-UX 11.31", "HP-UX", "11.31"),
    ("Plan 9 from Bell Labs", "Plan 9 from Bell Labs", None), ("NewOS v3.4.1 (build 22)", "NewOS", "3.4.1"),
])
def test_UP15_the_fallback_never_puts_a_version_into_os_product(name, product, version):
    i = normalize_server(name)
    assert (i.os_family, i.os_product, i.os_version) == ("other", product, version)


def test_UP15_an_exact_os_version_wins_over_the_one_in_the_name():
    i = normalize_server("Solaris 11.4", "11.4.42")
    assert (i.os_product, i.os_version) == ("Solaris", "11.4.42")


# ════════════════════════════════════════════════════════════════════
# UP-11: HP Inc. is not HPE
# ════════════════════════════════════════════════════════════════════

PRINTER_DESCR = "HP ETHERNET MULTI-ENVIRONMENT,ROM none,JETDIRECT,JD149,EEPROM JDI23600013,CIDATE 11/22/2023"


def test_UP11_a_laserjet_is_hp_inc_whichever_way_the_vendor_arrives():
    for vendor in ("HP", "Hewlett-Packard", "Hewlett Packard", None, "HP Inc."):
        i = normalize_device(vendor=vendor, model="HP LaserJet Pro M404dn", sys_object_id="1.3.6.1.4.1.11.2.3.9.1",
                             sys_descr=PRINTER_DESCR)
        assert (i.vendor_key, i.vendor_label, i.hw_vendor) == ("hp", "HP Inc.", "HP Inc."), vendor
    # The printer subtree alone is enough when the printer reports neither vendor nor model.
    assert normalize_device(sys_object_id="1.3.6.1.4.1.11.2.3.9.1").vendor_key == "hp"
    # And so is the product line in the model.
    assert normalize_device(vendor="HP", model="OfficeJet Pro 9015e").vendor_key == "hp"


def test_UP11_procurve_and_aruba_branded_switches_share_the_aruba_cx_vendor():
    cases = [
        dict(vendor="HP", sys_object_id="1.3.6.1.4.1.11.2.3.7.11.182",
             sys_descr="Aruba JL253A 2930F-48G-4SFP Switch, revision WC.16.11.0012, ROM WC.16.01.0007"),
        dict(vendor="HP", sys_object_id="1.3.6.1.4.1.11.2.3.7.11.147",
             sys_descr="ProCurve J9773A Switch 2530-24G-PoEP, revision YA.16.10.0003"),
        dict(vendor=None, sys_object_id="1.3.6.1.4.1.11.2.3.7.11.147"),
        dict(vendor="Hewlett-Packard", sys_descr="ProCurve J9021A Switch 2810-24G"),
        dict(vendor="Aruba", sys_object_id="1.3.6.1.4.1.47196.4.1.1.1.325"),
    ]
    for c in cases:
        i = normalize_device(**c)
        assert (i.vendor_key, i.vendor_label) == ("aruba", "HPE Aruba Networking"), c


def test_UP11_hpe_servers_stay_hpe_and_hp_inc_pcs_are_hp_inc():
    assert normalize_server("Ubuntu 22.04.4 LTS", hw_manufacturer="HP", hw_model="ProLiant DL360 Gen9").vendor_key == "hpe"
    assert normalize_server("Windows Server 2022", hw_manufacturer="HPE", hw_model="ProLiant DL380 Gen10").vendor_key == "hpe"
    assert normalize_server("Microsoft Windows 11 Pro", hw_manufacturer="HP", hw_model="HP EliteBook 840 G8 Notebook PC").vendor_key == "hp"
    assert normalize_server("Microsoft Windows 11 Pro", hw_manufacturer="HP", hw_model="HP ProDesk 600 G5 Desktop Mini").vendor_key == "hp"
    assert normalize_server("Windows Server 2019", hw_manufacturer="HP").vendor_key == "hpe"        # nothing to tell them apart
    assert normalize_device(vendor="HPE", model="ProLiant DL380 Gen10").vendor_key == "hpe"
    assert normalize_device(sys_object_id="1.3.6.1.4.1.232.9.2").vendor_key == "hpe"
    assert normalize_device(vendor="HP", model="ProLiant DL360 Gen9").vendor_key == "hpe"


def test_UP11_software_publishers_are_not_refined():
    assert canonical_vendor("HP") == ("hpe", "HPE")
    assert canonical_vendor("HP Inc.") == ("hp", "HP Inc.")


# ════════════════════════════════════════════════════════════════════
# UP-16: vendor spellings and the MikroTik version
# ════════════════════════════════════════════════════════════════════

VENDOR_GROUPS = [
    ("paloalto", ["Palo Alto Networks", "PaloAlto Networks", "Palo Alto Networks, Inc.", "Palo Alto"]),
    ("brocade", ["Brocade", "Brocade Communications Systems, Inc."]),
    ("ruckus", ["Ruckus Networks", "Ruckus Wireless", "CommScope Ruckus"]),
    ("kyocera", ["Kyocera", "KYOCERA Document Solutions", "Kyocera Mita"]),
    ("brother", ["Brother", "Brother Industries, Ltd."]),
    ("schneider", ["APC by Schneider Electric", "Schneider Electric", "APC", "American Power Conversion"]),
    ("mikrotik", ["MikroTik", "Routerboard", "Mikrotikls SIA"]),
]


@pytest.mark.parametrize(("key", "spellings"), VENDOR_GROUPS, ids=[g[0] for g in VENDOR_GROUPS])
def test_UP16_one_vendor_has_one_key_and_one_label(key, spellings):
    got = {canonical_vendor(s) for s in spellings}
    assert len(got) == 1, got
    assert next(iter(got))[0] == key


def test_UP16_the_apc_enterprise_number_maps_to_the_same_vendor():
    assert normalize_device(sys_object_id="1.3.6.1.4.1.318.1.3.2").vendor_key == "schneider"


def test_UP16_a_mikrotik_board_name_is_the_model_not_the_routeros_version():
    for board, descr in (("RB4011iGS", "RouterOS RB4011iGS+"), ("CCR1036", "RouterOS CCR1036-8G-2S+"), ("hAP", "RouterOS hAP ac2")):
        i = normalize_device(vendor="MikroTik", os_version=board, sys_descr=descr, sys_object_id="1.3.6.1.4.1.14988.1")
        assert (i.os_family, i.os_product, i.os_version) == ("routeros", "RouterOS", None), board
        assert i.hw_model and i.hw_model.startswith(descr.split(" ", 1)[1].split()[0].rstrip("+")), (board, i.hw_model)
    i = normalize_device(vendor="MikroTik", os_version="RB4011iGS", sys_descr="RouterOS RB4011iGS+", sys_object_id="1.3.6.1.4.1.14988.1")
    assert i.hw_model == "RB4011iGS+" and i.hw_source == "device"


def test_UP16_a_real_routeros_version_is_still_read_and_a_reported_model_wins():
    i = normalize_device(vendor="MikroTik", os_version="7.14.1", model="RB5009UG+S+", sys_descr="RouterOS RB5009UG+S+IN",
                         sys_object_id="1.3.6.1.4.1.14988.1")
    assert (i.os_version, i.hw_model) == ("7.14.1", "RB5009UG+S+")
    i = normalize_device(vendor="MikroTik", sys_descr="RouterOS 6.49.10 (long-term) on RB750Gr3", sys_object_id="1.3.6.1.4.1.14988.1")
    assert i.os_version == "6.49.10" and i.hw_model is None   # a version in sysDescr is never taken for a model


def test_UP16_other_families_keep_an_odd_version_string_as_evidence():
    i = normalize_device(vendor="Fortinet", profile_name="FortiGate", os_version="weird-7")
    assert i.os_version == "weird-7"       # strict_version is a RouterOS-only rule


# ════════════════════════════════════════════════════════════════════
# Pack structure
# ════════════════════════════════════════════════════════════════════

def test_the_new_pack_sections_loaded_without_errors():
    assert inv.PACK_ERRORS == []
    assert [r.id for r in inv._REFINEMENTS][:2] == ["procurve-switch-oid", "hp-inc-printer-oid"]
    assert {p.id for p in inv._Windows.platforms} == {"hyper-v-server", "azure-stack-hci"}
    assert any(d.kernel_rx is not None for d in inv._DISTROS)
    assert inv._Net.families["routeros"].strict_version and inv._Net.families["routeros"].descr_model_rx is not None


def test_a_wrong_typed_refinement_is_a_pack_error_not_an_import_failure(load_variant):
    def edit(name, data):
        if name == inv.PACK_VENDORS:
            data["vendor_refinements"].append({"id": "bad", "from": ["nope"], "to": "hp", "text_re": "x"})
            data["vendor_refinements"].append({"id": "bad2", "from": ["hpe"], "to": "hp"})
            data["vendor_refinements"].append("text")

    mod = load_variant(edit=edit)
    assert len(mod.PACK_ERRORS) == 3 and all(e.startswith("vendor_aliases.json:") for e in mod.PACK_ERRORS)
    assert mod.normalize_device(vendor="HP", model="LaserJet").vendor_key == "hp"    # the good entries still apply


# ════════════════════════════════════════════════════════════════════
# UI-03 / DT-08 (server side): runtime is hidden by default
# ════════════════════════════════════════════════════════════════════

def test_UI03_runtime_is_in_the_default_hidden_categories():
    assert svc.DEFAULT_SETTINGS["hide_categories_by_default"] == ["update", "driver", "os-component", "runtime"]
    assert svc.merge_inventory_settings(None)["hide_categories_by_default"] == ["update", "driver", "os-component", "runtime"]


def test_UI03_an_install_that_already_saved_its_hidden_categories_keeps_them():
    """The stored blob wins: only an install that never saved the key gets the new default."""
    old = {"hide_categories_by_default": ["update", "driver", "os-component"], "stale_days": 14}
    out = svc.merge_inventory_settings(old)
    assert out["hide_categories_by_default"] == ["update", "driver", "os-component"]
    assert out["stale_days"] == 14
    assert svc.merge_inventory_settings({"hide_categories_by_default": []})["hide_categories_by_default"] == []
    assert svc.merge_inventory_settings({"hide_categories_by_default": ["runtime"]})["hide_categories_by_default"] == ["runtime"]
    # A blob saved by an older release without the key, and one with only other settings, get the new default.
    assert svc.merge_inventory_settings({"stale_days": 3})["hide_categories_by_default"] == [
        "update", "driver", "os-component", "runtime"]
    # A stored list may name the new category.
    assert svc.merge_inventory_settings({"hide_categories_by_default": ["endpoint-protection", "bogus"]})[
        "hide_categories_by_default"] == ["endpoint-protection"]
    # A mistake (nothing but unknown names) falls back to the current default.
    assert svc.merge_inventory_settings({"hide_categories_by_default": ["bogus"]})["hide_categories_by_default"] == [
        "update", "driver", "os-component", "runtime"]


# ════════════════════════════════════════════════════════════════════
# A revision change re-keys stored values and never writes history
# ════════════════════════════════════════════════════════════════════

_seq = itertools.count(1)


@pytest.fixture
def inv_db(pg_db):
    pg_db.execute("DELETE FROM servers; DELETE FROM devices")
    return pg_db


@contextlib.asynccontextmanager
async def sessions(db):
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def tick(db, state=None):
    async with sessions(db) as factory:
        return await svc.run_normalise_tick(factory, state=state or svc.TickState())


def event_count(db):
    return db.scalar("SELECT count(*) FROM inventory_change_events")


def add_server(db, name, **cols):
    names = ["display_name", *cols]
    marks = ", ".join(f"${i + 1}" for i in range(len(names)))
    return db.scalar(f"INSERT INTO servers ({', '.join(names)}) VALUES ({marks}) RETURNING id", name, *cols.values())


def add_software(db, server_id, package, vendor=None, version="1.0", source="hklm"):
    db.execute("INSERT INTO server_software_inventory (server_id, package_name, version, vendor, source) "
               "VALUES ($1, $2, $3, $4, $5)", server_id, package, version, vendor, source)


def stored_software(db, server_id):
    return {r[0]: r[1:] for r in db.query(
        "SELECT package_name, name_key, category, norm_rev FROM server_software_inventory WHERE server_id = $1",
        server_id)}


@pytest.mark.asyncio
async def test_UP01_a_rule_pack_revision_re_keys_software_without_writing_change_events(inv_db):
    """Revision 1 filed the agent and every AV title wrongly; after the bump the rows are re-keyed in place."""
    srv = add_server(inv_db, "win-ep", os_type="windows", os_name="Microsoft Windows Server 2022 Standard",
                     kernel_or_build="10.0.20348.587 Build 20348.587")
    for title, publisher in (("ZenPlus Agent", "Zentryc LLC"), ("Avast Free Antivirus", "Avast Software s.r.o."),
                             ("CrowdStrike Windows Sensor", "CrowdStrike, Inc."),
                             ("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.44.35211", "Microsoft Corporation")):
        add_software(inv_db, srv, title, publisher)
    rev = inv.RULEPACK_REV
    # What revision 1 stored: the agent as security, Avast as an unmatched 'other', the old VC++ key.
    old = {
        "ZenPlus Agent": ("zenplus-agent", "security"),
        "Avast Free Antivirus": ("avast-software-avast-free-antivirus", "other"),
        "CrowdStrike Windows Sensor": ("crowdstrike-falcon-sensor", "security"),
        "Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.44.35211": ("microsoft-visual-c-plus-plus-redistributable", "runtime"),
    }
    for pkg, (key, category) in old.items():
        inv_db.execute("UPDATE server_software_inventory SET name_key = $3, category = $4, publisher_key = 'x', norm_rev = $5 "
                       "WHERE server_id = $1 AND package_name = $2", srv, pkg, key, category, rev - 1)
    inv_db.execute("DELETE FROM inventory_change_events")
    assert event_count(inv_db) == 0

    result = await tick(inv_db)

    assert result.software == 4
    got = stored_software(inv_db, srv)
    assert got["ZenPlus Agent"] == ("zenplus-agent", "monitoring", rev)
    assert got["Avast Free Antivirus"] == ("avast-antivirus", "endpoint-protection", rev)
    assert got["CrowdStrike Windows Sensor"] == ("crowdstrike-falcon-sensor", "endpoint-protection", rev)
    assert got["Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.44.35211"][:2] == (
        "microsoft-visual-c-plus-plus-2015-2022-redistributable", "runtime")
    assert inv_db.scalar("SELECT count(*) FROM server_software_inventory WHERE category = 'security'") == 0
    assert event_count(inv_db) == 0            # re-keying never reads as an install, upgrade or removal


@pytest.mark.asyncio
async def test_UP15_a_rule_pack_revision_re_renders_identity_values_without_writing_history(inv_db):
    """An ESXi host stored by revision 1 (the build in the product, no version) gets its version at the bump;
    that is a new rendering of the same source, not a change."""
    srv = add_server(inv_db, "esx-1", os_name="VMware ESXi 8.0.2", os_type="other")
    await tick(inv_db)
    rev = inv.RULEPACK_REV
    row = inv_db.query("SELECT os_family, os_product, os_release, os_version, norm_rev, values_rev FROM inventory_identity "
                       "WHERE asset_kind = 'server' AND asset_id = $1", srv)[0]
    assert row == ("other", "VMware ESXi", "8.0", "8.0.2", rev, rev)
    inv_db.psql("BEGIN;\nSELECT set_config('zenplus.inventory_suppress', 'on', true);\n"
                "UPDATE inventory_identity SET os_product = 'VMware ESXi 8.0.2', os_release = NULL, os_version = NULL, "
                f"norm_rev = {rev - 1}, values_rev = {rev - 1} WHERE asset_id = '{srv}';\nCOMMIT;")
    inv_db.execute("DELETE FROM inventory_change_events")

    result = await tick(inv_db)

    assert result.stale == 1 and result.dirty == 0
    row = inv_db.query("SELECT os_product, os_release, os_version, norm_rev, values_rev FROM inventory_identity "
                       "WHERE asset_kind = 'server' AND asset_id = $1", srv)[0]
    assert row == ("VMware ESXi", "8.0", "8.0.2", rev, rev)
    assert event_count(inv_db) == 0
    # The first real change after the bump is still recorded.
    inv_db.execute("UPDATE servers SET os_name = 'VMware ESXi 8.0.3' WHERE id = $1", srv)
    await tick(inv_db)
    got = inv_db.query("SELECT category, action, old_value, new_value FROM inventory_change_events")
    assert got == [("os", "changed", "8.0.2", "8.0.3")]


# ════════════════════════════════════════════════════════════════════
# The API sees the new category and the new default
# ════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_UP01_the_software_api_lists_and_filters_endpoint_protection(inv_db):
    from inventory_fleet import api_client

    srv = add_server(inv_db, "win-api", os_type="windows", os_name="Microsoft Windows Server 2022 Standard",
                     kernel_or_build="10.0.20348.587 Build 20348.587")
    other = add_server(inv_db, "win-api-2", os_type="windows", os_name="Microsoft Windows Server 2022 Standard",
                       kernel_or_build="10.0.20348.587 Build 20348.587")
    for title, publisher in (("CrowdStrike Windows Sensor", "CrowdStrike, Inc."), ("ZenPlus Agent", "Zentryc LLC"),
                             ("Google Chrome", "Google LLC"), ("Zscaler", "Zscaler, Inc."),
                             ("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.44.35211", "Microsoft Corporation")):
        add_software(inv_db, srv, title, publisher)
    add_software(inv_db, other, "Google Chrome", "Google LLC")
    await tick(inv_db)

    with api_client(inv_db, role="t_ep_view", permissions=["inventory.view"]) as client:
        resp = client.get("/api/v1/inventory/software", params={"category": "endpoint-protection"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert [(i["product_key"], i["category"]) for i in body["items"]] == [("crowdstrike-falcon-sensor", "endpoint-protection")]
        assert body["total"] == 1

        resp = client.get("/api/v1/inventory/software", params={"category": "security"})
        assert [i["product_key"] for i in resp.json()["items"]] == ["zscaler-client-connector"]     # the agent is not here

        default = client.get("/api/v1/inventory/software").json()
        assert default["hidden_categories"] == ["driver", "os-component", "runtime", "update"]
        keys = {i["product_key"] for i in default["items"]}
        assert "crowdstrike-falcon-sensor" in keys and "google-chrome" in keys
        assert not any("visual-c-plus-plus" in k for k in keys), "runtimes are hidden by default (UI-03)"
        assert default["hidden_products"] == 1

        shown = client.get("/api/v1/inventory/software", params={"hidden": "include"}).json()
        assert "microsoft-visual-c-plus-plus-2015-2022-redistributable" in {i["product_key"] for i in shown["items"]}

        assert client.get("/api/v1/inventory/software", params={"category": "no-such-category"}).status_code == 422
