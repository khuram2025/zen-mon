"""Unit tests for services/versioning.py (Inventory design doc 04, test T1).

Pure functions only: no database, no network, no fixtures.
"""
from __future__ import annotations

import dataclasses
import random
import time

import pytest

from app.services import versioning as ver
from app.services.versioning import (
    SCHEMES,
    canonicalize,
    compare,
    highest,
    parse,
    release_cycle,
    scheme_for,
)


# ── Required orderings (T1) ──────────────────────────────────────────
# (scheme, lower, higher): compare(lower, higher) == -1 and the reverse == 1.

ORDERINGS = [
    # Cisco IOS: numeric rebuild, maintenance, same train across majors, letters.
    ("cisco_ios", "15.2(4)E8", "15.2(4)E10"),
    ("cisco_ios", "15.2(4)E10", "15.2(5)E1"),
    ("cisco_ios", "15.2(4)E", "15.2(4)E1"),
    ("cisco_ios", "12.2(55)SE5", "15.0(2)SE11"),
    ("cisco_ios", "15.1(4)M12", "15.1(4)M12a"),
    ("cisco_ios", "15.1(4)M12a", "15.1(4)M12b"),
    ("cisco_ios", "12.4(25)", "12.4(25f)"),
    ("cisco_ios", "12.4(24)", "12.4(25f)"),
    # Cisco IOS XE: letter rebuilds, numeric (not lexical) minor.
    ("cisco_iosxe", "17.9.4", "17.9.4a"),
    ("cisco_iosxe", "17.9.4a", "17.9.4b"),
    ("cisco_iosxe", "17.9.4a", "17.10.1"),
    ("cisco_iosxe", "16.12.3", "17.3.1"),
    ("cisco_iosxe", "17.09.04", "17.9.4a"),
    ("cisco_iosxe", "03.06.04.E", "03.06.05.E"),
    # Cisco ASA: interim builds inside a maintenance release (N-04).
    ("cisco_asa", "9.16(4)", "9.16(4)18"),
    ("cisco_asa", "9.16(4)18", "9.16(4)57"),
    ("cisco_asa", "9.16(4)57", "9.16(5)"),
    ("cisco_asa", "9.8(4)45", "9.12(4)67"),
    ("cisco_asa", "9.9(2)", "9.12(1)"),
    ("cisco_asa", "9.12(4.65)", "9.12(4.67)"),
    ("cisco_asa", "9.12(4.67)", "9.12(5.1)"),
    # PAN-OS: hotfix ordering.
    ("panos", "11.1.2-h3", "11.1.2-h10"),
    ("panos", "11.1.2", "11.1.2-h1"),
    ("panos", "10.2.9-h1", "11.0.0"),
    ("panos", "9.1.16", "10.0.0"),
    # FortiOS: triple, then build.
    ("fortios", "7.2.5", "7.2.8"),
    ("fortios", "v7.2.5,build1517,230606 (GA.F)", "v7.2.8,build1639,230912 (GA.M)"),
    ("fortios", "v7.2.5,build1517,230606 (GA.F)", "7.2.8"),
    ("fortios", "v7.2.5,build1517,230606 (GA.F)", "v7.2.5,build1600,230901 (GA)"),
    ("fortios", "7.2.5", "7.10.0"),
    ("fortios", "6.4.12", "7.0.0"),
    # Junos: R, S, spin, X, F, Evolved.
    ("junos", "21.4R3-S5", "21.4R3-S5.4"),
    ("junos", "20.4R3-S9", "21.4R3"),
    ("junos", "21.4R3", "21.4R3-S1"),
    ("junos", "21.4R2", "21.4R3"),
    ("junos", "21.4R3", "21.4R3.1"),
    ("junos", "21.4R3.1", "21.4R3-S1"),
    ("junos", "15.1X49-D100", "15.1X49-D170.4"),
    ("junos", "15.1F2", "15.1F6"),
    ("junos", "21.2R1-EVO", "21.2R3-EVO"),
    # Windows builds.
    ("win_build", "10.0.20348.587", "10.0.20348.2700"),
    ("win_build", "6.3.9600.20778", "10.0.17763.6532"),
    ("win_build", "10.0.17763", "10.0.17763.1"),
    ("win_build", "10.0.20348.587 Build 20348.587", "10.0.20348.2700 Build 20348.2700"),
    # semverish.
    ("semverish", "1.2.9", "1.2.10"),
    ("semverish", "4", "4a"),
    ("semverish", "24.09", "25.01"),
    ("semverish", "151.0.7922.110", "152.0.8001.12"),
    ("semverish", "0.9", "1"),
    ("semverish", "1.1.1j", "1.1.1k"),
    ("semverish", "1.1.1k", "1.1.2"),
    ("semverish", "4a", "4b"),
    ("semverish", "1.0-rc1", "1.0"),
    ("semverish", "1.0-alpha", "1.0-beta"),
    ("semverish", "1.0-beta", "1.0-rc1"),
    ("semverish", "1.0-dev", "1.0-alpha"),
    ("semverish", "1.0rc2", "1.0rc10"),
    ("semverish", "1.0a1", "1.0b1"),
    ("semverish", "2.0.0-beta.2", "2.0.0-beta.11"),
    ("semverish", "1.0.0-rc1", "1.0.1"),
    ("semverish", "1.0a", "1.0.1"),
    ("semverish", "17.1.1.3", "17.1.2"),
    ("semverish", "10.13.1000", "10.13.1001"),
    # dpkg.
    ("dpkg", "1.9-1", "1:1.2-3ubuntu1"),
    ("dpkg", "1.0~rc1", "1.0"),
    ("dpkg", "1.0~~", "1.0~"),
    ("dpkg", "1.0~", "1.0"),
    ("dpkg", "1.0~rc1", "1.0~rc2"),
    ("dpkg", "2.0-1", "2.0-1ubuntu0.1"),
    ("dpkg", "1.0", "1.0a"),
    ("dpkg", "1.0", "1.0+b1"),
    ("dpkg", "1.0a", "1.0+b1"),
    ("dpkg", "1.2-9", "1.2-10"),
    ("dpkg", "1.2.9", "1.2.10"),
    ("dpkg", "1.0-1", "1.0-1+deb12u1"),
    ("dpkg", "3.0.2-0ubuntu1.9", "3.0.2-0ubuntu1.10"),
    # rpm.
    ("rpm", "1.0-1.el9", "1.0-2.el9"),
    ("rpm", "2.0", "1:0.9"),
    ("rpm", "1.0", "1.0-1"),
    ("rpm", "1.0~rc1", "1.0"),
    ("rpm", "1.0", "1.0^git1"),
    ("rpm", "1.0^git1", "1.0.1"),
    ("rpm", "5.14.0-427.el9", "5.14.0-503.el9"),
    ("rpm", "1.0-1.el9", "1.0-1.el9_4"),
    ("rpm", "1.0a", "1.0.1"),
]


