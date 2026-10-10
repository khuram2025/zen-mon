"""Scheme-aware version comparators for Inventory & Compliance.

Pure functions, no I/O, no third-party dependencies. Shared by the inventory
normaliser, software baselines and (later) lifecycle and CVE matching.

Contract
--------
``compare(scheme, a, b)`` returns ``-1``, ``0`` or ``1``, or ``None`` meaning
NOT COMPARABLE. ``None`` is never a guess: the caller shows "unknown" (the UI
shows a dash) rather than an ordering that may be wrong. ``None`` is returned
when either side is not a valid version for the scheme (including ``None``,
non-strings, empty and over-long input) and when both sides are valid but the
scheme says they cannot be ordered (cross-train Cisco IOS, Junos lines that
diverge, and so on). Malformed input never raises. An unknown *scheme* is a
programming error and raises ``ValueError``.

Input is stripped of surrounding whitespace and limited to 256 characters.
Digits are ASCII only (Unicode digits, which ``int()`` would accept, are
refused). Every parser is a full-match regular expression with no nested
quantifiers, so hostile input cannot cause catastrophic backtracking.

Parsed values are frozen dataclasses. Equality and hashing use the fields that
take part in ordering only: display text and build metadata are excluded, so
``parse_semverish("1.0") == parse_semverish("1.0.0")``. Every parsed value also
exposes ``text`` (canonical version string), ``build`` (build identifier or
None) and ``cycle`` (lifecycle cycle or None). Parsers are cached; parsed
values are immutable, so sharing them is safe.

Schemes
-------
``win_build`` (Windows OS builds and MSI four-part versions)
    Accepts 1 to 4 dotted numeric parts, an optional leading ``v``, and an
    optional ``Build <n>[.<n>...]`` suffix, for example ``10.0.20348.587`` or
    ``10.0.20348.587 Build 20348.587``. Compared numerically per part, with
    missing parts equal to zero (``10.0`` == ``10.0.0.0``). Refuses five or
    more parts, anything non-numeric (marketing names such as ``21H2`` are not
    versions) and any other text. The ``Build`` suffix is returned as the build
    but never affects ordering. Cycle: the first three parts
    (``10.0.20348``), because the build number identifies the Windows release;
    None with fewer than three parts.

``semverish`` (most applications; the safe default)
    Accepts a dotted numeric core (one or more parts, optional leading ``v``),
    then at most one tail, then optional ``+build`` metadata. Tails:
    a post-release letter suffix of one or two letters with no separator and no
    number (``4a``, ``1.1.1k``); or a pre-release made of ``dev``, ``alpha``,
    ``beta`` or ``rc`` with an optional number, optionally separated by ``-``,
    ``_`` or ``.`` (``1.0-rc1``, ``2.0.0-beta.2``); or ``a`` / ``b`` directly
    or separated and followed by a number (``1.0a1``, ``1.0b2``, the PEP 440
    spelling). Ordering is: pre-release (dev < alpha < beta < rc) < plain
    release < letter suffix, so ``4 < 4a`` and ``1.0rc1 < 1.0``. Core parts are
    compared numerically and trailing zero parts are ignored (``1.0`` ==
    ``1.0.0``, ``25.01`` > ``24.09``, ``1.2.9`` < ``1.2.10``). ``+build``
    metadata is ignored for ordering and returned as the build. Refuses any
    other tail on purpose: ``1.2.3-1`` could be a pre-release (semver) or a
    packaging revision (Debian style), ``9.6p1`` and ``1.2.3-SNAPSHOT`` have no
    portable ordering. Text with no numeric core (``abc``) is not a version.
    Cycle: the first two core parts as written (``3.12.10`` -> ``3.12``,
    ``22.04.4`` -> ``22.04``; one part stays one part).

``fortios`` (FortiOS)
    Accepts ``[FortiOS ][v]MAJOR.MINOR.PATCH`` with an optional
    ``,buildN`` (also ``-buildN`` or `` buildN``), an optional six-digit build
    date after the build, and an optional ``(GA.F)`` style tag, for example
    ``v7.2.5,build1517,230606 (GA.F)``. Ordered by (major, minor, patch) and
    then by build number. When two versions have the same triple and only one
    states a build, the result is None: the unstated build may be older, equal
    or newer. Triples that differ are ordered whether or not builds are stated.
    Refuses versions without three numeric parts. Build: the build number
    without leading zeros. Cycle: ``MAJOR.MINOR``.

``panos`` (PAN-OS)
    Accepts ``[PAN-OS ][v]X.Y.Z`` with an optional ``-hN`` hotfix, for example
    ``11.1.10-h25``. Ordered by (x, y, z, hotfix) with no hotfix equal to
    ``-h0``, so ``11.1.2 < 11.1.2-h1`` and ``11.1.2-h3 < 11.1.2-h10``. Refuses
    other suffixes (``-beta``, ``.xfr`` builds) and versions without three
    parts. Cycle: ``X.Y``.

``cisco_ios`` (classic IOS: ``15.2(4)E8``)
    Accepts ``MAJOR.MINOR(MAINT[letters])TRAIN[REBUILD[letters]]`` with an
    optional ``IOS``, ``Cisco IOS`` and ``Version`` prefix and an optional
    trailing ``, RELEASE SOFTWARE (fc1)``. TRAIN is the upper-case train
    designator (``E``, ``SE``, ``M``, ``T``, ``SRE``) and may be empty for
    mainline releases such as ``12.4(25f)``. Ordered by (major, minor, maint,
    maint letter, rebuild, rebuild letter), so ``15.2(4)E8 < 15.2(4)E10`` and
    ``15.1(4)M12 < 15.1(4)M12a``. Comparison is refused (None) when the train
    designators differ, because trains branch and merge independently:
    ``15.2(4)E8`` and ``15.2(4)M8`` have no meaningful order, and neither do
    successive designators such as ``SRD`` and ``SRE`` or mainline against a
    train. Lower-case trains and anything that does not fit the grammar are
    refused. Cycle: ``MAJOR.MINOR``.

``cisco_iosxe`` (IOS XE: ``17.9.4a``)
    Accepts ``MAJOR.MINOR.MAINT[letters]`` with zero-padded parts allowed
    (``17.09.04a``), an optional ``IOS XE``, ``Cisco IOS XE Software`` and
    ``Version`` prefix, an optional release codename (``Cupertino-17.9.4a``),
    an optional trailing ``, RELEASE SOFTWARE (fc3)`` and, for the legacy 3.x
    line, a train (``03.06.05.E``, ``3.7.4S``). Ordered by (major, minor,
    maint, letter): ``17.9.4 < 17.9.4a < 17.10.1``. Legacy versions with a train
    compare only within the same train; they are never ordered against the
    16.x and 17.x line (None). Canonical text drops the padding
    (``17.09.04a`` -> ``17.9.4a``), the form used by CPE and endoflife.date.
    Cycle: ``MAJOR.MINOR``.

``cisco_asa`` (Cisco ASA and FTD: ``9.16(4)57``)
    Accepts ``MAJOR.MINOR(MAINT)[INTERIM]`` with an optional ``Version`` prefix,
    for example ``9.16(4)57``, ``9.8(4)45`` and ``9.16(4)``, and the dotted
    interim form ``9.12(4.65)``. INTERIM is the interim build of a maintenance
    release; an absent interim is the initial build and orders as zero, so
    ``9.16(4) < 9.16(4)18 < 9.16(4)57 < 9.16(5)``. Ordered by (major, minor,
    maint, interim). The dotted form and the trailing form are different ways
    to write a build, not known to be the same one, so they are never ordered
    against each other (None). Canonical text drops zero padding. Cycle:
    ``MAJOR.MINOR``.

``junos`` (Juniper Junos and Junos Evolved)
    Accepts ``[JUNOS ]MAJOR.MINOR<R|F|B|X>REL[-S<n>|-D<n>][.<spin>][-EVO]``
    for example ``21.4R3-S5.4``, ``20.4R3-S9`` and ``15.1X49-D170.4``. ``-S`` is
    the service release of R, F and B releases and ``-D`` that of X releases.
    Ordered by (major, minor, release, service release, spin), absent parts
    being zero, so ``21.4R3-S5 < 21.4R3-S5.4`` and ``20.4R3-S9 < 21.4R3``.
    Comparison is refused (None) unless both sides are the same kind of line:
    the same release letter; for F and B releases also the same
    major.minor; for X releases also the same major.minor and X number (an X
    train is a platform branch that still receives service releases years
    after a later R release, so ``15.1X49-D170`` against ``16.1R4`` has no
    order); and the same OS (Junos Evolved ``-EVO`` is never ordered against
    Junos). Refuses ``I`` (internal) builds and other forms. Cycle:
    ``MAJOR.MINOR`` (``MAJOR.MINORXn`` for X releases).

``dpkg`` (Debian, Ubuntu packages)
    Accepts ``[epoch:]upstream[-revision]``. Epoch is digits; upstream starts
    with a digit and uses ``A-Za-z0-9.+~`` (and ``-`` when there is a
    revision); revision uses ``A-Za-z0-9.+~``. Compared exactly like dpkg:
    epoch, then upstream, then revision, each with the dpkg ``verrevcmp``
    algorithm in which ``~`` sorts before everything including the end of the
    string, letters sort before other punctuation, and digit runs compare
    numerically. ``1:1.2-3ubuntu1 > 1.9-1``, ``1.0~rc1 < 1.0`` and
    ``2.0-1ubuntu0.1 > 2.0-1``. Refuses a non-numeric epoch, an empty revision
    and an upstream that does not start with a digit. Cycle: the first two
    numeric parts of the upstream version.

``rpm`` (RPM EVR)
    Accepts ``[epoch:]version[-release]``. Epoch is digits (a missing epoch is
    zero); version starts with a digit and uses ``A-Za-z0-9._+~^``; release
    uses the same characters. Compared with the rpm ``rpmvercmp`` algorithm:
    epoch, then version, then release; numeric and alphabetic segments are
    compared separately, a numeric segment is newer than an alphabetic one,
    ``~`` sorts before everything, ``^`` sorts after the end of the string but
    before any further segment, and other punctuation only separates
    segments. ``1.0-1.el9 < 1.0-2.el9`` and ``1:0.9 > 2.0``. A missing release
    sorts before any release. Refuses a non-numeric epoch, an empty release and
    a version that does not start with a digit. Cycle: the first two numeric
    parts of the version.

Where not covered
-----------------
Schemes for NX-OS, AOS-CX and similar are not defined; ``scheme_for`` maps them
to ``semverish``, which then refuses forms such as ``10.2(3)`` instead of
guessing. NX-OS is left out on purpose: its trains (``7.0(3)I7(9)``, ``10.2(3)F``)
branch like IOS trains and no ordering across them is known to be right. Version strings with free text around them (``Zoom 5.17.5 (31501)``,
a whole sysDescr) must be cleaned by the normaliser first; the parsers accept
only the documented prefixes and suffixes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Callable, Iterable, Literal, Optional, get_args

__all__ = [
    "Scheme",
    "SCHEMES",
    "compare",
    "highest",
    "canonicalize",
    "equivalence_key",
    "natural_key",
    "EQUIVALENCE_KEY_SQL",
    "BLANK_VERSION_SQL",
    "release_cycle",
    "scheme_for",
    "parse",
    "parse_win_build",
    "parse_semverish",
    "parse_fortios",
    "parse_panos",
    "parse_cisco_ios",
    "parse_cisco_iosxe",
    "parse_cisco_asa",
    "parse_junos",
    "parse_dpkg",
    "parse_rpm",
    "WinBuildVersion",
    "SemverishVersion",
    "FortiosVersion",
    "PanosVersion",
    "CiscoIosVersion",
    "CiscoIosXeVersion",
    "CiscoAsaVersion",
    "JunosVersion",
    "DpkgVersion",
    "RpmVersion",
]

Scheme = Literal[
    "win_build",
    "semverish",
    "fortios",
    "panos",
    "cisco_ios",
    "cisco_iosxe",
    "cisco_asa",
    "junos",
    "dpkg",
    "rpm",
]
SCHEMES: tuple[str, ...] = get_args(Scheme)

_MAX_LEN = 256
_CACHE_SIZE = 4096


# ── Shared helpers ───────────────────────────────────────────────────

def _clean(raw: object) -> Optional[str]:
    """Stripped text, or None for non-strings, empty and over-long input."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > _MAX_LEN:
        return None
    return s


