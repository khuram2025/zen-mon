"""Unit tests for services/inventory_normalize.py (Inventory design doc 04, test T2).

Pure functions only: no database, no network. Golden fixtures live in
``fixtures/inventory_golden.json`` and cover every row of the doc 02 section 4
tables plus values recorded on the reference appliance. The rule-pack tests
import private copies of the module next to edited copies of the packs (in a
temporary directory) to prove that a broken pack degrades instead of crashing
the import, and one test starts subprocesses with different PYTHONHASHSEED values.
"""
from __future__ import annotations

import dataclasses
import hashlib
import importlib.util
import inspect
import itertools
import json
import logging
import random
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import pytest

from app.services import inventory_normalize as inv
from app.services import versioning as ver
from app.services.inventory_normalize import (
    Identity,
    SoftwareKey,
    canonical_vendor,
    normalize_device,
    normalize_server,
    normalize_software,
)

GOLDEN = json.loads((Path(__file__).parent / "fixtures" / "inventory_golden.json").read_text(encoding="utf-8"))
REPO_ROOT = Path(__file__).resolve().parents[2]
SOFTWARE_PACK = json.loads((inv._DATA_DIR / "software_rules.json").read_text(encoding="utf-8"))


def _check(actual, expect: dict) -> None:
    got = {k: getattr(actual, k) for k in expect}
    assert got == expect


# ── Golden fixtures ──────────────────────────────────────────────────

@pytest.mark.parametrize("case", GOLDEN["servers"], ids=lambda c: c["id"])
def test_golden_servers(case):
    inp = case["input"]
    ident = normalize_server(
        inp["os_name"], inp["os_version"], inp["kernel_or_build"], inp["architecture"],
        hw_manufacturer=inp.get("hw_manufacturer"), hw_model=inp.get("hw_model"), hw_serial=inp.get("hw_serial"),
    )
    assert isinstance(ident, Identity)
    _check(ident, case["expect"])


@pytest.mark.parametrize("case", GOLDEN["devices"], ids=lambda c: c["id"])
def test_golden_devices(case):
    ident = normalize_device(**case["input"])
    assert isinstance(ident, Identity)
    _check(ident, case["expect"])


@pytest.mark.parametrize("row", GOLDEN["vendors"], ids=lambda r: f"{r[0]!r}")
def test_golden_vendors(row):
    raw, key, label = row
    assert canonical_vendor(raw) == (key, label)


@pytest.mark.parametrize("case", GOLDEN["software"], ids=lambda c: c["id"])
def test_golden_software(case):
    inp = case["input"]
    key = normalize_software(inp["name"], inp["publisher"], inp["version"], source=inp["source"])
    assert isinstance(key, SoftwareKey)
    _check(key, case["expect"])


PAIRED_DEVICES = [c for c in GOLDEN["devices"] if c.get("agrees_without_sys_descr")]


@pytest.mark.parametrize("case", PAIRED_DEVICES, ids=lambda c: c["id"])
def test_sysdescr_does_not_change_the_os_identity(case):
    """The poller starts writing sys_descr after migration 154: rows that were right without it must stay right."""
    inp = dict(case["input"])
    with_descr = normalize_device(**inp)
    inp.pop("sys_descr")
    without = normalize_device(**inp)
    for field in ("os_family", "os_product", "os_release", "os_version"):
        assert getattr(with_descr, field) == getattr(without, field), field


def test_the_paired_cases_cover_both_cisco_trains():
    assert {c["expect"]["os_family"] for c in PAIRED_DEVICES} == {"ios", "ios-xe"}
    assert len(PAIRED_DEVICES) >= 8


# ── Required behaviours called out in the task ───────────────────────

def test_chrome_google_llc_and_google_inc_share_a_product_key():
    a = normalize_software("Google Chrome", "Google LLC", "151.0.7922.110")
    b = normalize_software("Google Chrome", "Google Inc.", "151.0.7922.110")
    assert a == b
    assert a.name_key == "google-chrome" and a.publisher_key == "google"


def test_x64_and_x86_variants_share_a_name_key():
    pairs = [
        ("Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.44.35211", "Microsoft Visual C++ 2015-2022 Redistributable (x86) - 14.44.35211"),
        ("Notepad++ (64-bit x64)", "Notepad++"),
        ("Java 8 Update 461 (64-bit)", "Java 8 Update 461"),
        ("Python 3.12.10 (64-bit)", "Python 3.12.10 (32-bit)"),
        ("Acme Widget Pro 2.3.1 (x64)", "Acme Widget Pro 2.3.1 (x86)"),
        ("Zoom Workplace (64-bit)", "Zoom(32bit)"),
        ("Mozilla Firefox 125.0.1 (x64 en-US)", "Mozilla Firefox 125.0.1 (x86 en-US)"),
    ]
    for a, b in pairs:
        ka, kb = normalize_software(a, "Vendor Inc."), normalize_software(b, "Vendor Inc.")
        assert ka.name_key == kb.name_key, (a, b)


def test_unknown_software_gets_a_stable_unmatched_key():
    first = normalize_software("Contoso Payroll Connector 12.4.1 (x64)", "Contoso Ltd")
    again = normalize_software("Contoso Payroll Connector 12.4.1 (x64)", "Contoso Ltd")
    other_version = normalize_software("Contoso Payroll Connector 12.5.0 (x86)", "Contoso Ltd")
    assert first == again
    assert first.status == "unmatched" and first.category == "other" and first.rule_id is None
    assert first.name_key == other_version.name_key == "contoso-payroll-connector"


def test_normalising_the_output_name_again_gives_the_same_key():
    cases = [(ex, None) for rule in SOFTWARE_PACK["rules"] + SOFTWARE_PACK["noise_rules"]
             for ex in rule["examples"] if not rule.get("sources")]
    cases += [
        ("Contoso Payroll Connector 12.4.1 (x64)", "Contoso Ltd"),
        ("Acme Studio Professional Edition 4.1", "Acme Corp"),
        ("Google Foo Tool 3.2", "Google LLC"),
        ("Security Update for Microsoft Office 2016 (KB4011281)", "Microsoft Corporation"),
        ("NVIDIA Graphics Driver 551.23", "NVIDIA Corporation"),
        ("PuTTY release 0.83 (64-bit)", "Simon Tatham"),
        ("Foo++ Toolkit v2.0 copyright 2020", None),
    ]
    for name, publisher in cases:
        first = normalize_software(name, publisher)
        second = normalize_software(first.product_name, publisher)
        assert second.name_key == first.name_key, (name, first.name_key, second.name_key)
        assert second.category == first.category, name
    for rule in SOFTWARE_PACK["rules"]:
        if not rule.get("sources"):
            product = normalize_software(rule["product"], None)
            assert product.name_key == normalize_software(rule["examples"][0], None).name_key, rule["id"]


def test_every_golden_and_probe_title_keys_the_same_way_again():
    """Words kept after a version (N-16) and hashes of non-ASCII names (N-08) must not break idempotence."""
    inputs = [c["input"] for c in GOLDEN["software"]] + GOLDEN["engine_probe"]["software"]
    checked = 0
    for i in inputs:
        if not i["name"] or len(i["name"]) > 200:  # nameless titles and names cut at 255 characters are not re-keyable
            continue
        first = normalize_software(i["name"], i["publisher"], i["version"], source=i["source"])
        again = normalize_software(first.product_name, i["publisher"], i["version"], source=i["source"])
        assert (again.name_key, again.category, again.publisher_key) == (first.name_key, first.category, first.publisher_key), i
        checked += 1
    assert checked > 200


def test_publisher_aliasing_is_consistent_between_vendor_and_software():
    for raw in ("Google LLC", "Google Inc.", "GOOGLE LLC", " Google  Inc "):
        assert canonical_vendor(raw) == ("google", "Google")
        k = normalize_software("Some Unlisted Google Tool 1.0", raw)
        assert (k.publisher_key, k.publisher_label) == ("google", "Google")
    a = normalize_software("Totally Unknown App", "Zeta Systems, Inc.")
    b = normalize_software("Totally Unknown App", "Zeta Systems Inc")
    c = normalize_software("Totally Unknown App", "ZETA SYSTEMS LLC")
    assert a.publisher_key == b.publisher_key == c.publisher_key == "zeta-systems"
    assert a.name_key == b.name_key == c.name_key


def test_determinism_and_order_independence():
    items = [(c["input"]["name"], c["input"]["publisher"], c["input"]["version"], c["input"]["source"]) for c in GOLDEN["software"]]
    inv._software_cached.cache_clear()
    forward = [normalize_software(n, p, v, source=s) for n, p, v, s in items]
    inv._software_cached.cache_clear()
    backward = [normalize_software(n, p, v, source=s) for n, p, v, s in reversed(items)][::-1]
    assert forward == backward
    assert forward == [normalize_software(n, p, v, source=s) for n, p, v, s in items]  # warm cache
    for case in GOLDEN["devices"]:
        assert normalize_device(**case["input"]) == normalize_device(**case["input"])


def test_results_are_immutable():
    ident = normalize_server("Ubuntu 22.04.4 LTS")
    key = normalize_software("Google Chrome", "Google LLC")
    with pytest.raises(dataclasses.FrozenInstanceError):
        ident.os_family = "x"  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        key.name_key = "x"  # type: ignore[misc]


# ── Doc-pinned interface ─────────────────────────────────────────────

def test_rulepack_rev_and_defaults():
    assert isinstance(inv.RULEPACK_REV, int) and inv.RULEPACK_REV >= 1
    blank = Identity()
    assert blank.os_family == "unknown"
    assert all(getattr(blank, f.name) is None for f in dataclasses.fields(Identity) if f.name != "os_family")
    assert normalize_server().os_family == "unknown"
    assert normalize_device().os_family == "unknown"