@pytest.mark.parametrize(("scheme", "low", "high"), ORDERINGS)
def test_pinned_orderings(scheme, low, high):
    assert compare(scheme, low, high) == -1
    assert compare(scheme, high, low) == 1
    assert compare(scheme, low, low) == 0
    assert compare(scheme, high, high) == 0


def test_orderings_cover_every_scheme():
    assert {s for s, _, _ in ORDERINGS} == set(SCHEMES)


# Different spellings of the same version compare equal.
EQUALITIES = [
    ("semverish", "1.0", "1.0.0"),
    ("semverish", "1", "1.0.0.0"),
    ("semverish", "0", "0.0"),
    ("semverish", "v1.2.3", "1.2.3"),
    ("semverish", "1.0.0+build5", "1.0.0"),
    ("semverish", "1.0.0+build5", "1.0.0+build9"),
    ("semverish", "1.0-RC1", "1.0-rc1"),
    ("semverish", "1.0rc1", "1.0-rc.1"),
    ("semverish", "1.0.0-rc1", "1-rc1"),
    ("semverish", "24.09", "24.9"),
    ("semverish", "  1.2.3  ", "1.2.3"),
    ("win_build", "10.0", "10.0.0.0"),
    ("win_build", "10.0.20348.587 Build 20348.587", "10.0.20348.587"),
    ("win_build", "10.0.020348.0587", "10.0.20348.587"),
    ("cisco_iosxe", "17.09.04a", "17.9.4a"),
    ("cisco_iosxe", "IOS XE 17.9.4a", "17.9.4a"),
    ("cisco_iosxe", "Cisco IOS XE Software, Version 17.09.04a", "17.9.4a"),
    ("cisco_iosxe", "Cupertino-17.9.4a", "17.9.4a"),
    ("cisco_iosxe", "17.9.4a, RELEASE SOFTWARE (fc3)", "17.9.4a"),
    ("cisco_asa", "09.16(04)57", "9.16(4)57"),
    ("cisco_asa", "Version 9.16(4)57", "9.16(4)57"),
    ("cisco_asa", "9.16(4)", "9.16(4)0"),
    ("cisco_ios", "15.02(04)E08", "15.2(4)E8"),
    ("cisco_ios", "Version 15.2(7)E8, RELEASE SOFTWARE (fc1)", "15.2(7)E8"),
    ("cisco_ios", "IOS 15.2(7)E8", "15.2(7)E8"),
    ("panos", "11.1.2-h0", "11.1.2"),
    ("panos", "PAN-OS 11.1.2-h3", "11.1.2-H3"),
    ("fortios", "v7.2.5,build1517,230606 (GA.F)", "7.2.5,build1517"),
    ("fortios", "7.2.5,build0157", "v7.2.5-build157"),
    ("fortios", "FortiOS v7.2.5", "7.2.5"),
    ("junos", "JUNOS 21.4R3-S5.4", "21.4R3-S5.4"),
    ("junos", "21.4R3-S0", "21.4R3"),
    ("dpkg", "0:1.0-1", "1.0-1"),
    ("dpkg", "1.0", "1.0-0"),
    ("dpkg", "1.007", "1.7"),
    ("rpm", "0:1.0-1", "1.0-1"),
    ("rpm", "1.0", "1_0"),
    ("rpm", "1.0001", "1.1"),
]


@pytest.mark.parametrize(("scheme", "a", "b"), EQUALITIES)
def test_equal_spellings(scheme, a, b):
    assert compare(scheme, a, b) == 0
    assert compare(scheme, b, a) == 0


# ── Refusals: None means not comparable, never a guess ───────────────

NOT_COMPARABLE = [
    # Cisco IOS refuses cross-train comparison.
    ("cisco_ios", "15.2(4)E8", "15.2(4)M8"),
    ("cisco_ios", "15.2(4)E8", "15.2(4)EX8"),
    ("cisco_ios", "15.2(4)E8", "15.2(4)SE8"),
    ("cisco_ios", "12.2(33)SRD1", "12.2(33)SRE1"),
    ("cisco_ios", "12.4(25f)", "12.4(24)T8"),
    ("cisco_ios", "15.2(4)E8", "15.3(1)T"),
    # Legacy IOS XE trains.
    ("cisco_iosxe", "3.7.4S", "3.7.4E"),
    ("cisco_iosxe", "03.16.05.S", "16.3.1"),
    ("cisco_iosxe", "03.06.05.E", "17.9.4a"),
    # Cisco ASA: the dotted interim form and the trailing form are not known to be the same build.
    ("cisco_asa", "9.12(4.65)", "9.12(4)65"),
    ("cisco_asa", "9.16(4.57)", "9.16(4)58"),
    ("cisco_asa", "17.9.4a", "9.16(4)57"),
    # Junos lines that do not form one sequence.
    ("junos", "21.4R3", "21.4F3"),
    ("junos", "21.4R3", "21.4R3-EVO"),
    ("junos", "21.2R1-EVO", "21.2R1"),
    ("junos", "15.1X49-D170", "16.1R4"),
    ("junos", "15.1X49-D170", "15.1X53-D10"),
    ("junos", "15.1X49-D170", "17.3X49-D10"),
    ("junos", "15.1F6", "16.1F1"),
    ("junos", "21.4B1", "21.4F1"),
    # FortiOS: same triple, only one side states a build.
    ("fortios", "7.2.5", "v7.2.5,build1517,230606 (GA.F)"),
    ("fortios", "v7.2.5,build1517", "7.2.5"),
    # Strings from another scheme are not versions of this one.
    ("cisco_ios", "17.9.4a", "15.2(4)E8"),
    ("cisco_iosxe", "15.2(4)E8", "17.9.4"),
    ("panos", "7.2.5,build1517", "11.1.2"),
    ("junos", "11.1.2-h3", "21.4R3"),
    # semverish refuses tails that have no portable ordering.
    ("semverish", "1.2.3-1", "1.2.3"),
    ("semverish", "9.6p1", "9.6"),
    ("semverish", "1.2.3-SNAPSHOT", "1.2.3"),
    ("semverish", "10.2(3)", "10.2(4)"),
    ("semverish", "1.2.3 (build 5)", "1.2.3"),
    ("semverish", "1.0-alpha.beta", "1.0"),
    ("semverish", "abc", "abd"),
    # Marketing strings are not Windows build numbers.
    ("win_build", "21H2", "22H2"),
    ("win_build", "10.0.20348.587.1", "10.0.20348.587"),
]