def _sign(a: Any, b: Any) -> int:
    return -1 if a < b else (1 if a > b else 0)


def _is_digit(c: str) -> bool:
    return "0" <= c <= "9"


def _is_alpha(c: str) -> bool:
    return "a" <= c <= "z" or "A" <= c <= "Z"


def _first_two(version: str) -> Optional[str]:
    """First two dotted numeric parts as written ('22.04.4' -> '22.04')."""
    m = re.match(r"[0-9]+(?:\.[0-9]+)?", version)
    return m.group(0) if m else None


# ── win_build ────────────────────────────────────────────────────────

_WIN_RE = re.compile(
    r"v?([0-9]+(?:\.[0-9]+){0,3})(?:\s+build\s+([0-9]+(?:\.[0-9]+){0,3}))?",
    re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True, slots=True)
class WinBuildVersion:
    parts: tuple[int, ...]  # always four parts, missing ones zero
    text: str = field(compare=False)
    build: Optional[str] = field(compare=False, default=None)

    @property
    def cycle(self) -> Optional[str]:
        nums = self.text.split(".")
        return ".".join(nums[:3]) if len(nums) >= 3 else None


@lru_cache(maxsize=_CACHE_SIZE)
def _win_build(s: str) -> Optional[WinBuildVersion]:
    m = _WIN_RE.fullmatch(s)
    if m is None:
        return None
    nums = [int(p) for p in m.group(1).split(".")]
    build = None
    if m.group(2):
        build = ".".join(str(int(p)) for p in m.group(2).split("."))
    return WinBuildVersion(
        parts=tuple(nums + [0] * (4 - len(nums))),
        text=".".join(str(n) for n in nums),
        build=build,
    )