def test_lifecycle_slug_is_never_set_in_this_phase():
    for case in GOLDEN["devices"]:
        assert normalize_device(**case["input"]).lifecycle_slug is None
    for case in GOLDEN["servers"]:
        i = case["input"]
        assert normalize_server(i["os_name"], i["os_version"], i["kernel_or_build"], i["architecture"]).lifecycle_slug is None


def test_identity_matches_the_migration_columns():
    sql = REPO_ROOT / "scripts" / "migrate-154-inventory-foundation.sql"
    if not sql.exists():
        pytest.skip("migrate-154 is not in this checkout")
    text = sql.read_text(encoding="utf-8")
    m = re.search(r"CREATE TABLE IF NOT EXISTS inventory_identity \((.*?)\n\);", text, re.S)
    assert m, "inventory_identity DDL not found"
    widths = {name: int(w) for name, w in re.findall(r"^\s+([a-z_]+)\s+varchar\((\d+)\)", m.group(1), re.M)}
    for f in dataclasses.fields(Identity):
        assert f.name in widths, f"{f.name} is not an inventory_identity column"
        assert inv._CAPS[f.name] == widths[f.name], f"{f.name} cap differs from varchar({widths[f.name]})"
    for col, cap in (("name_key", inv._NAME_KEY_MAX), ("publisher_key", inv._PUBLISHER_KEY_MAX), ("category", 32)):
        cm = re.search(rf"ADD COLUMN IF NOT EXISTS {col}\s+varchar\((\d+)\)", text)
        assert cm and int(cm.group(1)) == cap, col


def test_every_software_category_is_in_the_documented_set():
    documented = {"browser", "security", "remote-access", "database", "web-server", "runtime", "development",
                  "productivity", "monitoring", "backup", "virtualization", "networking", "driver", "os-component",
                  "update", "other"}
    assert set(inv.CATEGORIES) == documented
    assert set(SOFTWARE_PACK["categories"]) == documented
    for rule in SOFTWARE_PACK["rules"] + SOFTWARE_PACK["noise_rules"]:
        assert rule["category"] in documented, rule["id"]
    cats_used = {r["category"] for r in SOFTWARE_PACK["rules"] + SOFTWARE_PACK["noise_rules"]}
    assert cats_used == documented  # no dead category


# ── Vendor details ───────────────────────────────────────────────────

def test_vendor_canonicalisation_edge_cases():
    assert canonical_vendor("Contoso Networks, Inc.") == ("contoso-networks", "Contoso Networks")
    assert canonical_vendor("PAN") == ("paloalto", "Palo Alto Networks")
    assert canonical_vendor("PAN Pacific Systems")[0] != "paloalto"  # 'PAN' only matches as the whole value
    assert canonical_vendor("HP Inc.") == ("hp", "HP Inc.")  # HP Inc. and HPE are different vendors
    assert canonical_vendor("HP Enterprise")[0] == "hpe"
    key, label = canonical_vendor("x" * 5000)
    assert len(key) <= 64 and len(label) <= 128
    assert canonical_vendor("ベンダー")[0].startswith("n-")  # non-Latin names stay distinct and stable
    assert canonical_vendor("ベンダー") == canonical_vendor("ベンダー")


def test_sysobjectid_enterprise_number_supplies_a_missing_vendor():
    ident = normalize_device(vendor=None, sys_object_id=".1.3.6.1.4.1.12356.101.1.1234")
    assert (ident.vendor_key, ident.os_family) == ("fortinet", "fortios")
    ident = normalize_device(vendor="", sys_object_id="1.3.6.1.4.1.8741.1")
    assert ident.vendor_key == "sonicwall" and ident.os_family == "unknown"
    # An explicit vendor wins over the enterprise number.
    assert normalize_device(vendor="Cisco", sys_object_id="1.3.6.1.4.1.12356.101.1.1").vendor_key == "cisco"
    # A longer prefix beats a shorter one: Nexus is NX-OS, other Cisco is undecided from the OID alone.
    assert normalize_device(sys_object_id="1.3.6.1.4.1.9.12.3.1.3.7").os_family == "nx-os"
    assert normalize_device(sys_object_id="1.3.6.1.4.1.9.1.2695").os_family == "unknown"


def test_cisco_family_is_resolved_from_the_version_shape_never_guessed():
    f = lambda v: normalize_device(vendor="Cisco", os_version=v).os_family  # noqa: E731
    assert f("17.9.4a") == "ios-xe"
    assert f("16.12.3") == "ios-xe"
    assert f("15.2(4)E8") == "ios"
    assert f("12.2(55)SE5") == "ios"
    assert f("9.16(4)") == "unknown"  # ASA and NX-OS look the same: no guess
    assert f(None) == "unknown"
    assert normalize_device(vendor="Cisco", profile_name="Cisco IOS/IOS-XE Switch & Router").os_family == "unknown"


def test_model_hints_only_apply_to_the_matching_vendor():
    assert normalize_device(vendor="Fortinet", model="FS-148E").os_family == "fortiswitchos"
    assert normalize_device(vendor="FS.com", model="FS-S5800-48T4S").os_family in ("unknown", "other")
    assert normalize_device(vendor="Palo Alto Networks", model="PA-3220").os_family == "panos"
    assert normalize_device(vendor="Fortinet", model="FGT_200F").hw_model == "FortiGate-200F"
    assert normalize_device(vendor="Fortinet", model="FGT_VM64_KVM").hw_model == "FortiGate-VM64-KVM"
    assert normalize_device(vendor="Fortinet", model="FortiGate").hw_model == "FortiGate"
    assert normalize_device(vendor="Cisco", model="FGT_200F").hw_model == "FGT_200F"


def test_device_template_values_may_be_missing_or_malformed():
    base = dict(vendor="Fortinet", profile_name="Fortinet FortiGate", os_version="v7.2.5,build1517,230606 (GA.F)")
    for tv in (None, {}, {"fgt_fw_version": ""}, {"fgt_fw_version": None}, {"fgt_fw_version": 7.2}, {5: "x"}, "oops", ["a"]):
        ident = normalize_device(template_values=tv, **base)  # type: ignore[arg-type]
        assert ident.os_version == "7.2.5" and ident.os_source == "profile"


# ── Never raises, bounded input ──────────────────────────────────────

_GARBAGE = [None, "", " ", "\x00", "\x00\x01\x02", "\n\t\r", 0, -1, 3.14, True, False, b"bytes", b"\xff\xfe", [], {}, (), object(),
            "\ud800", "‮​", "null", "None", "NaN", "'; DROP TABLE servers; --", "%s%s%n", "${jndi:ldap://x}", "A" * 100_000,
            "\U0001f600" * 2000]


def test_functions_never_raise_on_garbage():
    for g in _GARBAGE:
        assert isinstance(normalize_server(g, g, g, g, cpu_model=g, hw_manufacturer=g, hw_model=g, hw_serial=g), Identity)
        assert isinstance(
            normalize_device(vendor=g, model=g, os_version=g, firmware_version=g, serial_number=g, sys_object_id=g,
                             sys_descr=g, profile_name=g, template_values=g),  # type: ignore[arg-type]
            Identity)
        assert isinstance(normalize_software(g, g, g, source=g), SoftwareKey)
        key, label = canonical_vendor(g)
        assert (key is None) == (label is None)


def test_garbage_gives_the_unknown_or_unmatched_result():
    assert normalize_server(None) == Identity()
    assert normalize_device() == Identity()
    for g in (None, "", "   ", [], {}):
        assert normalize_software(g).status == "unmatched" and normalize_software(g).category == "other"
        assert normalize_software(g).name_key == "unnamed-software"


def test_output_fields_respect_column_widths():
    huge = "Microsoft Windows Server 2022 " + "X" * 5000
    ident = normalize_server(huge, "1" * 5000, "10.0.20348.587 Build 20348.587 " + "9" * 5000, "x" * 5000,
                             hw_manufacturer="V" * 5000, hw_model="M" * 5000, hw_serial="S" * 5000)
    dev = normalize_device(vendor="V" * 5000, model="M" * 5000, os_version="7." + "9" * 5000, firmware_version="F" * 5000,
                           serial_number="S" * 5000, sys_object_id="1." * 5000, sys_descr="D" * 5000, profile_name="P" * 5000,
                           template_values={"fgt_fw_version": "v" + "7" * 5000})
    for obj in (ident, dev):
        for f in dataclasses.fields(Identity):
            value = getattr(obj, f.name)
            assert value is None or len(value) <= inv._CAPS[f.name], f.name
    sk = normalize_software("N" * 5000 + " 1.2.3", "P" * 5000, "9" * 5000)
    assert len(sk.name_key) <= 160 and len(sk.product_name) <= 255
    assert sk.publisher_key is None or len(sk.publisher_key) <= 96
    assert sk.publisher_label is None or len(sk.publisher_label) <= 255


def test_input_is_cut_before_any_regex_runs():
    # Only the first 512 characters can influence the result.
    prefix = "Acme Widget Pro "
    name = prefix + "x" * (inv.MAX_INPUT_LEN - len(prefix))
    assert normalize_software(name + " 9.9.9") == normalize_software(name + " Chrome Driver KB1234567")
    assert normalize_software("A" * 100_000).product_name == "A" * 255


def test_long_hostile_inputs_finish_quickly():
    hostile = [" " * 100_000, "1." * 50_000, "(" * 100_000, "a " * 50_000 + "!", "- " * 50_000, " 1.2" * 25_000]
    start = time.perf_counter()
    for h in hostile:
        normalize_software(h, h, h)
        normalize_server(h, h, h, h)
        normalize_device(vendor=h, model=h, os_version=h, sys_descr=h, profile_name=h)
        canonical_vendor(h)
    assert time.perf_counter() - start < 1.0