@pytest.mark.parametrize(("scheme", "a", "b"), NOT_COMPARABLE)
def test_not_comparable_is_none(scheme, a, b):
    assert compare(scheme, a, b) is None
    assert compare(scheme, b, a) is None


def test_none_is_not_zero_or_falsey_ordering():
    # Callers must test "is None"; the sentinel must not be a valid ordering.
    assert compare("cisco_ios", "15.2(4)E8", "15.2(4)M8") not in (-1, 0, 1)


# ── What each parser rejects ─────────────────────────────────────────

REJECTED = {
    "win_build": ["abc", "10.0.20348.587.1", "10.0.x", "10.0 Build", "21H2", "1.2.3-rc1", "v", "10.0.20348.587 Build"],
    "semverish": ["abc", "v", "1.", ".5", "1..2", "1.2.3-", "-1.2", "1.2.3+", "1.2.3+a+b",
                  "1.2.3 beta 4", "1.2.3-1", "9.6p1", "1.2.3-SNAPSHOT", "1.0-a", "1.0c1"],
    "fortios": ["7.2", "7", "7.2.5.1", "v7.2.x", "7.2.5,build", "7.2.5 (GA", "FortiGate-60F"],
    "panos": ["11.1", "11.1.2-beta", "11.1.2-h", "11.1.2.xfr", "11.1.2-hx", "11.1.2-h3.xfr"],
    "cisco_asa": ["9.16", "9.16(4", "9.16()", "9.16(4)5x", "9.12(4.65)7", "9.16(4)57)", "15.2(4)E8", "x9.16(4)",
                  "9.16.4", "9.16(4)-57", "ASA 9.16(4)"],
    "cisco_ios": ["17.9.4a", "15.2(4", "15.2(4)e8", "15.2E8", "15.2(4)E8abc", "12.4(25)3",
                  "IOS XE 17.9.4a", "15.2()E8"],
    "cisco_iosxe": ["15.2(4)E8", "17.9", "17.9.4.1", "17.9.4abc", "17.9.x", "IOS 17.9.4a"],
    "junos": ["21.4", "21.4R", "21.4I20220101", "21.4r3", "21.4R3-D5", "15.1X49-S5",
              "21.4R3-S", "21.4R3-S5-EVOx"],
    "dpkg": ["abc", "1.2:3", "x:1.2", "-1", "1.2-", "a1.2", ":1.2", "1.2_3", "1:", "1.0 rc1"],
    "rpm": ["abc", "x:1.2", "1.2-", "a1.2", ":1.2", "1.2 3", "1:", "1.2-3-"],
}


@pytest.mark.parametrize("scheme", SCHEMES)
def test_rejected_inputs(scheme):
    for bad in REJECTED[scheme]:
        assert parse(scheme, bad) is None, bad
        assert canonicalize(scheme, bad) is None, bad
        assert release_cycle(scheme, bad) is None, bad
        assert compare(scheme, bad, bad) is None, bad


def test_rejected_table_covers_every_scheme():
    assert set(REJECTED) == set(SCHEMES)


# ── Malformed input never raises ─────────────────────────────────────

MALFORMED = [
    "",
    " ",
    "\t\n",
    None,
    123,
    1.5,
    True,
    b"1.2.3",
    ["1.2.3"],
    {"v": "1.2.3"},
    object(),
    "abc",
    "٣.٤.٥",  # Arabic-Indic digits: int() would accept them
    "１.２.３",  # full-width digits
    "1.2.²",  # superscript two: str.isdigit() is True, int() raises
    "1.2.3\x00",
    "1.2.3\n4.5.6",
    "\U0001f642",
    "‮1.2.3",
    "a" * 10_000,
    "1" * 10_000,
    "1." * 5_000,
    "1" * 257,
    "(" * 300,
    "-" * 300,
    "1:" * 200,
    "~^" * 200,
    "1.2.3-rc" + "9" * 5_000,
]


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("bad", MALFORMED, ids=lambda b: repr(b)[:24])
def test_malformed_input_never_raises(scheme, bad):
    assert parse(scheme, bad) is None
    assert compare(scheme, bad, "1.2.3") is None
    assert compare(scheme, "1.2.3", bad) is None
    assert compare(scheme, bad, bad) is None
    assert canonicalize(scheme, bad) is None
    assert release_cycle(scheme, bad) is None
    assert highest(scheme, [bad]) is None
    assert highest(scheme, ["1.2.3", bad]) is None


@pytest.mark.parametrize("scheme", SCHEMES)
def test_empty_and_odd_collections_for_highest(scheme):
    assert highest(scheme, []) is None
    assert highest(scheme, ()) is None
    assert highest(scheme, iter(())) is None
    assert highest(scheme, None) is None
    assert highest(scheme, 5) is None
    assert highest(scheme, "1.2.3") is None  # a bare string is not a list
    assert highest(scheme, b"1.2.3") is None


def test_length_limit_boundary():
    # 256 characters is the longest accepted input; 257 is refused outright.
    at_limit = "1" * 256
    assert compare("semverish", at_limit, at_limit) == 0
    assert compare("semverish", at_limit, "1" * 257) is None
    padded = "  " + at_limit + "  "  # whitespace is stripped before the check
    assert compare("semverish", padded, at_limit) == 0
    assert parse("semverish", "1" * 257) is None


def test_huge_numbers_do_not_overflow():
    big = "9" * 200
    assert compare("semverish", big + ".1", big + ".2") == -1
    assert compare("win_build", big + ".1", big + ".2") == -1
    assert compare("dpkg", big, big + "0") == -1
    assert compare("rpm", big, big + "0") == -1


# ── Canonical form, build and lifecycle cycle ────────────────────────