def parse_win_build(raw: object) -> Optional[WinBuildVersion]:
    s = _clean(raw)
    return None if s is None else _win_build(s)


def _cmp_win_build(a: WinBuildVersion, b: WinBuildVersion) -> Optional[int]:
    return _sign(a.parts, b.parts)


# ── semverish ────────────────────────────────────────────────────────

_SEMVER_CORE_RE = re.compile(r"v?([0-9]+(?:\.[0-9]+)*)", re.IGNORECASE | re.ASCII)
_SEMVER_TAIL_RE = re.compile(r"([-_.]?)([A-Za-z]+)(?:([-_.]?)([0-9]+))?", re.ASCII)
_SEMVER_BUILD_RE = re.compile(r"[0-9A-Za-z.-]+", re.ASCII)
_PRE_RANK = {"dev": 0, "alpha": 1, "beta": 2, "rc": 3}
_PRE_SHORT = {"a": "alpha", "b": "beta"}


@dataclass(frozen=True, slots=True)
class SemverishVersion:
    core: tuple[int, ...]  # trailing zero parts removed
    pre: Optional[tuple[int, int]]  # (rank, number) of a pre-release
    letter: str  # post-release letter suffix, lower case
    text: str = field(compare=False)
    build: Optional[str] = field(compare=False, default=None)
    cycle: Optional[str] = field(compare=False, default=None)

    def _key(self) -> tuple:
        # Stage: 0 pre-release, 1 plain release, 2 letter suffix.
        if self.pre is not None:
            return (self.core, 0, self.pre[0], self.pre[1], "")
        if self.letter:
            return (self.core, 2, 0, 0, self.letter)
        return (self.core, 1, 0, 0, "")


@lru_cache(maxsize=_CACHE_SIZE)
def _semverish(s: str) -> Optional[SemverishVersion]:
    main, plus, build = s.partition("+")
    if plus and not _SEMVER_BUILD_RE.fullmatch(build):
        return None
    m = _SEMVER_CORE_RE.match(main)
    if m is None:
        return None
    comps = m.group(1).split(".")
    rest = main[m.end():]
    pre: Optional[tuple[int, int]] = None
    letter = ""
    if rest:
        t = _SEMVER_TAIL_RE.fullmatch(rest)
        if t is None:
            return None
        sep, word, sep2, num = t.group(1), t.group(2).lower(), t.group(3), t.group(4)
        if word in _PRE_RANK:
            pre = (_PRE_RANK[word], int(num) if num else 0)
        elif word in _PRE_SHORT and num is not None:
            pre = (_PRE_RANK[_PRE_SHORT[word]], int(num))
        elif not sep and not sep2 and num is None and len(word) <= 2:
            letter = word
        else:
            return None
    core = tuple(int(c) for c in comps)
    while core and core[-1] == 0:
        core = core[:-1]
    return SemverishVersion(
        core=core,
        pre=pre,
        letter=letter,
        text=(m.group(1) + rest).lower(),
        build=build if plus else None,
        cycle=".".join(comps[:2]),
    )