# ── Server operating systems: extra rows ─────────────────────────────

def test_windows_release_comes_from_the_build_not_the_marketing_string():
    # Windows 11 reports "Windows 10" in some registry fields; the build says which it is.
    wrong_name = normalize_server("Microsoft Windows 10 Pro", "22H2", "10.0.22631.3296")
    assert (wrong_name.os_product, wrong_name.os_release) == ("Windows 11", "23H2")
    # A Server name on a client-only build keeps the marketing year instead of inventing a release.
    odd = normalize_server("Microsoft Windows Server 2019 Standard", None, "10.0.19045.1")
    assert (odd.os_product, odd.os_release) == ("Windows Server", "2019")
    # A pack build table row exists for every pinned release in the task.
    expectations = {14393: "2016", 17763: "2019", 20348: "2022", 26100: "2025"}
    for build, release in expectations.items():
        got = normalize_server("Microsoft Windows Server Datacenter", None, f"10.0.{build}.1")
        assert got.os_release == release, build
    client = {19044: "21H2", 19045: "22H2", 22000: "21H2", 22621: "22H2", 22631: "23H2", 26100: "24H2"}
    for build, release in client.items():
        got = normalize_server("Microsoft Windows 11 Pro", None, f"10.0.{build}.1")
        assert got.os_release == release, build
        assert got.os_product == ("Windows 11" if build >= 22000 else "Windows 10"), build


def test_windows_version_goes_through_versioning_canonicalisation():
    ident = normalize_server("Microsoft Windows Server 2022 Standard Evaluation", "21H2", "v10.0.20348.587 Build 20348.587")
    assert ident.os_version == "10.0.20348.587" and ident.os_build == "20348.587"
    # Marketing strings are not versions.
    assert normalize_server("Microsoft Windows Server 2022 Standard", "21H2", "21H2").os_version is None


def test_os_source_is_recorded():
    assert normalize_server("Ubuntu 22.04.4 LTS").os_source == "agent"
    assert normalize_server(None).os_source is None
    assert normalize_device(vendor="Cisco", os_version="17.9.4a").os_source == "profile"
    assert normalize_device(vendor="Cisco", os_version="", firmware_version="17.9.4a").os_source == "entity"
    assert normalize_device(vendor="Cisco").os_source is None


# ── Rule pack integrity ──────────────────────────────────────────────

def test_packs_load_without_errors():
    assert inv.PACK_ERRORS == []
    assert inv.packs_healthy()
    for name in ("software_rules.json", "vendor_aliases.json", "os_patterns.json"):
        assert (inv._DATA_DIR / name).is_file(), name
    assert inv._DATA_DIR.parts[-4:] == ("server", "app", "data", "inventory")


def test_software_pack_size_and_shape():
    rules = SOFTWARE_PACK["rules"]
    assert len(rules) >= 120, "the design targets about 300 enterprise titles; ship at least 120"
    ids = [r["id"] for r in rules + SOFTWARE_PACK["noise_rules"]]
    assert len(ids) == len(set(ids)), "rule ids must be unique"
    assert len(inv._SOFT_RULES) == len(rules) and len(inv._NOISE_RULES) == len(SOFTWARE_PACK["noise_rules"])
    for r in rules + SOFTWARE_PACK["noise_rules"]:
        assert r["examples"], f"{r['id']} needs at least one example"
    publishers = set(inv._VENDOR_BY_KEY)
    for r in rules:
        assert r.get("publisher") in publishers | {None}, r["id"]


def test_every_rule_example_fires_its_own_rule():
    for rule in SOFTWARE_PACK["rules"] + SOFTWARE_PACK["noise_rules"]:
        source = (rule.get("sources") or [None])[0]
        for example in rule["examples"]:
            got = normalize_software(example, None, source=source)
            assert got.rule_id == rule["id"], f"{rule['id']}: {example!r} fired {got.rule_id}"
            assert got.status == "normalized"
            assert got.category == rule["category"]


def test_rule_templates_extract_what_they_promise():
    for rule in SOFTWARE_PACK["rules"]:
        if not (rule.get("edition") or rule.get("cycle")):
            continue
        source = (rule.get("sources") or [None])[0]
        rx = re.compile(rule["name_re"], inv._FLAGS)
        hits = 0
        for example in rule["examples"]:
            cleaned = inv._LINUX_ARCH_RE.sub("", example) if source else inv._strip_arch(inv._clean(example))
            m = rx.search(cleaned)
            assert m, (rule["id"], example)
            for field in ("edition", "cycle"):
                if rule.get(field):
                    value = m.expand(rule[field])
                    got = getattr(normalize_software(example, None, source=source), field)
                    assert got == (value or None), (rule["id"], example, field)
                    hits += bool(value)
        assert hits, f"{rule['id']}: no example exercises its edition/cycle template"


def test_noise_rules_do_not_swallow_real_products():
    for name, category in [
        ("Microsoft ODBC Driver 17 for SQL Server", "database"),
        ("Microsoft Visual C++ 2022 X64 Additional Runtime - 14.38.33135", "runtime"),
        ("Microsoft Office Professional Plus 2016 - fr-fr", "productivity"),
        ("Dell Command | Update for Windows Universal", "driver"),
        ("Windows Subsystem for Linux Update", "os-component"),
        ("Microsoft Edge", "browser"),
    ]:
        assert normalize_software(name, "Microsoft Corporation").category == category, name
    assert normalize_software("Microsoft Office Professional Plus 2016 - fr-fr").rule_id == "office"
    assert normalize_software("Microsoft Office Shared MUI (English) 2013").rule_id == "noise-language-pack"


def test_kb_updates_key_by_kb_number():
    a = normalize_software("Security Update for Microsoft Office 2016 (KB4011281)", "Microsoft Corporation")
    b = normalize_software("Update for Microsoft Office 2016 (KB4011281) 64-Bit Edition", "Microsoft Corporation")
    c = normalize_software("Security Update for Microsoft Office 2016 (KB4011282)", "Microsoft Corporation")
    assert a.name_key == b.name_key == "kb4011281"
    assert c.name_key == "kb4011282"
    assert a.category == b.category == c.category == "update"


# ── Pack revision guard ──────────────────────────────────────────────

# RULEPACK_REV is stored with every normalised row. Anything that changes what the engine writes must bump it, so
# that stored rows are re-keyed (with change events suppressed). The pinned entry for a revision therefore covers
#   packs   the parsed rule packs (descriptions and examples are documentation and are not part of it), and
#   engine  a digest of what normalize_server / normalize_device / normalize_software / canonical_vendor and
#           versioning.canonicalize / release_cycle / compare produce over a fixed corpus: every golden fixture plus
#           the engine_probe set in the fixture file, which exists to exercise the engine itself (text clean-up,
#           slugs, edition and trailing words, the Windows and Linux logic, version schemes ...).
# A change to _clean, _slug, the Windows or Linux code or versioning.py moves the engine digest and fails the test
# even when no pack changed. When either fails, bump RULEPACK_REV in inventory_normalize.py and register the new
# values (the failing assertion prints them). Never edit the entry of a revision that has shipped; a revision that
# has not shipped yet may have its entry updated in place instead of bumping again. Revision 1 has not shipped.
PACK_FINGERPRINTS = {
    1: {
        "packs": "b6561f880c5eb2f30e10c8271ffafa56c371f74abfca102f21c23b99c3e9d023",
        "engine": "d483aab25e4c39d7f293a9546764a830e82163333886b5ae837e3853d2528c42",
    },
}


def _stable(value):
    return json.loads(json.dumps(value, sort_keys=True, ensure_ascii=True, default=str))


def _asdict(obj):
    return dataclasses.asdict(obj)


def _server_from(inp: dict):
    return normalize_server(
        inp["os_name"], inp["os_version"], inp["kernel_or_build"], inp.get("architecture"),
        hw_manufacturer=inp.get("hw_manufacturer"), hw_model=inp.get("hw_model"), hw_serial=inp.get("hw_serial"),
    )


def _engine_records() -> list:
    """Everything the engine returns for the golden inputs and the engine_probe inputs, in a fixed order."""
    inv._software_cached.cache_clear()
    inv._vendor_cached.cache_clear()
    probe = GOLDEN["engine_probe"]
    out: list = []
    add = out.append
    for case in GOLDEN["servers"]:
        add(("server", _asdict(_server_from(case["input"]))))
    for case in GOLDEN["devices"]:
        add(("device", _asdict(normalize_device(**case["input"]))))
    for raw, _key, _label in GOLDEN["vendors"]:
        add(("vendor", canonical_vendor(raw)))
    for case in GOLDEN["software"]:
        i = case["input"]
        add(("software", _asdict(normalize_software(i["name"], i["publisher"], i["version"], source=i["source"]))))
    for text in probe["text"]:
        cleaned = inv._clean(text)
        add(("text", cleaned, inv._slug(cleaned or "", 160), inv._slug(text, 24)))
    for i in probe["software"]:
        add(("probe-software", _asdict(normalize_software(i["name"], i["publisher"], i["version"], source=i["source"]))))
    win = probe["windows"]
    for name, (osv, kernel) in itertools.product(win["names"], win["builds"]):
        add(("probe-windows", _asdict(normalize_server(name, osv, kernel))))
    lin = probe["linux"]
    for name, osv in itertools.product(lin["names"], lin["versions"]):
        add(("probe-linux", _asdict(normalize_server(name, osv, None))))
    for manufacturer, model, serial in probe["hardware"]:
        add(("probe-hardware", _asdict(normalize_server("Ubuntu 22.04.4 LTS", hw_manufacturer=manufacturer,
                                                          hw_model=model, hw_serial=serial)), canonical_vendor(manufacturer)))
    dp = probe["device_profiles"]
    for profile, osv in itertools.product(dp["profiles"], dp["versions"]):
        add(("probe-device-profile", _asdict(normalize_device(profile_name=profile, os_version=osv))))
    de = probe["device_evidence"]
    for descr, oid in itertools.product(de["descr"], de["oids"]):
        add(("probe-device-evidence", _asdict(normalize_device(sys_descr=descr, sys_object_id=oid))))
    dv = probe["device_vendors"]
    for vendor, model in itertools.product(dv["vendors"], dv["models"]):
        add(("probe-device-vendor", _asdict(normalize_device(vendor=vendor, model=model,
                                                              serial_number="SERIAL-1" if model else None))))
    dt = probe["device_templates"]
    for case, extra in itertools.product(dt["cases"], dt["extras"]):
        add(("probe-device-template", _asdict(normalize_device(**{**extra, **case}))))
    add(("probe-device-huge", _asdict(normalize_device(**probe["device_huge"]))))
    for scheme, texts in probe["versions"].items():
        for text in texts:
            add(("version", scheme, text, ver.canonicalize(scheme, text), ver.release_cycle(scheme, text)))
        for a, b in itertools.product(texts[:6], texts):
            add(("compare", scheme, a, b, ver.compare(scheme, a, b)))
    for family in probe["scheme_for"]:
        add(("scheme_for", family, ver.scheme_for(family)))
    return _stable(out)