CANONICAL = [
    # The examples pinned in the task and in doc 02 section 4.2.
    ("fortios", "v7.2.5,build1517,230606 (GA.F)", ("7.2.5", "1517")),
    ("panos", "11.1.10-h25", ("11.1.10-h25", None)),
    ("cisco_iosxe", "17.09.04a", ("17.9.4a", None)),
    ("cisco_ios", "15.2(7)E8", ("15.2(7)E8", None)),
    ("junos", "21.4R3-S5.4", ("21.4R3-S5.4", None)),
    ("semverish", "17.1.1.3", ("17.1.1.3", None)),  # F5 TMOS
    ("semverish", "10.13.1000", ("10.13.1000", None)),  # Aruba AOS-CX
    ("win_build", "10.0.20348.587 Build 20348.587", ("10.0.20348.587", "20348.587")),
    ("win_build", "10.0.17763.6532", ("10.0.17763.6532", None)),
    ("win_build", "10.0.22631.4317", ("10.0.22631.4317", None)),
    # More forms.
    ("fortios", "v7.4.2-build2222 231220 (GA.M)", ("7.4.2", "2222")),
    ("fortios", "FortiOS v6.4.12", ("6.4.12", None)),
    ("fortios", "v7.0.1,build0157,210716 (GA)", ("7.0.1", "157")),
    ("panos", "PAN-OS 10.2.9-h1", ("10.2.9-h1", None)),
    ("panos", "11.1.2-h0", ("11.1.2", None)),
    ("panos", "11.0.0", ("11.0.0", None)),
    ("cisco_iosxe", "IOS XE 17.9.4a", ("17.9.4a", None)),
    ("cisco_iosxe", "Cisco IOS XE Software, Version 17.09.04a", ("17.9.4a", None)),
    ("cisco_iosxe", "Cupertino-17.9.4a", ("17.9.4a", None)),
    ("cisco_iosxe", "03.06.05.E", ("3.6.5.E", None)),
    ("cisco_asa", "9.16(4)57", ("9.16(4)57", None)),
    ("cisco_asa", "9.16(4)", ("9.16(4)", None)),
    ("cisco_asa", "Version 09.08(04)45", ("9.8(4)45", None)),
    ("cisco_asa", "9.12(4.65)", ("9.12(4.65)", None)),
    ("cisco_ios", "15.02(07)E08", ("15.2(7)E8", None)),
    ("cisco_ios", "Version 12.4(25f), RELEASE SOFTWARE (fc1)", ("12.4(25f)", None)),
    ("cisco_ios", "15.1(4)M12a", ("15.1(4)M12a", None)),
    ("cisco_ios", "12.2(33)SRE9a", ("12.2(33)SRE9a", None)),
    ("junos", "JUNOS 15.1X49-D170.4", ("15.1X49-D170.4", None)),
    ("junos", "21.2R1-S1-EVO", ("21.2R1-S1-EVO", None)),
    ("junos", "21.4R3-S0", ("21.4R3", None)),
    ("junos", "20.4R3-S9", ("20.4R3-S9", None)),
    ("semverish", "v1.2.3+45", ("1.2.3", "45")),
    ("semverish", "25.01", ("25.01", None)),  # leading zeros are kept as written
    ("semverish", "1.0.0-RC1", ("1.0.0-rc1", None)),
    ("semverish", "1.1.1K", ("1.1.1k", None)),
    ("dpkg", "0:1.2-3", ("1.2-3", None)),
    ("dpkg", "1:1.2-3ubuntu1", ("1:1.2-3ubuntu1", None)),
    ("rpm", "0:9.4-5.el9", ("9.4-5.el9", None)),
    ("rpm", "1:0.9", ("1:0.9", None)),
]


@pytest.mark.parametrize(("scheme", "raw", "expected"), CANONICAL)
def test_canonicalize(scheme, raw, expected):
    assert canonicalize(scheme, raw) == expected


def _with_build(scheme, version, build):
    """Version text carrying its build again, for round-trip comparisons."""
    if scheme == "fortios" and build:
        return f"{version},build{build}"
    return version


@pytest.mark.parametrize(("scheme", "raw", "expected"), CANONICAL)
def test_canonical_form_is_a_fixed_point_and_compares_equal(scheme, raw, expected):
    version, build = expected
    assert canonicalize(scheme, version) == (version, None)
    assert compare(scheme, raw, _with_build(scheme, version, build)) == 0


def test_fortios_version_without_build_is_not_comparable_to_one_with_build():
    # The normaliser stores version and build separately: "7.2.5" and "1517".
    # Comparing the stored version with a full string is refused, not guessed.
    assert compare("fortios", "v7.2.5,build1517,230606 (GA.F)", "7.2.5") is None


CYCLES = [
    ("fortios", "7.2.5", "7.2"),
    ("fortios", "v7.2.5,build1517,230606 (GA.F)", "7.2"),
    ("cisco_iosxe", "IOS XE 17.9.4a", "17.9"),
    ("cisco_iosxe", "17.09.04a", "17.9"),
    ("cisco_ios", "15.2(7)E8", "15.2"),
    ("cisco_ios", "12.2(55)SE12", "12.2"),
    ("cisco_asa", "9.16(4)57", "9.16"),
    ("cisco_asa", "9.12(4.65)", "9.12"),
    ("panos", "11.1.10-h25", "11.1"),
    ("junos", "21.4R3-S5.4", "21.4"),
    ("junos", "15.1X49-D170.4", "15.1X49"),
    ("junos", "21.2R1-S1-EVO", "21.2"),
    ("semverish", "3.12.10", "3.12"),
    ("semverish", "22.04.4", "22.04"),
    ("semverish", "17.1.1.3", "17.1"),
    ("semverish", "10.13.1000", "10.13"),
    ("semverish", "4", "4"),
    ("semverish", "4a", "4"),
    ("semverish", "3.13.0rc1", "3.13"),
    ("semverish", "v8.6.4+abc", "8.6"),
    ("win_build", "10.0.20348.587", "10.0.20348"),
    ("win_build", "10.0.20348.587 Build 20348.587", "10.0.20348"),
    ("win_build", "10.0.20348", "10.0.20348"),
    ("win_build", "10.0", None),
    ("dpkg", "1:1.2-3ubuntu1", "1.2"),
    ("dpkg", "3.0.2-0ubuntu1.10", "3.0"),
    ("rpm", "9.4.1-5.el9", "9.4"),
    ("rpm", "1:0.9", "0.9"),
]


@pytest.mark.parametrize(("scheme", "raw", "expected"), CYCLES)
def test_release_cycle(scheme, raw, expected):
    assert release_cycle(scheme, raw) == expected


def test_doc_02_section_4_2_network_rows():
    # (scheme, raw sysDescr-derived value, version, build, release)
    rows = [
        ("fortios", "v7.2.5,build1517,230606 (GA.F)", "7.2.5", "1517", "7.2"),
        ("panos", "11.1.10-h25", "11.1.10-h25", None, "11.1"),
        ("cisco_iosxe", "17.09.04a", "17.9.4a", None, "17.9"),
        ("cisco_ios", "15.2(7)E8", "15.2(7)E8", None, "15.2"),
        ("cisco_asa", "9.16(4)57", "9.16(4)57", None, "9.16"),
        ("junos", "21.4R3-S5.4", "21.4R3-S5.4", None, "21.4"),
        ("semverish", "17.1.1.3", "17.1.1.3", None, "17.1"),
        ("semverish", "10.13.1000", "10.13.1000", None, "10.13"),
    ]
    for scheme, raw, version, build, release in rows:
        assert canonicalize(scheme, raw) == (version, build)
        assert release_cycle(scheme, version) == release