def parse_semverish(raw: object) -> Optional[SemverishVersion]:
    s = _clean(raw)
    return None if s is None else _semverish(s)


def _cmp_semverish(a: SemverishVersion, b: SemverishVersion) -> Optional[int]:
    return _sign(a._key(), b._key())


# ── fortios ──────────────────────────────────────────────────────────

_FORTI_RE = re.compile(
    r"(?:fortios\s+)?v?([0-9]+)\.([0-9]+)\.([0-9]+)"
    r"(?:[,\s-]+build\s*([0-9]+)(?:[,\s]+[0-9]{6})?)?"
    r"(?:\s*\(([A-Za-z0-9._ -]*)\))?",
    re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True, slots=True)
class FortiosVersion:
    major: int
    minor: int
    patch: int
    build_no: Optional[int]
    tag: str = field(compare=False, default="")

    @property
    def text(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def build(self) -> Optional[str]:
        return None if self.build_no is None else str(self.build_no)

    @property
    def cycle(self) -> Optional[str]:
        return f"{self.major}.{self.minor}"


@lru_cache(maxsize=_CACHE_SIZE)
def _fortios(s: str) -> Optional[FortiosVersion]:
    m = _FORTI_RE.fullmatch(s)
    if m is None:
        return None
    return FortiosVersion(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        patch=int(m.group(3)),
        build_no=int(m.group(4)) if m.group(4) is not None else None,
        tag=(m.group(5) or "").strip(),
    )


def parse_fortios(raw: object) -> Optional[FortiosVersion]:
    s = _clean(raw)
    return None if s is None else _fortios(s)


def _cmp_fortios(a: FortiosVersion, b: FortiosVersion) -> Optional[int]:
    ta, tb = (a.major, a.minor, a.patch), (b.major, b.minor, b.patch)
    if ta != tb:
        return _sign(ta, tb)
    if a.build_no is None and b.build_no is None:
        return 0
    if a.build_no is None or b.build_no is None:
        return None
    return _sign(a.build_no, b.build_no)


# ── panos ────────────────────────────────────────────────────────────

_PAN_RE = re.compile(
    r"(?:pan-?os\s+)?v?([0-9]+)\.([0-9]+)\.([0-9]+)(?:-h([0-9]+))?",
    re.IGNORECASE | re.ASCII,
)


@dataclass(frozen=True, slots=True)
class PanosVersion:
    major: int
    minor: int
    patch: int
    hotfix: int  # 0 when the version carries no hotfix

    @property
    def text(self) -> str:
        base = f"{self.major}.{self.minor}.{self.patch}"
        return f"{base}-h{self.hotfix}" if self.hotfix else base

    @property
    def build(self) -> Optional[str]:
        return None

    @property
    def cycle(self) -> Optional[str]:
        return f"{self.major}.{self.minor}"


@lru_cache(maxsize=_CACHE_SIZE)
def _panos(s: str) -> Optional[PanosVersion]:
    m = _PAN_RE.fullmatch(s)
    if m is None:
        return None
    return PanosVersion(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        patch=int(m.group(3)),
        hotfix=int(m.group(4)) if m.group(4) is not None else 0,
    )


def parse_panos(raw: object) -> Optional[PanosVersion]:
    s = _clean(raw)
    return None if s is None else _panos(s)


def _cmp_panos(a: PanosVersion, b: PanosVersion) -> Optional[int]:
    return _sign(
        (a.major, a.minor, a.patch, a.hotfix),
        (b.major, b.minor, b.patch, b.hotfix),
    )


# ── cisco_ios ────────────────────────────────────────────────────────

# The prefix and suffix are case-insensitive; the body is not, because a lower
# case letter after the train is a rebuild letter and the train is upper case.
_IOS_RE = re.compile(
    r"(?i:(?:cisco\s+)?(?:ios[\s-]+)?(?:version\s+)?)"
    r"([0-9]+)\.([0-9]+)\(([0-9]+)([a-z]{0,2})\)([A-Z]*)(?:([0-9]+)([a-z]{0,2}))?"
    r"(?i:(?:,\s*release\s+software(?:\s*\([^)]*\))?)?)",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class CiscoIosVersion:
    major: int
    minor: int
    maint: int
    maint_letter: str
    train: str  # '' for mainline
    rebuild: int  # 0 when absent
    rebuild_letter: str

    @property
    def text(self) -> str:
        reb = str(self.rebuild) if self.rebuild else ""
        return (
            f"{self.major}.{self.minor}({self.maint}{self.maint_letter})"
            f"{self.train}{reb}{self.rebuild_letter}"
        )

    @property
    def build(self) -> Optional[str]:
        return None

    @property
    def cycle(self) -> Optional[str]:
        return f"{self.major}.{self.minor}"


@lru_cache(maxsize=_CACHE_SIZE)
def _cisco_ios(s: str) -> Optional[CiscoIosVersion]:
    m = _IOS_RE.fullmatch(s)
    if m is None:
        return None
    train = m.group(5)
    rebuild = int(m.group(6)) if m.group(6) else 0
    if not train and m.group(6):
        return None  # a rebuild number only exists within a train
    return CiscoIosVersion(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        maint=int(m.group(3)),
        maint_letter=m.group(4),
        train=train,
        rebuild=rebuild,
        rebuild_letter=m.group(7) or "",
    )


def parse_cisco_ios(raw: object) -> Optional[CiscoIosVersion]:
    s = _clean(raw)
    return None if s is None else _cisco_ios(s)


def _cmp_cisco_ios(a: CiscoIosVersion, b: CiscoIosVersion) -> Optional[int]:
    if a.train != b.train:
        return None
    return _sign(
        (a.major, a.minor, a.maint, a.maint_letter, a.rebuild, a.rebuild_letter),
        (b.major, b.minor, b.maint, b.maint_letter, b.rebuild, b.rebuild_letter),
    )


# ── cisco_iosxe ──────────────────────────────────────────────────────

_IOSXE_RE = re.compile(
    r"(?i:(?:(?:cisco\s+)?ios[\s-]*xe[\s-]*)?(?:software\s*,?\s*)?(?:version\s+)?"
    r"(?:[a-z]+-)?)"
    r"([0-9]+)\.([0-9]+)\.([0-9]+)([a-z]{0,2})(?:\.?([A-Z]{1,3}))?"
    r"(?i:(?:,\s*release\s+software(?:\s*\([^)]*\))?)?)",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class CiscoIosXeVersion:
    major: int
    minor: int
    maint: int
    letter: str
    train: str  # '' for 16.x and later; set only on the legacy 3.x line

    @property
    def text(self) -> str:
        base = f"{self.major}.{self.minor}.{self.maint}{self.letter}"
        return f"{base}.{self.train}" if self.train else base

    @property
    def build(self) -> Optional[str]:
        return None

    @property
    def cycle(self) -> Optional[str]:
        return f"{self.major}.{self.minor}"


@lru_cache(maxsize=_CACHE_SIZE)
def _cisco_iosxe(s: str) -> Optional[CiscoIosXeVersion]:
    m = _IOSXE_RE.fullmatch(s)
    if m is None:
        return None
    return CiscoIosXeVersion(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        maint=int(m.group(3)),
        letter=m.group(4),
        train=m.group(5) or "",
    )


def parse_cisco_iosxe(raw: object) -> Optional[CiscoIosXeVersion]:
    s = _clean(raw)
    return None if s is None else _cisco_iosxe(s)


def _cmp_cisco_iosxe(a: CiscoIosXeVersion, b: CiscoIosXeVersion) -> Optional[int]:
    if a.train != b.train:
        return None
    return _sign(
        (a.major, a.minor, a.maint, a.letter),
        (b.major, b.minor, b.maint, b.letter),
    )


# ── cisco_asa ────────────────────────────────────────────────────────

_ASA_RE = re.compile(
    r"(?i:(?:version\s+)?)([0-9]+)\.([0-9]+)\(([0-9]+)(?:\.([0-9]+))?\)([0-9]+)?",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class CiscoAsaVersion:
    major: int
    minor: int
    maint: int
    interim: int  # 0 when absent
    dotted: bool  # interim written inside the parentheses: 9.12(4.65)

    @property
    def text(self) -> str:
        if self.dotted:
            return f"{self.major}.{self.minor}({self.maint}.{self.interim})"
        return f"{self.major}.{self.minor}({self.maint}){self.interim or ''}"

    @property
    def build(self) -> Optional[str]:
        return None

    @property
    def cycle(self) -> Optional[str]:
        return f"{self.major}.{self.minor}"


@lru_cache(maxsize=_CACHE_SIZE)
def _cisco_asa(s: str) -> Optional[CiscoAsaVersion]:
    m = _ASA_RE.fullmatch(s)
    if m is None or (m.group(4) is not None and m.group(5) is not None):
        return None  # an interim in both places is not a version
    return CiscoAsaVersion(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        maint=int(m.group(3)),
        interim=int(m.group(4) or m.group(5) or 0),
        dotted=m.group(4) is not None,
    )


def parse_cisco_asa(raw: object) -> Optional[CiscoAsaVersion]:
    s = _clean(raw)
    return None if s is None else _cisco_asa(s)


def _cmp_cisco_asa(a: CiscoAsaVersion, b: CiscoAsaVersion) -> Optional[int]:
    if a.dotted != b.dotted:
        return None
    return _sign((a.major, a.minor, a.maint, a.interim), (b.major, b.minor, b.maint, b.interim))


# ── junos ────────────────────────────────────────────────────────────

_JUNOS_RE = re.compile(
    r"(?i:(?:junos(?:\s+os)?\s+)?)"
    r"([0-9]+)\.([0-9]+)([RFBX])([0-9]+)(?:-([SD])([0-9]+))?(?:\.([0-9]+))?(-EVO)?",
    re.ASCII,
)


@dataclass(frozen=True, slots=True)
class JunosVersion:
    major: int
    minor: int
    rtype: str  # 'R', 'F', 'B' or 'X'
    rel: int
    service: int  # -S<n> (R, F, B) or -D<n> (X); 0 when absent
    spin: int  # trailing .<n>; 0 when absent
    evo: bool  # Junos Evolved

    @property
    def text(self) -> str:
        out = f"{self.major}.{self.minor}{self.rtype}{self.rel}"
        if self.service:
            out += f"-{'D' if self.rtype == 'X' else 'S'}{self.service}"
        if self.spin:
            out += f".{self.spin}"
        return out + ("-EVO" if self.evo else "")

    @property
    def build(self) -> Optional[str]:
        return None

    @property
    def cycle(self) -> Optional[str]:
        base = f"{self.major}.{self.minor}"
        return f"{base}X{self.rel}" if self.rtype == "X" else base


@lru_cache(maxsize=_CACHE_SIZE)
def _junos(s: str) -> Optional[JunosVersion]:
    m = _JUNOS_RE.fullmatch(s)
    if m is None:
        return None
    rtype, kind = m.group(3), m.group(5)
    if kind is not None and kind != ("D" if rtype == "X" else "S"):
        return None
    return JunosVersion(
        major=int(m.group(1)),
        minor=int(m.group(2)),
        rtype=rtype,
        rel=int(m.group(4)),
        service=int(m.group(6)) if m.group(6) else 0,
        spin=int(m.group(7)) if m.group(7) else 0,
        evo=m.group(8) is not None,
    )


def parse_junos(raw: object) -> Optional[JunosVersion]:
    s = _clean(raw)
    return None if s is None else _junos(s)


def _cmp_junos(a: JunosVersion, b: JunosVersion) -> Optional[int]:
    if a.evo != b.evo or a.rtype != b.rtype:
        return None
    if a.rtype in ("F", "B") and (a.major, a.minor) != (b.major, b.minor):
        return None
    if a.rtype == "X" and (a.major, a.minor, a.rel) != (b.major, b.minor, b.rel):
        return None
    return _sign(
        (a.major, a.minor, a.rel, a.service, a.spin),
        (b.major, b.minor, b.rel, b.service, b.spin),
    )


# ── dpkg ─────────────────────────────────────────────────────────────

_EPOCH_RE = re.compile(r"[0-9]+", re.ASCII)
_DPKG_UPSTREAM_RE = re.compile(r"[0-9][A-Za-z0-9.+~-]*", re.ASCII)
_DPKG_REVISION_RE = re.compile(r"[A-Za-z0-9.+~]+", re.ASCII)


def _dpkg_order(c: str) -> int:
    """dpkg's per-character weight; '' is the end of the string."""
    if not c or _is_digit(c):
        return 0
    if c == "~":
        return -1
    if _is_alpha(c):
        return ord(c)
    return ord(c) + 256


def _dpkg_verrevcmp(a: str, b: str) -> int:
    """Port of dpkg's verrevcmp() (lib/dpkg/version.c)."""
    i = j = 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        first_diff = 0
        while (i < la and not _is_digit(a[i])) or (j < lb and not _is_digit(b[j])):
            ac = _dpkg_order(a[i] if i < la else "")
            bc = _dpkg_order(b[j] if j < lb else "")
            if ac != bc:
                return -1 if ac < bc else 1
            i += 1
            j += 1
        while i < la and a[i] == "0":
            i += 1
        while j < lb and b[j] == "0":
            j += 1
        while i < la and j < lb and _is_digit(a[i]) and _is_digit(b[j]):
            if not first_diff:
                first_diff = ord(a[i]) - ord(b[j])
            i += 1
            j += 1
        if i < la and _is_digit(a[i]):
            return 1
        if j < lb and _is_digit(b[j]):
            return -1
        if first_diff:
            return -1 if first_diff < 0 else 1
    return 0


@dataclass(frozen=True, slots=True)
class DpkgVersion:
    epoch: int
    upstream: str
    revision: str  # '' when absent

    @property
    def text(self) -> str:
        out = f"{self.epoch}:" if self.epoch else ""
        out += self.upstream
        return f"{out}-{self.revision}" if self.revision else out

    @property
    def build(self) -> Optional[str]:
        return None

    @property
    def cycle(self) -> Optional[str]:
        return _first_two(self.upstream)


@lru_cache(maxsize=_CACHE_SIZE)
def _dpkg(s: str) -> Optional[DpkgVersion]:
    epoch = 0
    if ":" in s:
        e, _, s = s.partition(":")
        if not _EPOCH_RE.fullmatch(e):
            return None
        epoch = int(e)
    revision = ""
    if "-" in s:
        s, _, revision = s.rpartition("-")
        if not _DPKG_REVISION_RE.fullmatch(revision):
            return None
    if not _DPKG_UPSTREAM_RE.fullmatch(s):
        return None
    return DpkgVersion(epoch=epoch, upstream=s, revision=revision)


def parse_dpkg(raw: object) -> Optional[DpkgVersion]:
    s = _clean(raw)
    return None if s is None else _dpkg(s)


def _cmp_dpkg(a: DpkgVersion, b: DpkgVersion) -> Optional[int]:
    if a.epoch != b.epoch:
        return _sign(a.epoch, b.epoch)
    return _dpkg_verrevcmp(a.upstream, b.upstream) or _dpkg_verrevcmp(a.revision, b.revision)


# ── rpm ──────────────────────────────────────────────────────────────

_RPM_VERSION_RE = re.compile(r"[0-9][A-Za-z0-9._+~^]*", re.ASCII)
_RPM_RELEASE_RE = re.compile(r"[A-Za-z0-9._+~^]+", re.ASCII)


def _rpmvercmp(a: str, b: str) -> int:
    """Port of rpm's rpmvercmp() (rpmio/rpmvercmp.c), including ~ and ^."""
    if a == b:
        return 0
    i = j = 0
    la, lb = len(a), len(b)
    while i < la or j < lb:
        while i < la and not (_is_digit(a[i]) or _is_alpha(a[i])) and a[i] not in "~^":
            i += 1
        while j < lb and not (_is_digit(b[j]) or _is_alpha(b[j])) and b[j] not in "~^":
            j += 1
        ca = a[i] if i < la else ""
        cb = b[j] if j < lb else ""

        if ca == "~" or cb == "~":
            if ca != "~":
                return 1
            if cb != "~":
                return -1
            i += 1
            j += 1
            continue

        if ca == "^" or cb == "^":
            if not ca:
                return -1
            if not cb:
                return 1
            if ca != "^":
                return 1
            if cb != "^":
                return -1
            i += 1
            j += 1
            continue

        if not (ca and cb):
            break

        si, sj = i, j
        if _is_digit(ca):
            while si < la and _is_digit(a[si]):
                si += 1
            while sj < lb and _is_digit(b[sj]):
                sj += 1
            numeric = True
        else:
            while si < la and _is_alpha(a[si]):
                si += 1
            while sj < lb and _is_alpha(b[sj]):
                sj += 1
            numeric = False
        seg_a, seg_b = a[i:si], b[j:sj]
        if not seg_b:
            # Segments of different kinds: numeric is newer than alphabetic.
            return 1 if numeric else -1
        if numeric:
            seg_a, seg_b = seg_a.lstrip("0"), seg_b.lstrip("0")
            if len(seg_a) != len(seg_b):
                return 1 if len(seg_a) > len(seg_b) else -1
        if seg_a != seg_b:
            return -1 if seg_a < seg_b else 1
        i, j = si, sj

    if i >= la and j >= lb:
        return 0
    return -1 if i >= la else 1


@dataclass(frozen=True, slots=True)
class RpmVersion:
    epoch: int
    version: str
    release: str  # '' when absent

    @property
    def text(self) -> str:
        out = f"{self.epoch}:" if self.epoch else ""
        out += self.version
        return f"{out}-{self.release}" if self.release else out

    @property
    def build(self) -> Optional[str]:
        return None

    @property
    def cycle(self) -> Optional[str]:
        return _first_two(self.version)


@lru_cache(maxsize=_CACHE_SIZE)
def _rpm(s: str) -> Optional[RpmVersion]:
    epoch = 0
    if ":" in s:
        e, _, s = s.partition(":")
        if not _EPOCH_RE.fullmatch(e):
            return None
        epoch = int(e)
    release = ""
    if "-" in s:
        s, _, release = s.rpartition("-")
        if not _RPM_RELEASE_RE.fullmatch(release):
            return None
    if not _RPM_VERSION_RE.fullmatch(s):
        return None
    return RpmVersion(epoch=epoch, version=s, release=release)


def parse_rpm(raw: object) -> Optional[RpmVersion]:
    s = _clean(raw)
    return None if s is None else _rpm(s)


def _cmp_rpm(a: RpmVersion, b: RpmVersion) -> Optional[int]:
    if a.epoch != b.epoch:
        return _sign(a.epoch, b.epoch)
    return _rpmvercmp(a.version, b.version) or _rpmvercmp(a.release, b.release)


# ── Public API ───────────────────────────────────────────────────────

@dataclass(frozen=True, slots=True)
class _Impl:
    parse: Callable[[object], Any]
    compare: Callable[[Any, Any], Optional[int]]


_IMPLS: dict[str, _Impl] = {
    "win_build": _Impl(parse_win_build, _cmp_win_build),
    "semverish": _Impl(parse_semverish, _cmp_semverish),
    "fortios": _Impl(parse_fortios, _cmp_fortios),
    "panos": _Impl(parse_panos, _cmp_panos),
    "cisco_ios": _Impl(parse_cisco_ios, _cmp_cisco_ios),
    "cisco_iosxe": _Impl(parse_cisco_iosxe, _cmp_cisco_iosxe),
    "cisco_asa": _Impl(parse_cisco_asa, _cmp_cisco_asa),
    "junos": _Impl(parse_junos, _cmp_junos),
    "dpkg": _Impl(parse_dpkg, _cmp_dpkg),
    "rpm": _Impl(parse_rpm, _cmp_rpm),
}


def _impl(scheme: object) -> _Impl:
    impl = _IMPLS.get(scheme) if isinstance(scheme, str) else None
    if impl is None:
        raise ValueError(f"unknown version scheme: {scheme!r}")
    return impl


def parse(scheme: Scheme, raw: object) -> Any:
    """Parsed value for the scheme, or None when ``raw`` is not a valid version."""
    return _impl(scheme).parse(raw)


def compare(scheme: Scheme, a: object, b: object) -> Optional[int]:
    """-1, 0 or 1 for a < b, a == b, a > b; None when not comparable."""
    impl = _impl(scheme)
    pa, pb = impl.parse(a), impl.parse(b)
    if pa is None or pb is None:
        return None
    return impl.compare(pa, pb)


def highest(scheme: Scheme, versions: Iterable[object]) -> Optional[object]:
    """The element that compares >= every other element, as given.

    On ties the first such element wins. Returns None for an empty input, when
    any element is not a valid version, and when no element can be shown to be
    >= all of the others (some pair is not comparable). A bare string is not a
    list of versions and returns None.
    """
    impl = _impl(scheme)
    if isinstance(versions, (str, bytes)):
        return None
    try:
        items = list(versions)
    except TypeError:
        return None
    if not items:
        return None
    parsed = []
    for raw in items:
        p = impl.parse(raw)
        if p is None:
            return None
        parsed.append((raw, p))

    best_raw, best = parsed[0]
    for raw, p in parsed[1:]:
        c = impl.compare(p, best)
        # An incomparable pair cannot tell which is greater; move on and let the
        # verification pass decide whether some element dominates all others.
        if c is None or c > 0:
            best_raw, best = raw, p
    for _, p in parsed:
        c = impl.compare(best, p)
        if c is None or c < 0:
            return None
    return best_raw


def canonicalize(scheme: Scheme, raw: object) -> Optional[tuple[str, Optional[str]]]:
    """``(version, build_or_None)`` for the normaliser, or None when invalid.

    Examples: FortiOS ``v7.2.5,build1517,230606 (GA.F)`` -> ``('7.2.5', '1517')``;
    IOS XE ``17.09.04a`` -> ``('17.9.4a', None)``; Windows
    ``10.0.20348.587 Build 20348.587`` -> ``('10.0.20348.587', '20348.587')``.
    """
    p = _impl(scheme).parse(raw)
    return None if p is None else (p.text, p.build)


# The one definition of "the same version, spelled differently" that counts and filters share. Two spellings are
# one version when they differ only by case, surrounding blanks and trailing ``.0`` parts (``7.2`` and ``7.2.0``,
# ``14.38.33135`` and ``14.38.33135.0``, ``V1`` and ``v1``). It is deliberately text-only and scheme-free, so
# PostgreSQL can evaluate exactly the same function (``EQUIVALENCE_KEY_SQL``) in a filter or a GROUP BY: a number on
# a page and the list behind it can then never disagree about which spellings are one version. The comparators
# above are finer (``1.0.0-rc1`` equals ``1.0-rc1`` there) and order the groups; they never decide the grouping.
#
# The trailing zero parts are only dropped from behind at least one character (``(?<=.)``): the key of a version is never
# empty, which is what a blank (or whitespace) version has. Without it ``.0`` and ``..0`` reduced to ``''`` and a filter
# for that spelling also matched every row that reports no version (NB-07).
_TRAILING_ZERO_PARTS = re.compile(r"(?<=.)(?:\.0+)+$", re.DOTALL)
_KEY_BLANKS = " \t\n\r\x0c\x0b"           # the characters the SQL key trims too (btrim with the same set)
EQUIVALENCE_KEY_SQL = "regexp_replace(lower(btrim({c}, E' \\t\\n\\r\\f\\x0b')), '(?<=.)(\\.0+)+$', '')"
BLANK_VERSION_SQL = "btrim({c}, E' \\t\\n\\r\\f\\x0b') = ''"


def equivalence_key(raw: object) -> Optional[str]:
    """The grouping key of a version spelling (see ``EQUIVALENCE_KEY_SQL``), or None for a blank or non-text value.

    ``equivalence_key('7.2.0') == equivalence_key('7.2') == '7.2'``; ``'10.0'`` -> ``'10'``; ``'1.10'`` and
    ``'1.100'`` keep their last part (only parts made of zeros are dropped, and never the first character: ``'.0'`` stays
    ``'.0'``, so a non-blank version never has the empty key of a blank one). The SQL twin applies
    ``lower(btrim(x))`` first, exactly as this function does (``BLANK_VERSION_SQL`` is its test for no version).
    """
    if not isinstance(raw, str):
        return None
    s = raw.strip(_KEY_BLANKS).lower()
    if not s:
        return None
    return _TRAILING_ZERO_PARTS.sub("", s)


_NATURAL_DIGIT_RUN = re.compile(r"[0-9]+")        # ASCII only: str.isdigit() / \d accept '²' or '٣' that int() may refuse


def natural_key(value: object) -> tuple:
    """A total sort key for version text: digit runs by value, other text lower-cased (NB-01, NQ-04).

    ``natural_key('7.2') < natural_key('7.10') < natural_key('17.9')``. It is the fallback order where a scheme
    refuses to order two versions (``9.6p1``, ``1.2.3-1``) and the order of the Inventory lists; it is not a claim
    about which release is newer. Unlike an ``int()`` based key it can never raise: digits are the ASCII ``0-9``
    only (a superscript two, a fullwidth or an Arabic-Indic digit is ordinary text), and a digit run is compared by
    its length once leading zeros are gone and then by its text, so a run of any length costs no integer conversion
    (``int()`` refuses runs of more than 4,300 digits). Anything that is not text, and blank text, gives ``()``.
    Every element is a 3-tuple whose first item tells a number (0) from text (1), so keys of any two values compare.
    """
    if not isinstance(value, str) or not value:
        return ()
    parts: list[tuple[int, int, str]] = []
    position = 0
    for match in _NATURAL_DIGIT_RUN.finditer(value):
        if match.start() > position:
            parts.append((1, 0, value[position:match.start()].lower()))
        digits = match.group().lstrip("0")
        parts.append((0, len(digits), digits))
        position = match.end()
    if position < len(value):
        parts.append((1, 0, value[position:].lower()))
    return tuple(parts)


def release_cycle(scheme: Scheme, version: object) -> Optional[str]:
    """Lifecycle cycle for a version (``'7.2.5'`` -> ``'7.2'``), or None."""
    p = _impl(scheme).parse(version)
    return None if p is None else p.cycle


# Keys are lower case with spaces, underscores and hyphens removed.
_FAMILY_SCHEME: dict[str, str] = {
    "winbuild": "win_build",
    "windows": "win_build",
    "semverish": "semverish",
    "fortios": "fortios",
    "panos": "panos",
    "ios": "cisco_ios",
    "ciscoios": "cisco_ios",
    "iosxe": "cisco_iosxe",
    "ciscoiosxe": "cisco_iosxe",
    "asa": "cisco_asa",
    "ciscoasa": "cisco_asa",
    "junos": "junos",
    "junosos": "junos",
    "junosevolved": "junos",
    "junosevo": "junos",
    "dpkg": "dpkg",
    "debian": "dpkg",
    "ubuntu": "dpkg",
    "raspbian": "dpkg",
    "linuxmint": "dpkg",
    "kali": "dpkg",
    "kalilinux": "dpkg",
    "rpm": "rpm",
    "rhel": "rpm",
    "redhat": "rpm",
    "redhatenterpriselinux": "rpm",
    "centos": "rpm",
    "centosstream": "rpm",
    "rocky": "rpm",
    "rockylinux": "rpm",
    "alma": "rpm",
    "almalinux": "rpm",
    "fedora": "rpm",
    "oraclelinux": "rpm",
    "amazonlinux": "rpm",
    "suse": "rpm",
    "sles": "rpm",
    "sled": "rpm",
    "opensuse": "rpm",
    "opensuseleap": "rpm",
    "opensusetumbleweed": "rpm",
}


def scheme_for(os_family_or_product: object) -> Scheme:
    """Scheme for a normaliser ``os_family`` (or OS product) value.

    Unknown, empty and non-string values map to ``semverish``. The generic
    family ``linux`` also maps to ``semverish``: the package scheme (dpkg or
    rpm) depends on the package source, which only the caller knows; a distro
    product such as ``ubuntu`` or ``rhel`` is enough to choose one.
    """
    if not isinstance(os_family_or_product, str):
        return "semverish"
    key = re.sub(r"[\s_-]+", "", os_family_or_product.lower())
    if key.startswith("windows") or key.startswith("microsoftwindows"):
        return "win_build"
    return _FAMILY_SCHEME.get(key, "semverish")  # type: ignore[return-value]