def _engine_digest() -> str:
    return hashlib.sha256(json.dumps(_engine_records(), sort_keys=True, ensure_ascii=True,
                                     separators=(",", ":")).encode("ascii")).hexdigest()


def test_pack_changes_require_a_rulepack_rev_bump():
    assert inv.RULEPACK_REV in PACK_FINGERPRINTS, (
        f"register RULEPACK_REV {inv.RULEPACK_REV}: packs {inv.pack_fingerprint()!r}, engine {_engine_digest()!r}"
    )
    assert inv.pack_fingerprint() == PACK_FINGERPRINTS[inv.RULEPACK_REV]["packs"], (
        f"the packs changed without a RULEPACK_REV bump; bump it and add "
        f"{inv.RULEPACK_REV + 1}: packs {inv.pack_fingerprint()!r}"
    )


def test_engine_changes_require_a_rulepack_rev_bump():
    assert inv.RULEPACK_REV in PACK_FINGERPRINTS
    assert _engine_digest() == PACK_FINGERPRINTS[inv.RULEPACK_REV]["engine"], (
        f"the normaliser or versioning output changed without a RULEPACK_REV bump; bump it and add "
        f"{inv.RULEPACK_REV + 1}: engine {_engine_digest()!r}"
    )


def test_the_engine_corpus_is_large_enough_to_mean_something():
    kinds = {}
    for record in _engine_records():
        kinds[record[0]] = kinds.get(record[0], 0) + 1
    assert sum(kinds.values()) > 3000
    for kind in ("server", "device", "vendor", "software", "text", "probe-software", "probe-windows", "probe-linux",
                 "probe-hardware", "probe-device-profile", "probe-device-evidence", "probe-device-vendor",
                 "probe-device-template", "version", "compare", "scheme_for"):
        assert kinds.get(kind, 0) >= 5, kind


def _swap(monkeypatch, obj, name, replacement):
    monkeypatch.setattr(obj, name, replacement)


class _NfcInsteadOfNfkc:
    """Stands in for the unicodedata module inside inventory_normalize: compatibility folding switched off."""

    def __getattr__(self, name):
        return getattr(unicodedata, name)

    @staticmethod
    def normalize(form, text):
        return unicodedata.normalize("NFC" if form == "NFKC" else form, text)


def _engine_mutations():
    """Each entry edits the engine the way a careless change would; none touches a pack."""
    real_slug, real_canon, real_windows = inv._slug, ver.canonicalize, inv._windows_identity
    return {
        "slug-truncation": lambda mp: _swap(mp, inv, "_slug", lambda t, n: real_slug(t, n - 1)),
        "slug-plus-mapping": lambda mp: _swap(mp, inv, "_SLUG_MAP", (("++", " pp "),) + inv._SLUG_MAP[1:]),
        "clean-nfc-not-nfkc": lambda mp: _swap(mp, inv, "unicodedata", _NfcInsteadOfNfkc()),
        "edition-word-removed": lambda mp: _swap(mp, inv, "_EDITION_RE", re.compile(
            inv._EDITION_RE.pattern.replace("|workstation", ""), inv._FLAGS)),
        "trailing-word-removed": lambda mp: _swap(mp, inv, "_TRAILING_WORD_RE", re.compile(
            inv._TRAILING_WORD_RE.pattern.replace("|rev\\.?", ""), inv._FLAGS)),
        "arch-token-removed": lambda mp: _swap(mp, inv, "_ARCH_RE", re.compile(
            inv._ARCH_RE.pattern.replace("|x86|win64", "|win64"), inv._FLAGS)),
        "dotted-version-narrowed": lambda mp: _swap(mp, inv, "_DOTTED_RE", re.compile(r"^\d{1,4}(?:\.\d{1,3}){0,2}$")),
        "windows-edition-logic": lambda mp: _swap(mp, inv, "_windows_identity", lambda *a: {**real_windows(*a), "os_edition": None}),
        "versioning-cycle": lambda mp: _swap(mp, ver, "release_cycle", lambda scheme, v: None),
        "versioning-canonicalize": lambda mp: _swap(mp, ver, "canonicalize", lambda scheme, raw: (
            None if real_canon(scheme, raw) is None else (real_canon(scheme, raw)[0] + ".0", None))),
        "caps-narrowed": lambda mp: _swap(mp, inv, "_CAPS", {**inv._CAPS, "os_product": 10}),
    }


@pytest.mark.parametrize("name", sorted(_engine_mutations()))
def test_engine_digest_detects_engine_only_changes(name, monkeypatch):
    """N-05: editing engine code (no pack) must move the digest, otherwise the guard would stay green."""
    baseline = _engine_digest()
    _engine_mutations()[name](monkeypatch)
    try:
        assert _engine_digest() != baseline, name
    finally:
        monkeypatch.undo()
        inv._software_cached.cache_clear()
        inv._vendor_cached.cache_clear()
    assert _engine_digest() == baseline


def test_engine_digest_does_not_depend_on_the_hash_seed():
    server_dir = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, %r); sys.path.insert(0, %r); import test_inventory_normalize as t; "
        "print(t.inv.pack_fingerprint(), t._engine_digest())" % (str(server_dir), str(Path(__file__).parent))
    )
    seen = set()
    for seed in ("0", "1", "4242"):
        env = {"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin", "ZENPLUS_API": "http://127.0.0.1:9"}
        done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
        assert done.returncode == 0, done.stderr[-2000:]
        seen.add(done.stdout.strip().splitlines()[-1])
    assert len(seen) == 1
    assert seen == {f"{inv.pack_fingerprint()} {_engine_digest()}"}


# ── Regex safety and speed ───────────────────────────────────────────