# ── scheme_for ───────────────────────────────────────────────────────

SCHEME_FOR = {
    "windows": "win_build",
    "Windows": "win_build",
    "windows server": "win_build",
    "Microsoft Windows Server 2022": "win_build",
    "fortios": "fortios",
    "FortiOS": "fortios",
    "panos": "panos",
    "PAN-OS": "panos",
    "ios": "cisco_ios",
    "IOS": "cisco_ios",
    "ios-xe": "cisco_iosxe",
    "ios_xe": "cisco_iosxe",
    "IOS XE": "cisco_iosxe",
    "iosxe": "cisco_iosxe",
    "asa": "cisco_asa",
    "ASA": "cisco_asa",
    "junos": "junos",
    "Junos OS": "junos",
    "junos-evolved": "junos",
    "ubuntu": "dpkg",
    "Debian": "dpkg",
    "dpkg": "dpkg",
    "rhel": "rpm",
    "Red Hat Enterprise Linux": "rpm",
    "rocky": "rpm",
    "rpm": "rpm",
    # Product names as the normaliser writes them (N-17): these used to fall through to semverish.
    "Kali Linux": "dpkg",
    "Linux Mint": "dpkg",
    "openSUSE Leap": "rpm",
    "openSUSE Tumbleweed": "rpm",
    "SLES": "rpm",
    "SLED": "rpm",
    "CentOS Stream": "rpm",
    "Oracle Linux": "rpm",
    "Amazon Linux": "rpm",
    "Rocky Linux": "rpm",
    "AlmaLinux": "rpm",
    # Safe default.
    "tmos": "semverish",
    "aos-cx": "semverish",
    "arubaos": "semverish",
    "nx-os": "semverish",
    "routeros": "semverish",
    "dellos10": "semverish",
    "linux": "semverish",
    "other": "semverish",
    "unknown": "semverish",
    "": "semverish",
    "   ": "semverish",
    "something-new": "semverish",
    None: "semverish",
    42: "semverish",
}


@pytest.mark.parametrize(("family", "expected"), list(SCHEME_FOR.items()), ids=repr)
def test_scheme_for(family, expected):
    assert scheme_for(family) == expected


@pytest.mark.parametrize("scheme", SCHEMES)
def test_scheme_names_map_to_themselves(scheme):
    assert scheme_for(scheme) == scheme


def test_scheme_for_always_returns_a_known_scheme():
    for value in [*SCHEME_FOR, "windows10", "ciscoios", "x" * 1000, "é"]:
        assert scheme_for(value) in SCHEMES


# ── highest ──────────────────────────────────────────────────────────

def test_highest_basic():
    assert highest("semverish", ["1.2.9", "1.2.10", "1.2.3"]) == "1.2.10"
    assert highest("semverish", ["1.2.10", "1.2.9"]) == "1.2.10"
    assert highest("semverish", ["4", "4a", "3"]) == "4a"
    assert highest("semverish", ["7.0"]) == "7.0"
    assert highest("dpkg", ["1.0~rc1", "1.0", "1:0.1", "0.9"]) == "1:0.1"
    assert highest("rpm", ["1.0-1.el9", "1.0-2.el9"]) == "1.0-2.el9"
    assert highest("cisco_ios", ["15.2(4)E8", "15.2(4)E10", "15.2(4)E9"]) == "15.2(4)E10"
    assert highest("junos", ["21.4R3-S5", "21.4R3-S5.4", "20.4R3-S9"]) == "21.4R3-S5.4"
    assert highest("panos", ["11.1.2-h3", "11.1.2-h10", "11.1.2"]) == "11.1.2-h10"


def test_highest_returns_the_element_as_given():
    assert highest("semverish", ["v1.2.3", " 1.2.10 ", "1.2.9"]) == " 1.2.10 "
    assert highest("win_build", ["10.0.20348.587 Build 20348.587", "10.0.17763.1"]) == (
        "10.0.20348.587 Build 20348.587"
    )


def test_highest_tie_returns_first():
    assert highest("semverish", ["1.0", "1.0.0", "0.9"]) == "1.0"
    assert highest("semverish", ["1.0.0", "1.0", "0.9"]) == "1.0.0"
    assert highest("semverish", ["0.9", "1.0.0", "1.0"]) == "1.0.0"


def test_highest_accepts_any_iterable():
    assert highest("semverish", (v for v in ["1.0", "2.0"])) == "2.0"
    assert highest("semverish", ("1.0", "2.0")) == "2.0"
    assert highest("semverish", {"1.0": 1, "3.0": 2}) == "3.0"
    assert highest("semverish", iter(["1.0", "2.0"])) == "2.0"


def test_highest_refuses_when_not_mutually_comparable():
    assert highest("cisco_ios", ["15.2(4)E8", "15.2(4)M8"]) is None
    assert highest("cisco_ios", ["15.2(4)E8", "15.2(4)E10", "15.2(4)M8"]) is None
    assert highest("junos", ["21.4R3", "21.4R3-EVO"]) is None
    assert highest("semverish", ["1.2.3", "1.2.3-1"]) is None
    assert highest("semverish", ["1.2.3", "not a version"]) is None
    assert highest("semverish", ["1.2.3", None]) is None


def test_highest_is_independent_of_input_order_when_one_element_dominates():
    # 7.2.5 and 7.2.5+build cannot be ordered against each other, but 7.2.8
    # is provably greater than both, so it is the answer in every order.
    a, b, c = "7.2.5", "v7.2.5,build1517", "7.2.8"
    for order in ([a, b, c], [c, a, b], [a, c, b], [b, a, c], [b, c, a], [c, b, a]):
        assert highest("fortios", order) == c
    assert highest("fortios", [a, b]) is None
    assert highest("fortios", [b, a]) is None


# ── Parsed values are immutable and hashable ─────────────────────────

PARSED_SAMPLES = {
    "win_build": "10.0.20348.587 Build 20348.587",
    "semverish": "1.0.0-rc1+b5",
    "fortios": "v7.2.5,build1517,230606 (GA.F)",
    "panos": "11.1.10-h25",
    "cisco_ios": "15.2(7)E8",
    "cisco_iosxe": "17.09.04a",
    "cisco_asa": "9.16(4)57",
    "junos": "21.4R3-S5.4",
    "dpkg": "1:1.2-3ubuntu1",
    "rpm": "1:0.9-1.el9",
}


@pytest.mark.parametrize("scheme", SCHEMES)
def test_parsed_values_are_frozen_and_hashable(scheme):
    p = parse(scheme, PARSED_SAMPLES[scheme])
    assert dataclasses.is_dataclass(p)
    field_name = dataclasses.fields(p)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(p, field_name, 99)
    assert hash(p) == hash(parse(scheme, PARSED_SAMPLES[scheme]))
    assert {p, parse(scheme, PARSED_SAMPLES[scheme])} == {p}
    assert isinstance(p.text, str)
    assert p.build is None or isinstance(p.build, str)


def test_parse_helpers_return_structured_values():
    p = ver.parse_fortios("v7.2.5,build1517,230606 (GA.F)")
    assert (p.major, p.minor, p.patch, p.build_no, p.tag) == (7, 2, 5, 1517, "GA.F")
    p = ver.parse_panos("11.1.10-h25")
    assert (p.major, p.minor, p.patch, p.hotfix) == (11, 1, 10, 25)
    p = ver.parse_cisco_ios("12.2(33)SRE9a")
    assert (p.major, p.minor, p.maint, p.train, p.rebuild, p.rebuild_letter) == (12, 2, 33, "SRE", 9, "a")
    p = ver.parse_cisco_iosxe("17.09.04a")
    assert (p.major, p.minor, p.maint, p.letter, p.train) == (17, 9, 4, "a", "")
    p = ver.parse_cisco_asa("9.16(4)57")
    assert (p.major, p.minor, p.maint, p.interim, p.dotted) == (9, 16, 4, 57, False)
    p = ver.parse_cisco_asa("9.12(4.65)")
    assert (p.maint, p.interim, p.dotted) == (4, 65, True)
    p = ver.parse_junos("21.4R3-S5.4")
    assert (p.major, p.minor, p.rtype, p.rel, p.service, p.spin, p.evo) == (21, 4, "R", 3, 5, 4, False)
    p = ver.parse_junos("15.1X49-D170.4")
    assert (p.rtype, p.rel, p.service, p.spin) == ("X", 49, 170, 4)
    p = ver.parse_dpkg("1:1.2-3ubuntu1")
    assert (p.epoch, p.upstream, p.revision) == (1, "1.2", "3ubuntu1")
    p = ver.parse_rpm("2:1.0-1.el9")
    assert (p.epoch, p.version, p.release) == (2, "1.0", "1.el9")
    p = ver.parse_win_build("10.0.20348.587 Build 20348.587")
    assert (p.parts, p.build) == ((10, 0, 20348, 587), "20348.587")


def test_equal_versions_hash_equal():
    assert ver.parse_semverish("1.0") == ver.parse_semverish("1.0.0")
    assert len({ver.parse_semverish(x) for x in ("1.0", "1.0.0", "v1", "1.0.0+b1")}) == 1
    assert ver.parse_cisco_iosxe("17.09.04a") == ver.parse_cisco_iosxe("17.9.4a")
    assert ver.parse_cisco_asa("09.16(04)57") == ver.parse_cisco_asa("9.16(4)57")
    assert ver.parse_semverish("1.0") != ver.parse_semverish("1.0.1")


def test_unknown_scheme_is_a_programming_error():
    for bad in ("nope", "", None, 5, ["semverish"]):
        with pytest.raises(ValueError):
            compare(bad, "1", "2")
        with pytest.raises(ValueError):
            parse(bad, "1")
        with pytest.raises(ValueError):
            canonicalize(bad, "1")
        with pytest.raises(ValueError):
            release_cycle(bad, "1")
        with pytest.raises(ValueError):
            highest(bad, ["1"])


def test_schemes_constant():
    assert set(SCHEMES) == {
        "win_build", "semverish", "fortios", "panos", "cisco_ios",
        "cisco_iosxe", "cisco_asa", "junos", "dpkg", "rpm",
    }


# ── Reference vectors for the Linux algorithms ───────────────────────
# dpkg and rpm are ports of C code with many edge cases, so these tables
# were cross-checked against the real dpkg 1.22 and librpm 4.x when written.

DPKG_VERREVCMP = [
    ("1.0", "1.0", 0),
    ("1.0", "1.1", -1),
    ("1.1", "1.0", 1),
    ("1.0", "1.0.0", -1),
    ("1.0~", "1.0", -1),
    ("1.0~~", "1.0~", -1),
    ("1.0~a", "1.0~", 1),
    ("1.0a", "1.0", 1),
    ("1.0+", "1.0a", 1),
    ("1.0.", "1.0+", 1),
    ("1.0-", "1.0a", 1),
    ("9", "10", -1),
    ("007", "7", 0),
    ("1a2", "1a10", -1),
    ("a1", "1", 1),
    ("0", "0", 0),
]


@pytest.mark.parametrize(("a", "b", "expected"), DPKG_VERREVCMP)
def test_dpkg_verrevcmp_vectors(a, b, expected):
    assert ver._dpkg_verrevcmp(a, b) == expected
    assert ver._dpkg_verrevcmp(b, a) == -expected