def _adversarial_inputs() -> list[str]:
    n = 10_000
    return [
        "a" * n, " " * n, "1" * n, "." * n, "1." * (n // 2), "(" * n, ")" * n, "[" * n, "x " * (n // 2), "a1" * (n // 2),
        "-" * n, "- " * (n // 2), ", " * (n // 2), " 1.2" * (n // 4), "9.9.9." * (n // 6), "KB1234567 " * (n // 10),
        "v1.2.3,build" * (n // 12), "x64 " * (n // 4), "(x64) " * (n // 6), "Microsoft Visual C++ " * (n // 21),
        "a" * (n - 1) + "!", "1." * (n // 2 - 1) + "x", "\n".join(["1.2 x"] * (n // 6)), " " * (n - 1) + "1",
        "aruba" * (n // 5), "Version " * (n // 8), "Java " * (n // 5), "\t " * (n // 2), "é" * n, " " * n,
        "Python 3." * (n // 9), "Windows Server " * (n // 15), "FortiGate " * (n // 10), "1" * (n // 2) + "." + "1" * (n // 2),
        "Microsoft Office " + "Professional Plus " * (n // 18), "Cisco " * (n // 6) + "IOS", "0" * n + "(" + "1" * 10,
        "ios/ios-xe " * (n // 11), "copyright " * (n // 10), "Professional Edition " * (n // 21), " - en-us" * (n // 8),
    ]


def test_every_regex_survives_adversarial_input():
    patterns = inv.compiled_patterns()
    assert len(patterns) > 300  # engine regexes plus every pack regex
    inputs = _adversarial_inputs()
    slowest = []
    for label, rx in patterns:
        worst = 0.0
        for text in inputs:
            best = None
            for _ in range(2):  # best of two to ride out scheduler noise
                start = time.perf_counter()
                rx.search(text)
                elapsed = time.perf_counter() - start
                best = elapsed if best is None else min(best, elapsed)
            worst = max(worst, best)
        slowest.append((worst, label))
    slowest.sort(reverse=True)
    assert slowest[0][0] < 0.05, f"slowest patterns: {slowest[:5]}"


def test_pathological_patterns_are_rejected_at_load():
    for bad in (r"(a+)+$", r"(\d+\.?)*x", r"(?:a|b+)*c", r"(?:[a-z]+\s?)+$", r"((a*)b)+", r"(?P<x>\w+\s*)*"):
        assert inv._unbounded_nested(bad), bad
        with pytest.raises(ValueError):
            inv._rx(bad, "test")
    for good in (r"^Python (\d+\.\d+)\.\d+$", r"(?:\.\d{1,4}){0,4}", r"(?:KB|Q)\d{6,7}", r"[\d.]{0,18}", r"(?:a|b){1,5}c+",
                 r"\((?:r|tm)\)", r"(?P<yr>\d{4}(?:-\d{4})?)", r"[(\[]x64[)\]]", r"\\(a+)"):
        assert not inv._unbounded_nested(good), good
    with pytest.raises(ValueError):
        inv._rx("a" * (inv._MAX_PATTERN_LEN + 1), "too-long")
    before = len(inv.compiled_patterns())
    with pytest.raises(ValueError):
        inv._rx(r"(x+)+y", "rejected")
    assert len(inv.compiled_patterns()) == before  # a rejected pattern is never registered


def test_throughput_1000_titles_under_250ms():
    random.seed(20261007)
    bases = [e for r in SOFTWARE_PACK["rules"] if not r.get("sources") for e in r["examples"]]
    bases += ["Acme Widget", "Contoso Reporter", "Fabrikam Sync Client", "Northwind Viewer", "Tailspin Toys Studio"]
    titles = []
    for i in range(1000):
        base = random.choice(bases)
        titles.append((f"{base} {i}.{random.randint(0, 20)}.{random.randint(0, 99)} (x64)", random.choice(["Acme Corp", "Microsoft Corporation", None])))
    assert len({t for t, _ in titles}) == 1000
    best = None
    for _ in range(3):
        inv._software_cached.cache_clear()  # distinct titles, cold cache
        start = time.perf_counter()
        for name, publisher in titles:
            normalize_software(name, publisher, "1.2.3")
        elapsed = time.perf_counter() - start
        best = elapsed if best is None else min(best, elapsed)
    assert best < 0.25, f"{best * 1000:.0f} ms for 1000 distinct titles"


def test_server_and_device_throughput():
    start = time.perf_counter()
    for i in range(1000):
        normalize_server("Microsoft Windows Server 2022 Standard Evaluation", "21H2", f"10.0.20348.{i} Build 20348.{i}", "x86_64")
        normalize_device(vendor="Cisco", model="C9300-48UXM", os_version="17.09.04a", profile_name="Cisco IOS/IOS-XE Switch & Router",
                         sys_object_id=".1.3.6.1.4.1.9.1.2494", sys_descr="Cisco IOS Software [Cupertino], Version 17.9.4a")
    assert time.perf_counter() - start < 1.0


# ── Strict mode (N-02) ───────────────────────────────────────────────
# The persisting callers pass strict=True so that an internal failure leaves the row dirty and retried instead of
# stamping it clean with the generic identity; everyone else keeps the total, never-raising behaviour.

STRICT_ENTRY_POINTS = [normalize_server, normalize_device, normalize_software, canonical_vendor]


@pytest.mark.parametrize("fn", STRICT_ENTRY_POINTS, ids=lambda f: f.__name__)
def test_strict_is_a_keyword_only_flag_that_defaults_to_false(fn):
    param = inspect.signature(fn).parameters["strict"]
    assert param.kind is inspect.Parameter.KEYWORD_ONLY
    assert param.default is False


def test_strict_mode_gives_the_same_results_when_nothing_fails():
    for case in GOLDEN["servers"]:
        i = case["input"]
        assert normalize_server(i["os_name"], i["os_version"], i["kernel_or_build"], i["architecture"], strict=True,
                                hw_manufacturer=i.get("hw_manufacturer"), hw_model=i.get("hw_model"),
                                hw_serial=i.get("hw_serial")) == _server_from(i)
    for case in GOLDEN["devices"]:
        assert normalize_device(**case["input"], strict=True) == normalize_device(**case["input"])
    for case in GOLDEN["software"]:
        i = case["input"]
        assert (normalize_software(i["name"], i["publisher"], i["version"], source=i["source"], strict=True)
                == normalize_software(i["name"], i["publisher"], i["version"], source=i["source"]))
    for raw, key, label in GOLDEN["vendors"]:
        assert canonical_vendor(raw, strict=True) == (key, label)


def _boom(*_a, **_k):
    raise RuntimeError("injected failure")


# (id, internal helper to break, call(strict) -> result, what the total mode returns)
STRICT_CASES = [
    ("canonical_vendor", "_vendor_cached", lambda strict: canonical_vendor("Strict Vendor Ltd", strict=strict), (None, None)),
    ("normalize_server-distro", "_distro_identity", lambda strict: normalize_server("Ubuntu 22.04.4 LTS", strict=strict), inv._UNKNOWN),
    ("normalize_server-windows", "_windows_identity",
     lambda strict: normalize_server("Microsoft Windows Server 2022", None, "10.0.20348.1", strict=strict), inv._UNKNOWN),
    ("normalize_device", "_detect_family",
     lambda strict: normalize_device(vendor="Fortinet", profile_name="Fortinet FortiGate", strict=strict), inv._UNKNOWN),
    ("normalize_software-key", "_software_cached", lambda strict: normalize_software("Strict Title One 1.0", strict=strict), inv._UNNAMED),
    ("normalize_software-residual", "_generic_residual", lambda strict: normalize_software("Strict Title Two 2.0", strict=strict), inv._UNNAMED),
]


@pytest.mark.parametrize(("name", "helper", "call", "total"), STRICT_CASES, ids=[c[0] for c in STRICT_CASES])
def test_an_internal_failure_propagates_only_in_strict_mode(name, helper, call, total, monkeypatch):
    monkeypatch.setattr(inv, helper, _boom)
    assert call(False) == total  # total by default, exactly as before
    with pytest.raises(RuntimeError, match="injected failure"):
        call(True)


def test_a_vendor_failure_inside_a_server_or_device_follows_the_flag(monkeypatch):
    monkeypatch.setattr(inv, "_vendor_cached", _boom)
    server = normalize_server("Ubuntu 22.04.4 LTS", hw_manufacturer="Dell Inc.")
    assert (server.os_family, server.os_product, server.vendor_key) == ("linux", "Ubuntu", None)  # partial, as always
    device = normalize_device(vendor="Fortinet", profile_name="Fortinet FortiGate", os_version="v7.2.5,build1517")
    assert (device.os_family, device.os_version) == ("fortios", "7.2.5")
    with pytest.raises(RuntimeError):
        normalize_server("Ubuntu 22.04.4 LTS", hw_manufacturer="Dell Inc.", strict=True)
    with pytest.raises(RuntimeError):
        normalize_device(vendor="Fortinet", strict=True)


def test_a_failure_is_never_cached_as_a_result(monkeypatch):
    real = inv._generic_residual
    monkeypatch.setattr(inv, "_generic_residual", _boom)
    with pytest.raises(RuntimeError):
        normalize_software("Strict Title Three 3.0", strict=True)
    monkeypatch.setattr(inv, "_generic_residual", real)
    assert normalize_software("Strict Title Three 3.0", strict=True).name_key == "strict-title-three"


# ── Rule packs: group arity and templates (N-02) ─────────────────────

def test_a_pattern_must_have_the_capture_groups_the_caller_reads():
    with pytest.raises(ValueError, match="capture group"):
        inv._rx(r"abc", "no-group", groups=1)
    with pytest.raises(ValueError, match="capture group"):
        inv._rx(r"(a)", "one-group", groups=2)


def test_a_template_must_refer_to_existing_groups():
    rx = re.compile(r"^FGT_(\w+)$")
    for good in (r"FortiGate-\1", r"FortiGate-\g<1>", "no references", r"\\1", r"\\2 and \1"):
        inv._check_template(rx, good)  # an escaped backslash followed by a digit is not a group reference
    for bad in (r"FortiGate-\2", r"\g<2>", r"\g<name>", r"x\12"):
        with pytest.raises(ValueError):
            inv._check_template(rx, bad)
    inv._check_template(re.compile(r"(?P<model>\d+)"), r"\g<model>")


def _pack_edit(pack, *path, value):
    def edit(name, data):
        if name != pack:
            return
        node = data
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value
    return edit


TEMPLATE_AND_ARITY_TYPOS = [
    ("hardware-template-group-too-high", inv.PACK_OS, ("hardware", "model_rules", 1, "template"), r"FortiWiFi-\2"),
    ("hardware-pattern-lost-its-group", inv.PACK_OS, ("hardware", "model_rules", 0, "pattern"), "^FGT[_-][A-Za-z0-9_-]{0,40}$"),
    ("distro-version-lost-its-group", inv.PACK_OS, ("linux", "distros", 7, "version_re"), r"(?:server\s{1,3})?\d{1,2}\b"),
    ("windows-name-lost-its-group", inv.PACK_OS, ("windows", "client_name_re"), r"\bwindows\s{1,3}(?:11|10)\b"),
    ("network-descr-lost-its-group", inv.PACK_OS, ("network", "families", "asa", "descr_version_re"), r"\bversion\s{1,3}\d{1,2}\.\d{1,2}"),
    ("network-release-lost-its-group", inv.PACK_OS, ("network", "families", "ios", "release_re"), r"^\d{1,2}\.\d{1,2}"),
    ("software-edition-template-group-too-high", inv.PACK_SOFTWARE, ("rules", 0, "edition"), r"\9"),
    ("software-key-re-lost-its-group", inv.PACK_SOFTWARE, ("noise_rules", 0, "key_re"), r"KB\d{6,7}"),
]


@pytest.fixture
def load_variant(tmp_path):
    """Import a private copy of the engine next to edited copies of the packs; returns the module."""
    counter = itertools.count()

    def load(edit=None, raw=None):
        root = tmp_path / f"variant{next(counter)}"
        (root / "services").mkdir(parents=True)
        (root / "data" / "inventory").mkdir(parents=True)
        shutil.copy(inv.__file__, root / "services" / "inventory_normalize.py")
        for name in inv.PACK_FILES:
            text = (inv._DATA_DIR / name).read_text(encoding="utf-8")
            if raw is not None and name in raw:
                text = raw[name](text)
            elif edit is not None:
                data = json.loads(text)
                edit(name, data)
                text = json.dumps(data)
            if text is not None:
                (root / "data" / "inventory" / name).write_text(text, encoding="utf-8")
        modname = f"inventory_normalize_variant_{next(counter)}"
        spec = importlib.util.spec_from_file_location(modname, root / "services" / "inventory_normalize.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[modname] = module
        logging.disable(logging.CRITICAL)
        try:
            spec.loader.exec_module(module)  # must never raise, whatever the packs hold
        finally:
            logging.disable(logging.NOTSET)
            sys.modules.pop(modname, None)
        return module

    return load


def _set_path(data, path, value):
    node = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


@pytest.mark.parametrize(("name", "pack", "path", "value"), TEMPLATE_AND_ARITY_TYPOS, ids=[t[0] for t in TEMPLATE_AND_ARITY_TYPOS])
def test_a_pack_typo_that_would_blank_identities_is_caught_at_load(name, pack, path, value, load_variant):
    mod = load_variant(edit=lambda n, d: _set_path(d, path, value) if n == pack else None)
    assert not mod.packs_healthy()
    assert not mod.packs_healthy_for(pack)
    assert all(e.split(":")[0] in inv.PACK_FILES for e in mod.PACK_ERRORS)


def test_the_unmodified_packs_load_clean_in_a_private_copy(load_variant):
    mod = load_variant()
    assert mod.PACK_ERRORS == [] and mod.packs_healthy()
    assert all(mod.packs_healthy_for(name) for name in inv.PACK_FILES)
    assert mod.pack_fingerprint() == inv.pack_fingerprint()


# ── Rule packs: loaders never crash the import (N-06) ────────────────

# Documentation keys are never read by the engine, so editing them is not a pack error.
_DOC_KEYS = {"description", "examples", "schema"}


def _container_paths(node, path=(), seen=None, out=None):
    """One path per distinct container shape (list indices collapsed to '*'); the first instance is used."""
    seen = set() if seen is None else seen
    out = [] if out is None else out
    shape = tuple("*" if isinstance(p, int) else p for p in path)
    if path and isinstance(node, (dict, list)) and shape not in seen:
        seen.add(shape)
        out.append(path)
    if isinstance(node, dict):
        # Maps keyed by data (one entry per family or enterprise) are collapsed like lists.
        items = list(node.items())
        if path and path[-1] in ("families", "enterprises", "vendor_default_family", "template_family_keys"):
            items = items[:1]
        for key, value in items:
            if key not in _DOC_KEYS:
                _container_paths(value, path + (key,), seen, out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _container_paths(value, path + (index,), seen, out)
    return out


def _wrong_type_variants(original):
    return [None, "text", 7, [] if isinstance(original, dict) else {}]


def _mutations():
    cases = []
    for pack in inv.PACK_FILES:
        data = json.loads((inv._DATA_DIR / pack).read_text(encoding="utf-8"))
        for path in _container_paths(data):
            node = data
            for key in path:
                node = node[key]
            for i, variant in enumerate(_wrong_type_variants(node)):
                if i in (1, 2) and len(path) > 1:  # keep the matrix small: text and numbers only at the top level
                    continue
                label = f"{pack.split('.')[0]}:{'.'.join(str(p) for p in path)}={variant!r}"
                cases.append(pytest.param(pack, path, variant, id=label))
    return cases


MUTATIONS = _mutations()


def test_the_wrong_type_matrix_covers_every_container_the_loaders_read():
    paths = {(m.values[0], tuple("*" if isinstance(p, int) else p for p in m.values[1])) for m in MUTATIONS}
    for needed in [
        (inv.PACK_SOFTWARE, ("noise_rules",)), (inv.PACK_SOFTWARE, ("categories",)), (inv.PACK_VENDORS, ("vendors",)),
        (inv.PACK_VENDORS, ("enterprises",)), (inv.PACK_OS, ("linux", "distros")), (inv.PACK_OS, ("windows", "builds")),
        (inv.PACK_OS, ("network", "profile_rules")), (inv.PACK_OS, ("hardware", "placeholder_values")),
        (inv.PACK_OS, ("network", "families", "fortios", "template_keys")), (inv.PACK_VENDORS, ("vendors", "*")),
        (inv.PACK_SOFTWARE, ("rules", "*", "sources")), (inv.PACK_OS, ("windows", "builds", "*", "client")),
    ]:
        assert needed in paths, needed
    assert len(MUTATIONS) >= 100


@pytest.mark.parametrize(("pack", "path", "value"), MUTATIONS)
def test_a_wrong_typed_container_degrades_the_pack_and_never_stops_the_import(pack, path, value, load_variant):
    mod = load_variant(edit=lambda n, d: _set_path(d, path, value) if n == pack else None)
    assert not mod.packs_healthy()
    assert not mod.packs_healthy_for(pack), mod.PACK_ERRORS
    assert all(e.split(":")[0] in inv.PACK_FILES for e in mod.PACK_ERRORS)
    # Still total after a degraded load.
    assert isinstance(mod.normalize_server("Ubuntu 22.04.4 LTS", hw_manufacturer="Dell Inc."), mod.Identity)
    assert isinstance(mod.normalize_device(vendor="Cisco", os_version="17.9.4a", sys_descr="x"), mod.Identity)
    assert isinstance(mod.normalize_software("Google Chrome", "Google LLC"), mod.SoftwareKey)


@pytest.mark.parametrize("pack", inv.PACK_FILES)
@pytest.mark.parametrize("shape", ["missing", "empty", "invalid-json", "list", "object", "string"])
def test_an_unusable_pack_file_degrades_and_never_stops_the_import(pack, shape, load_variant):
    texts = {"missing": None, "empty": "", "invalid-json": "{not json", "list": "[]", "object": "{}", "string": '"x"'}
    mod = load_variant(raw={pack: lambda _t, text=texts[shape]: text})
    assert not mod.packs_healthy_for(pack), mod.PACK_ERRORS
    assert isinstance(mod.normalize_server("Ubuntu 22.04.4 LTS"), mod.Identity)
    assert isinstance(mod.normalize_device(vendor="Cisco", os_version="17.9.4a"), mod.Identity)
    assert isinstance(mod.normalize_software("Google Chrome", "Google LLC"), mod.SoftwareKey)


def test_a_rule_that_is_not_an_object_is_one_error_not_a_crash(load_variant):
    def edit(name, data):
        if name == inv.PACK_SOFTWARE:
            data["rules"][3] = "not a rule"
            data["noise_rules"][0] = 5
        if name == inv.PACK_VENDORS:
            data["vendors"][2] = ["x"]
    mod = load_variant(edit=edit)
    assert not mod.packs_healthy_for(inv.PACK_SOFTWARE) and not mod.packs_healthy_for(inv.PACK_VENDORS)
    assert len([e for e in mod.PACK_ERRORS if e.startswith(inv.PACK_SOFTWARE)]) >= 2


def test_duplicate_json_keys_are_rejected_instead_of_last_one_wins(load_variant):
    def hijack_family(text):
        key = '"fortios": {"vendor": "fortinet", "product": "FortiOS"'
        assert text.count(key) == 1
        return text.replace(key, '"fortios": {"vendor": "fortinet", "product": "Hijacked", "scheme": null, "version_re": "x"}, ' + key)

    mod = load_variant(raw={inv.PACK_OS: hijack_family})
    assert not mod.packs_healthy_for(inv.PACK_OS)
    assert any("duplicate JSON key 'fortios'" in e for e in mod.PACK_ERRORS)
    assert mod.normalize_device(vendor="Fortinet", profile_name="Fortinet FortiGate", os_version="7.2.5").os_product != "Hijacked"

    def remap_enterprise(text):
        assert text.count('"enterprises": {"9": "cisco",') == 1
        return text.replace('"enterprises": {"9": "cisco",', '"enterprises": {"9": "cisco", "9": "fortinet",')

    mod = load_variant(raw={inv.PACK_VENDORS: remap_enterprise})
    assert not mod.packs_healthy_for(inv.PACK_VENDORS)
    assert mod.normalize_device(sys_object_id="1.3.6.1.4.1.9.1.1745").vendor_key != "fortinet"


def test_an_exact_alias_claimed_by_two_vendors_is_an_error(load_variant):
    def edit(name, data):
        if name == inv.PACK_VENDORS:
            for v in data["vendors"]:
                if v["key"] == "paloalto":
                    v["exact_aliases"] = ["PAN", "Zoho"]  # Zoho belongs to manageengine

    mod = load_variant(edit=edit)
    assert not mod.packs_healthy_for(inv.PACK_VENDORS)
    assert any("exact alias" in e and "Zoho" in e for e in mod.PACK_ERRORS)


def test_health_is_reported_per_pack_file(load_variant):
    def break_software(name, data):
        if name == inv.PACK_SOFTWARE:
            data["rules"][0]["category"] = "no-such-category"

    mod = load_variant(edit=break_software)
    assert not mod.packs_healthy()  # the global answer every caller uses today
    assert mod.packs_healthy_for(inv.PACK_SOFTWARE) is False
    assert mod.packs_healthy_for(inv.PACK_OS) is True and mod.packs_healthy_for(inv.PACK_VENDORS) is True
    assert mod.normalize_server("Ubuntu 22.04.4 LTS").os_product == "Ubuntu"  # identities do not depend on software rules

    def break_os(name, data):
        if name == inv.PACK_OS:
            data["network"]["profile_rules"][0]["family"] = "nope"

    mod = load_variant(edit=break_os)
    assert mod.packs_healthy_for(inv.PACK_OS) is False and mod.packs_healthy_for(inv.PACK_SOFTWARE) is True


def test_packs_healthy_for_is_true_for_the_shipped_packs_and_unknown_names():
    assert inv.packs_healthy_for(inv.PACK_SOFTWARE) and inv.packs_healthy_for(inv.PACK_OS) and inv.packs_healthy_for(inv.PACK_VENDORS)
    assert inv.packs_healthy_for("no-such-pack.json")


def test_unknown_references_between_packs_are_errors(load_variant):
    def edit(name, data):
        if name == inv.PACK_VENDORS:
            data["vendor_default_family"]["juniper"] = "no-such-family"
            data["enterprises"]["2636"] = "no-such-vendor"
        if name == inv.PACK_OS:
            data["network"]["families"]["fortios"]["vendor"] = "no-such-vendor"

    mod = load_variant(edit=edit)
    assert not mod.packs_healthy_for(inv.PACK_VENDORS) and not mod.packs_healthy_for(inv.PACK_OS)


# ── Regex safety: the load-time lint (N-07) ──────────────────────────
# The lint is best-effort: it must reject the classic exponential shapes without rejecting any shipped pattern.

MUST_REJECT = [
    # Reproduced by the reviewers on ~30 characters: 1.7 s, 5.1 s, an exponential 2x per character.
    r"^(?:\d|\d\d|\d\d\d)+x",
    r"^(?:\w+\s{0,3}){1,40}$",
    r"^(?:a|aa)+$",
    r"^oracle linux (?:\d|\d\d)+ update",
    # Bounded outer repeat around an unbounded inner quantifier.
    r"(?:a+){2,5}b",
    r"(?:\d+\.?){1,20}x",
    r"(?P<n>\w+\s*){0,30}$",
    # Alternation branches that overlap, or can be empty, under an outer repeat.
    r"(?:a|ab|b)*c",
    r"(?:ab|a)+$",
    r"(?:x|y|xy){1,50}z",
    r"(?:a|)+b",
    # A repeated group that can match nothing.
    r"(?:\s{0,3}){1,40}x",
    r"(?:a?){1,30}a{30}",
    # The shapes the text scanner already refused.
    r"(a+)+$", r"(\d+\.?)*x", r"(?:a|b+)*c", r"(?:[a-z]+\s?)+$", r"((a*)b)+", r"(?P<x>\w+\s*)*",
]

MUST_ACCEPT = [
    r"^Python (\d+\.\d+)\.\d+$", r"(?:\.\d{1,4}){0,4}", r"(?:KB|Q)\d{6,7}", r"[\d.]{0,18}", r"(?:a|b){1,5}c+", r"\((?:r|tm)\)",
    r"(?P<yr>\d{4}(?:-\d{4})?)", r"[(\[]x64[)\]]", r"\\(a+)",
    r"(?:inc|incorporated|llc)\.?\s{0,2}$",          # alternation that is not repeated
    r"(?:foo|bar|baz){1,3}x",                         # disjoint branches under a bounded repeat
    r"^(?:\d{1,3}\.){3}\d{1,3}$",                     # bounded inner quantifier under a bounded repeat
    r"(?:ab|cd)+$",                                   # branches start differently
    r"\d+(?:\.\d+){0,3}",
    r"^(?:\w+\s{1,3}){1,40}$",                        # a mandatory separator keeps the passes apart
]


@pytest.mark.parametrize("pattern", MUST_REJECT)
def test_dangerous_pattern_shapes_are_rejected_at_load(pattern):
    with pytest.raises(ValueError):
        inv._rx(pattern, "must-reject")
    assert inv._unbounded_nested(pattern) or inv._backtracking_risk(pattern)


@pytest.mark.parametrize("pattern", MUST_ACCEPT)
def test_ordinary_pattern_shapes_are_accepted(pattern):
    assert not inv._unbounded_nested(pattern)
    assert inv._backtracking_risk(pattern) is None


def test_rejected_patterns_are_not_registered():
    before = len(inv.compiled_patterns())
    for pattern in MUST_REJECT:
        with pytest.raises(ValueError):
            inv._rx(pattern, "rejected")
    assert len(inv.compiled_patterns()) == before


def test_no_shipped_pattern_is_flagged_by_the_structural_lint():
    assert inv._sre_p is not None, "the lint needs the regex parser; without it only the text scanner runs"
    assert len(inv.compiled_patterns()) > 300
    for label, rx in inv.compiled_patterns():
        assert inv._backtracking_risk(rx.pattern, rx.flags) is None, label


def test_the_module_docstring_does_not_claim_more_than_the_lint_proves():
    doc = inv.__doc__
    assert "best-effort" in doc
    assert "cannot cause catastrophic backtracking" not in doc


def test_a_dangerous_pack_pattern_degrades_the_pack_instead_of_loading(load_variant):
    def edit(name, data):
        if name == inv.PACK_OS:
            data["linux"]["distros"][0]["version_re"] = r"ubuntu\s{1,3}((?:\d|\d\d)+\.\d{2})"

    mod = load_variant(edit=edit)
    assert not mod.packs_healthy_for(inv.PACK_OS)
    assert any("repeated group" in e for e in mod.PACK_ERRORS), mod.PACK_ERRORS


# ── Slugs keep mixed-script names distinct (N-08) ────────────────────

def _legacy_slug(text: str, maxlen: int) -> str:
    """The slug before N-08: used to prove that pure ASCII names keep exactly the key they always had."""
    t = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    for a, b in inv._SLUG_MAP:
        t = t.replace(a, b)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    if not t and text.strip():
        t = "n-" + hashlib.sha1(text.strip().lower().encode("utf-8", "replace")).hexdigest()[:10]
    if len(t) > maxlen:
        t = t[: maxlen - 9].rstrip("-") + "-" + hashlib.sha1(t.encode("ascii")).hexdigest()[:8]
    return t


def _slug_corpus() -> list[str]:
    texts = {t for t in GOLDEN["engine_probe"]["text"] if isinstance(t, str)}
    for case in GOLDEN["software"]:
        texts.update(v for v in (case["input"]["name"], case["input"]["publisher"]) if v)
    for row in GOLDEN["vendors"]:
        if isinstance(row[0], str):
            texts.add(row[0])
    texts.update(i["name"] for i in GOLDEN["engine_probe"]["software"])
    for rule in SOFTWARE_PACK["rules"] + SOFTWARE_PACK["noise_rules"]:
        texts.update(rule["examples"])
        texts.add(rule.get("product", ""))
    cleaned = {inv._clean(t) for t in texts}
    cleaned.discard(None)
    return sorted(cleaned | {inv._strip_arch(c) for c in cleaned})


def test_pure_ascii_names_keep_exactly_their_old_keys():
    corpus = [t for t in _slug_corpus() if t.isascii()]
    assert len(corpus) > 300
    for text in corpus:
        for maxlen in (160, 96, 64):
            assert inv._slug(text, maxlen) == _legacy_slug(text, maxlen), (text, maxlen)


def test_diacritics_fold_without_changing_the_key():
    for text in ("Müller Reporter", "Café Tool", "Ångström Viewer", "Señor Tool"):
        folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
        assert inv._slug(text, 160) == _legacy_slug(text, 160) == inv._slug(folded, 160)


def test_every_non_ascii_name_that_lost_a_letter_gets_a_distinct_stable_suffix():
    seen = {}
    for text in _slug_corpus():
        decomposed = unicodedata.normalize("NFKD", text)
        lost = any(c.isalnum() and not c.isascii() for c in decomposed)
        key = inv._slug(text, 160)
        assert key == inv._slug(text, 160) and len(key) <= 160
        if lost:
            assert re.search(r"-[0-9a-f]{8}$", key) or key.startswith("n-"), (text, key)
            assert seen.setdefault(key, text) == text, (text, seen[key])


def test_mixed_script_titles_no_longer_collapse_onto_one_key():
    keys = {normalize_software(n, "Tencent").name_key for n in ("QQ音乐 19.2", "腾讯QQ 9.7.1", "QQ浏览器 11.0", "QQ 9.7.1")}
    assert len(keys) == 4
    ones = {normalize_software(n).name_key for n in ("1С:Предприятие 8.3 (8.3.22.1923)", "1С:Бухгалтерия 8.3")}
    assert len(ones) == 2
    assert normalize_software("Gооgle Chrоme").name_key != normalize_software("Ggle Chrme").name_key
    assert normalize_software("Straße Tool").name_key != normalize_software("Strae Tool").name_key
    # The Latin-only key is the plain one.
    assert normalize_software("QQ 9.7.1", "Tencent").name_key == "tencent-qq"


def test_the_suffix_hashes_the_nfkc_lowercased_text_so_spellings_agree():
    fullwidth = "ＱＱ音乐"  # fullwidth "QQ" followed by the same CJK
    assert inv._slug(inv._clean(fullwidth), 160) == inv._slug(inv._clean("QQ音乐"), 160)
    assert inv._slug("QQ音乐", 160) == inv._slug("qq音乐", 160)


def test_a_suffixed_key_still_respects_the_column_width():
    key = inv._slug("A" * 300 + "音乐", 160)
    assert len(key) <= 160 and re.search(r"-[0-9a-f]{8}$", key)
    key = inv._slug("音乐" * 300, 64)
    assert len(key) <= 64


# ── Invisible characters never reach stored strings (N-13) ───────────

def test_clean_removes_every_format_character_and_lone_surrogate():
    stripped, kept = [], []
    for cp in range(0x110000):
        ch = chr(cp)
        category = unicodedata.category(ch)
        hit = bool(inv._INVISIBLE_RE.fullmatch(ch))
        if category in ("Cf", "Cs"):
            stripped.append(cp)
            assert hit, f"U+{cp:04X} ({category}) survives _clean"
        elif hit:
            kept.append(cp)
    assert not kept, f"letters, digits or punctuation would be deleted: {[hex(c) for c in kept[:5]]}"
    assert len(stripped) > 2000  # the surrogates alone are 2048
    for cp in stripped:
        if 0xD800 <= cp <= 0xDFFF:
            continue
        assert inv._clean("ab" + chr(cp) + "cd") == "abcd", hex(cp)


def test_clean_turns_control_characters_into_separators():
    for cp in list(range(0, 32)) + list(range(0x7F, 0xA0)) + [0x2028, 0x2029]:
        assert inv._clean("ab" + chr(cp) + "cd") == "ab cd", hex(cp)


def test_the_specific_characters_from_the_review_are_gone():
    for cp in (0x2066, 0x2067, 0x2068, 0x2069, 0x2060, 0x00AD, 0x061C, 0x180E, 0x202E, 0x200B, 0xFEFF):
        text = "Model" + chr(cp) + " 123" + chr(cp)
        assert inv._clean(text) == "Model 123", hex(cp)
        ident = normalize_server("Other" + chr(cp) + "OS " + chr(cp) + "X", hw_model=text, hw_serial="SN" + chr(cp) + "42",
                                 hw_manufacturer="Dell " + chr(cp) + "Inc.")
        assert (ident.os_product, ident.hw_model, ident.hw_serial, ident.vendor_key) == ("OtherOS X", "Model 123", "SN42", "dell")
    # No stored string of the identity or the software key contains a format character.
    nasty = "".join(chr(c) for c in (0x2066, 0x2069, 0x061C, 0x00AD, 0x2060))
    key = normalize_software("Acme" + nasty + " Tool" + nasty + " 1.0", "Acme" + nasty + " Corp")
    for value in (key.name_key, key.product_name, key.publisher_key, key.publisher_label):
        assert value and not any(unicodedata.category(c) in ("Cf", "Cs", "Cc") for c in value), value


def test_values_that_differ_only_by_invisible_characters_are_equal():
    plain = normalize_server("Ubuntu 22.04.4 LTS", hw_manufacturer="Dell Inc.", hw_model="PowerEdge R740", hw_serial="ABC1234")
    noisy = normalize_server("Ubuntu" + chr(0x2060) + " 22.04.4 LTS", hw_manufacturer="De" + chr(0xAD) + "ll Inc.",
                             hw_model="Power" + chr(0x2067) + "Edge R740" + chr(0x2069), hw_serial="ABC" + chr(0x200B) + "1234")
    assert plain == noisy


# ── Vendor mapping (N-14) ────────────────────────────────────────────

def test_hp_and_hpe_are_mapped_consistently():
    by_name = {canonical_vendor(n)[0] for n in ("HP", "Hewlett-Packard", "Hewlett-Packard Company", "Hewlett Packard", "HPE")}
    by_oid = normalize_device(sys_object_id="1.3.6.1.4.1.11.2.3.7.11.147").vendor_key  # enterprise 11: ProCurve / Aruba-Switch
    assert by_name == {by_oid} == {"hpe"}
    assert canonical_vendor("HP Inc.") == ("hp", "HP Inc.")  # only the explicit HP Inc. is HP Inc.
    assert normalize_device(sys_object_id="1.3.6.1.4.1.232.9.2").vendor_key == "hpe"


def test_short_vendor_words_only_match_the_whole_value():
    assert canonical_vendor("Apache") == ("apache", "Apache Software Foundation")
    assert canonical_vendor("The Apache Software Foundation")[0] == "apache"
    assert canonical_vendor("Apache Friends")[0] != "apache"
    assert canonical_vendor("Extreme")[0] == "extreme" and canonical_vendor("Extreme Networks, Inc.")[0] == "extreme"
    assert canonical_vendor("Extreme Software")[0] != "extreme"


# ── Network OS evidence (N-03, N-04, N-09, N-11) ─────────────────────

def test_asa_uses_the_asa_version_scheme_so_interim_builds_compare():
    assert inv._Net.families["asa"].scheme == "cisco_asa"
    a = normalize_device(vendor="Cisco", profile_name="Cisco ASA Firewall", os_version="9.16(4)18").os_version
    b = normalize_device(vendor="Cisco", profile_name="Cisco ASA Firewall", os_version="9.16(4)57").os_version
    assert (a, b) == ("9.16(4)18", "9.16(4)57") and a != b
    assert ver.compare(ver.scheme_for("asa"), a, b) == -1


def test_a_stale_template_rank_is_the_lowest_and_it_is_labelled():
    fortigate = dict(vendor="Fortinet", profile_name="Fortinet FortiGate")
    stale = {"fgt_fw_version": "v7.2.5,build1517,230606 (GA.F)"}
    only = normalize_device(**fortigate, stale_template_values=stale)
    assert (only.os_version, only.os_build, only.os_source) == ("7.2.5", "1517", "template_stale")
    assert len(only.os_source) <= inv._CAPS["os_source"]
    for override in (dict(os_version="v7.4.1,build2463"), dict(firmware_version="7.4.1"),
                     dict(sys_descr="FortiGate-100F v7.4.1,build2463"), dict(template_values={"fgt_fw_version": "v7.4.1,build2463"})):
        got = normalize_device(**fortigate, **override, stale_template_values=stale)
        assert got.os_version == "7.4.1" and got.os_source != "template_stale", override


def test_a_stale_template_is_ignored_for_a_key_that_also_has_a_fresh_value():
    fresh = {"fgt_fw_version": "v7.4.3,build2573"}
    stale = {"fgt_fw_version": "v7.2.5,build1517"}
    assert normalize_device(vendor="Fortinet", template_values=fresh, stale_template_values=stale) == normalize_device(
        vendor="Fortinet", template_values=fresh)


def test_a_stale_template_of_another_family_is_ignored():
    got = normalize_device(vendor="Cisco", os_version="15.2(4)E8", stale_template_values={"fgt_fw_version": "v7.2.5,build1517"})
    assert (got.os_family, got.os_version, got.os_source) == ("ios", "15.2(4)E8", "profile")
    # Exactly as a fresh one: the other family's template is never a version source for this family.
    fresh = normalize_device(vendor="Cisco", os_version="15.2(4)E8", template_values={"fgt_fw_version": "v7.2.5,build1517"})
    assert fresh == got


def test_stale_template_values_may_be_malformed():
    base = dict(vendor="Fortinet", profile_name="Fortinet FortiGate", os_version="v7.2.5,build1517")
    for bad in (None, {}, {"fgt_fw_version": ""}, {"fgt_fw_version": None}, {"fgt_fw_version": 7.2}, {5: "x"}, "oops", ["a"]):
        ident = normalize_device(stale_template_values=bad, **base)  # type: ignore[arg-type]
        assert ident.os_version == "7.2.5" and ident.os_source == "profile"


def _group_dependent_fields():
    """Every pack field the engine reads with group(N) or expand(): (pack, path, kind)."""
    os_pack = json.loads((inv._DATA_DIR / inv.PACK_OS).read_text(encoding="utf-8"))
    sw_pack = json.loads((inv._DATA_DIR / inv.PACK_SOFTWARE).read_text(encoding="utf-8"))
    fields = []
    for key in ("server_year_re", "client_name_re", "version_re", "build_re"):
        fields.append((inv.PACK_OS, ("windows", key), "group"))
    for section, items in (("linux", os_pack["linux"]["distros"]), ("other_os", os_pack["other_os"]["systems"])):
        listing = "distros" if section == "linux" else "systems"
        for i, d in enumerate(items):
            for key in ("version_re", "sp_re", "edition_re"):
                if d.get(key):
                    fields.append((inv.PACK_OS, (section, listing, i, key), "group"))
    for name, fam in os_pack["network"]["families"].items():
        for key in ("descr_version_re", "release_re"):
            if fam.get(key):
                fields.append((inv.PACK_OS, ("network", "families", name, key), "group"))
    fields.append((inv.PACK_OS, ("network", "cisco_resolve", "descr_version_re"), "group"))
    for i, rule in enumerate(os_pack["hardware"]["model_rules"]):
        fields.append((inv.PACK_OS, ("hardware", "model_rules", i, "template"), "template"))
    for i, rule in enumerate(sw_pack["noise_rules"]):
        if rule.get("key_re"):
            fields.append((inv.PACK_SOFTWARE, ("noise_rules", i, "key_re"), "group"))
    for i, rule in enumerate(sw_pack["rules"]):
        for key in ("edition", "cycle"):
            if rule.get(key):
                fields.append((inv.PACK_SOFTWARE, ("rules", i, key), "template"))
    return fields


GROUP_FIELDS = _group_dependent_fields()


def test_the_group_sweep_covers_the_fields_the_reviewers_found_unguarded():
    paths = {path for _pack, path, _kind in GROUP_FIELDS}
    assert len(GROUP_FIELDS) >= 50
    for needed in (("linux", "distros", 8, "version_re"), ("hardware", "model_rules", 1, "template"), ("windows", "client_name_re")):
        assert needed in paths, needed


@pytest.mark.parametrize(("pack", "path", "kind"), GROUP_FIELDS, ids=[".".join(str(p) for p in f[1]) for f in GROUP_FIELDS])
def test_losing_a_capture_group_or_a_template_reference_is_caught_at_load(pack, path, kind, load_variant):
    def edit(name, data):
        if name != pack:
            return
        node = data
        for key in path[:-1]:
            node = node[key]
        if kind == "group":
            node[path[-1]] = re.sub(r"\((?!\?)", "(?:", node[path[-1]])  # keep the pattern, drop every capture group
        else:
            node[path[-1]] = "\\9"  # a reference to a group no pattern has

    mod = load_variant(edit=edit)
    assert not mod.packs_healthy_for(pack), mod.PACK_ERRORS