RPMVERCMP = """
1.0 1.0 0|1.0 2.0 -1|2.0 1.0 1|2.0.1 2.0.1 0|2.0 2.0.1 -1|2.0.1 2.0 1|2.0.1a 2.0.1a 0
2.0.1a 2.0.1 1|2.0.1 2.0.1a -1|5.5p1 5.5p1 0|5.5p1 5.5p2 -1|5.5p2 5.5p1 1|5.5p10 5.5p10 0
5.5p1 5.5p10 -1|5.5p10 5.5p1 1|10xyz 10.1xyz -1|10.1xyz 10xyz 1|xyz10 xyz10 0
xyz10 xyz10.1 -1|xyz10.1 xyz10 1|xyz.4 xyz.4 0|xyz.4 8 -1|8 xyz.4 1|xyz.4 2 -1|2 xyz.4 1
5.5p2 5.6p1 -1|5.6p1 5.5p2 1|5.6p1 6.5p1 -1|6.5p1 5.6p1 1|6.0.rc1 6.0 1|6.0 6.0.rc1 -1
10b2 10a1 1|10a2 10b2 -1|1.0aa 1.0aa 0|1.0a 1.0aa -1|1.0aa 1.0a 1|10.0001 10.0001 0
10.0001 10.1 0|10.1 10.0001 0|10.0001 10.0039 -1|10.0039 10.0001 1|4.999.9 5.0 -1
5.0 4.999.9 1|20101121 20101121 0|20101121 20101122 -1|20101122 20101121 1|2_0 2_0 0
2.0 2_0 0|2_0 2.0 0|a a 0|a+ a+ 0|a+ a_ 0|a_ a+ 0|+a +a 0|+a _a 0|_a +a 0|+_ +_ 0
_+ +_ 0|_+ _+ 0|+ _ 0|_ + 0|1.0~rc1 1.0~rc1 0|1.0~rc1 1.0 -1|1.0 1.0~rc1 1
1.0~rc1 1.0~rc2 -1|1.0~rc2 1.0~rc1 1|1.0~rc1~git123 1.0~rc1~git123 0
1.0~rc1~git123 1.0~rc1 -1|1.0~rc1 1.0~rc1~git123 1|1.0^ 1.0^ 0|1.0^ 1.0 1|1.0 1.0^ -1
1.0^git1 1.0^git1 0|1.0^git1 1.0 1|1.0 1.0^git1 -1|1.0^git1 1.0^git2 -1|1.0^git2 1.0^git1 1
1.0^git1 1.01 -1|1.01 1.0^git1 1|1.0^20160101 1.0^20160101 0|1.0^20160101 1.0.1 -1
1.0.1 1.0^20160101 1|1.0^20160101^git1 1.0^20160101^git1 0|1.0^20160102 1.0^20160101^git1 1
1.0^20160101^git1 1.0^20160102 -1|1.0~rc1^git1 1.0~rc1^git1 0|1.0~rc1^git1 1.0~rc1 1
1.0~rc1 1.0~rc1^git1 -1|1.0^git1~pre 1.0^git1~pre 0|1.0^git1 1.0^git1~pre 1
1.0^git1~pre 1.0^git1 -1
"""
RPMVERCMP_VECTORS = [
    (a, b, int(e))
    for a, b, e in (item.split() for item in RPMVERCMP.replace("\n", "|").split("|") if item.strip())
]


def test_rpmvercmp_vector_table_is_loaded():
    assert len(RPMVERCMP_VECTORS) == 91


@pytest.mark.parametrize(("a", "b", "expected"), RPMVERCMP_VECTORS)
def test_rpmvercmp_vectors(a, b, expected):
    assert ver._rpmvercmp(a, b) == expected
    assert ver._rpmvercmp(b, a) == -expected


# ── Property tests: laws every scheme must obey ──────────────────────

def _pad(r: random.Random, n: int) -> str:
    """A number, sometimes zero padded (equal spellings are wanted)."""
    return f"{n:0{r.choice([1, 1, 2, 3])}d}"


def _gen_win_build(r):
    parts = [r.choice([0, 1, 6, 10, 20348, 20349, 587, 2700]) for _ in range(r.randint(1, 4))]
    text = ".".join(_pad(r, p) for p in parts)
    if len(parts) == 4 and r.random() < 0.3:
        text += f" Build {parts[2]}.{parts[3]}"
    return text


def _gen_semverish(r):
    core = ".".join(_pad(r, r.choice([0, 1, 2, 9, 10, 25])) for _ in range(r.randint(1, 4)))
    tail = r.choice(["", "", "", "a", "b", "z", "za", "-rc1", "-rc2", "rc10", "-beta", "-beta.2",
                     "-alpha1", "a1", "b2", ".dev3", "-dev", "_rc.1", "-RC1"])
    meta = r.choice(["", "", "", "+b5", "+exp.sha"])
    return r.choice(["", "", "v"]) + core + tail + meta


def _gen_fortios(r):
    triple = f"{r.choice([6, 7])}.{r.choice([0, 2, 4])}.{r.choice([0, 5, 8, 12])}"
    build = r.choice([157, 1517, 1600])
    return r.choice([
        "{t}", "v{t}", "v{t},build{b},230606 (GA.F)", "v{t},build{b}", "{t}-build{b}",
        "FortiOS v{t}", "v{t},build0{b},230606 (GA)",
    ]).format(t=triple, b=build)


def _gen_panos(r):
    base = f"{r.choice([9, 10, 11])}.{r.choice([0, 1, 2])}.{r.choice([0, 2, 10])}"
    return r.choice(["", "PAN-OS ", "v"]) + base + r.choice(["", "", "-h0", "-h1", "-h3", "-h10", "-H25"])


def _gen_cisco_ios(r):
    train = r.choice(["E", "E", "M", "T", "SE", "SY", "", "SRE"])
    major, minor = r.choice([(12, 2), (15, 0), (15, 2), (15, 9)])
    maint = f"{_pad(r, r.choice([1, 4, 5, 55, 7]))}{r.choice(['', '', 'a'])}"
    tail = ""
    if train and r.random() < 0.8:
        tail = _pad(r, r.choice([1, 8, 10, 12])) + r.choice(["", "", "a"])
    prefix = r.choice(["", "", "Version ", "IOS "])
    suffix = r.choice(["", "", ", RELEASE SOFTWARE (fc1)"])
    return f"{prefix}{major}.{minor}({maint}){train}{tail}{suffix}"


def _gen_cisco_iosxe(r):
    if r.random() < 0.2:
        return f"{_pad(r, 3)}.{_pad(r, r.choice([6, 7, 16]))}.{_pad(r, r.choice([4, 5]))}.{r.choice(['E', 'S'])}"
    major, minor = r.choice([(16, 9), (16, 12), (17, 3), (17, 9), (17, 10)])
    text = f"{major}.{_pad(r, minor)}.{_pad(r, r.choice([1, 3, 4, 5]))}{r.choice(['', '', 'a', 'b'])}"
    return r.choice(["", "", "IOS XE ", "Cupertino-"]) + text


def _gen_cisco_asa(r):
    major, minor = r.choice([(9, 8), (9, 12), (9, 16), (9, 18), (8, 4)])
    maint = _pad(r, r.choice([1, 2, 4, 7]))
    interim = r.choice([0, 0, 18, 45, 57, 67])
    if r.random() < 0.25 and interim:
        text = f"{major}.{minor}({maint}.{_pad(r, interim)})"
    else:
        text = f"{major}.{minor}({maint})" + (_pad(r, interim) if interim else "")
    return r.choice(["", "", "Version "]) + text


def _gen_junos(r):
    rtype = r.choice(["R", "R", "R", "F", "X", "B"])
    major, minor = r.choice([(19, 4), (20, 4), (21, 2), (21, 4), (15, 1), (17, 3)])
    rel = r.choice([49, 53]) if rtype == "X" else r.choice([1, 2, 3, 6])
    kind = "D" if rtype == "X" else "S"
    service = r.choice(["", "", f"-{kind}1", f"-{kind}5", f"-{kind}9", f"-{kind}170", f"-{kind}0"])
    spin = r.choice(["", "", ".1", ".4", ".0"])
    evo = r.choice(["", "", "", "-EVO"])
    return r.choice(["", "", "JUNOS "]) + f"{major}.{minor}{rtype}{rel}{service}{spin}{evo}"


def _gen_dpkg(r):
    atoms = ["0", "1", "2", "9", "10", "007", "a", "b", "z", "~", "~rc1", "+b1", "+dfsg", ".",
             "ubuntu1", "ubuntu0.1", "deb12u1", "rc1"]
    upstream = str(r.randint(0, 3)) + "".join(r.choice(atoms) for _ in range(r.randint(0, 4)))
    revision = "" if r.random() < 0.4 else "-" + "".join(r.choice(atoms) for _ in range(r.randint(1, 3)))
    epoch = "" if r.random() < 0.7 else f"{r.randint(0, 2)}:"
    return epoch + upstream + revision


def _gen_rpm(r):
    atoms = ["0", "1", "2", "9", "10", "007", "a", "b", "aa", "~", "^", ".", "_", "+", "rc1",
             "git123", "el9", "el9_4", "fc40", "p1", "20240101", "~rc", "^git"]
    version = str(r.randint(0, 3)) + "".join(r.choice(atoms) for _ in range(r.randint(0, 4)))
    release = "" if r.random() < 0.4 else "-" + "".join(r.choice(atoms) for _ in range(r.randint(1, 3)))
    epoch = "" if r.random() < 0.7 else f"{r.randint(0, 2)}:"
    return epoch + version + release


GENERATORS = {
    "win_build": _gen_win_build,
    "semverish": _gen_semverish,
    "fortios": _gen_fortios,
    "panos": _gen_panos,
    "cisco_ios": _gen_cisco_ios,
    "cisco_iosxe": _gen_cisco_iosxe,
    "cisco_asa": _gen_cisco_asa,
    "junos": _gen_junos,
    "dpkg": _gen_dpkg,
    "rpm": _gen_rpm,
}

# Schemes where comparability is not an equivalence relation: FortiOS refuses
# a version with a stated build against the same version without one, so
# "a~b and b~c implies a~c" does not hold there.
NON_TRANSITIVE_COMPARABILITY = {"fortios"}


def _generated_set(scheme: str, size: int = 70) -> list[str]:
    r = random.Random(f"versioning-{scheme}")
    gen = GENERATORS[scheme]
    values: list[str] = []
    seen = set()
    attempts = 0
    while len(values) < size and attempts < size * 50:
        attempts += 1
        v = gen(r)
        if v in seen:
            continue
        seen.add(v)
        values.append(v)
    return values


@pytest.mark.parametrize("scheme", SCHEMES)
def test_generator_only_produces_valid_versions(scheme):
    values = _generated_set(scheme)
    assert len(values) >= 40
    for v in values:
        assert parse(scheme, v) is not None, v


@pytest.mark.parametrize("scheme", SCHEMES)
def test_order_laws(scheme):
    values = _generated_set(scheme)
    n = len(values)
    m = [[compare(scheme, a, b) for b in values] for a in values]

    outcomes = {x for row in m for x in row}
    assert {-1, 0, 1} <= outcomes, "generator should exercise all three orderings"
    if scheme in ("cisco_ios", "cisco_iosxe", "cisco_asa", "junos", "fortios"):
        assert None in outcomes, "generator should exercise refusals"
    else:
        assert None not in outcomes, "total-order schemes never refuse valid input"

    for i in range(n):
        assert m[i][i] == 0, values[i]  # reflexive
        for j in range(i + 1, n):
            # Antisymmetry; None is symmetric too.
            if m[i][j] is None:
                assert m[j][i] is None, (values[i], values[j])
            else:
                assert m[j][i] == -m[i][j], (values[i], values[j])

    closed = scheme not in NON_TRANSITIVE_COMPARABILITY
    for i in range(n):
        for j in range(n):
            x = m[i][j]
            for k in range(n):
                y, z = m[j][k], m[i][k]
                if closed and x is not None and y is not None:
                    assert z is not None, (values[i], values[j], values[k])
                if x is None or y is None or z is None:
                    continue
                # Transitivity of <= and < over triples that are all comparable.
                if x <= 0 and y <= 0:
                    assert z <= 0, (values[i], values[j], values[k])
                    if x < 0 or y < 0:
                        assert z < 0, (values[i], values[j], values[k])
                # Equal versions are interchangeable.
                if x == 0:
                    assert z == y, (values[i], values[j], values[k])


@pytest.mark.parametrize("scheme", SCHEMES)
def test_highest_matches_pairwise_maximum(scheme):
    values = _generated_set(scheme, size=30)
    r = random.Random(f"highest-{scheme}")
    for _ in range(60):
        sample = r.sample(values, r.randint(1, 6))
        got = highest(scheme, sample)
        expected = [
            c for c in sample
            if all(compare(scheme, c, o) in (0, 1) for o in sample)
        ]
        if expected:
            assert got == expected[0], sample
        else:
            assert got is None, sample


@pytest.mark.parametrize("scheme", SCHEMES)
def test_canonicalize_is_idempotent_and_order_preserving(scheme):
    for v in _generated_set(scheme):
        c1 = canonicalize(scheme, v)
        assert c1 is not None, v
        version, build = c1
        assert canonicalize(scheme, version) == (version, None), (v, version)
        assert compare(scheme, v, _with_build(scheme, version, build)) == 0, (v, version)
        cycle = release_cycle(scheme, v)
        assert cycle is None or isinstance(cycle, str)


def test_throughput_50k_semverish_comparisons():
    r = random.Random("throughput")
    pool = [_gen_semverish(r) for _ in range(5_000)]
    pairs = [(r.choice(pool), r.choice(pool)) for _ in range(50_000)]
    start = time.perf_counter()
    results = [compare("semverish", a, b) for a, b in pairs]
    elapsed = time.perf_counter() - start
    assert all(x in (-1, 0, 1) for x in results)
    assert elapsed < 3.0, f"50,000 semverish comparisons took {elapsed:.2f}s"


def test_throughput_hostile_input_is_fast():
    # Long and pathological strings are refused cheaply for every scheme.
    hostile = ["1." * 5_000, "(" * 5_000, "1" * 5_000, "~" * 5_000, ("1.2.3-rc" * 40)]
    start = time.perf_counter()
    for _ in range(200):
        for scheme in SCHEMES:
            for h in hostile:
                compare(scheme, h, h)
    assert time.perf_counter() - start < 3.0
