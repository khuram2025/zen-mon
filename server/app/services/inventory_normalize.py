"""Inventory normalisation engine (design doc 02, section 4).

Pure functions, no I/O after import. Raw collector values go in, normalised
identity keys come out, driven by three JSON rule packs under
``server/app/data/inventory/``:

- ``os_patterns.json``      Windows build table, Linux distributions, network OS
                            families, sysObjectID prefixes, hardware model clean-up
- ``vendor_aliases.json``   vendor and publisher canonicalisation
- ``software_rules.json``   product rules, noise rules and categories

Contract
--------
Every public function is total by default: it never raises, and garbage in
gives the ``unknown`` identity or an ``unmatched`` software key. The
persisting callers (the normalise loop and agent ingest) pass ``strict=True``
instead: an internal failure then propagates so that the row stays dirty and is
retried, rather than being stamped clean with a generic identity that no later
tick would revisit.

Input text is cut to 512 characters before any regular expression runs, which
bounds the cost of any single match. Every pack regex is also linted at load
time: length, no unbounded quantifier inside a repeated group, no repeated
group that is itself repeated around an unbounded quantifier, no alternation
with overlapping or empty branches inside a repeated group. The lint is
best-effort. It catches the classic exponential shapes but is not a proof of
linear time; ``test_inventory_normalize.py`` also times every pattern against
adversarial input. Results are deterministic; ``normalize_software`` and
``canonical_vendor`` are cached because the same titles repeat across
thousands of servers and the packs are immutable after import.

``RULEPACK_REV`` is stored in the ``norm_rev`` columns. Bump it whenever a pack
or the engine logic changes its output, so the normalise loop re-keys rows with
change events suppressed. ``test_inventory_normalize.py`` pins, per revision, a
fingerprint of the packs and a digest of the engine's output over a fixed
corpus (the golden fixtures plus an engine probe set, including version
canonicalisation); it fails when either changes without a bump.

If a pack cannot be loaded (missing from a release, corrupt, wrong-typed or
containing duplicate keys) the module still imports, logs the error, records it
in ``PACK_ERRORS`` and degrades to the generic fallback. Every entry starts with
the name of the pack file. ``packs_healthy()`` is False in that state; a caller
that persists results should refuse to stamp ``norm_rev`` while it is.
``packs_healthy_for(pack)`` answers for one file.

Version canonicalisation goes through ``services/versioning.py`` (``_canon``,
``_cycle`` below); this module only decides which substring is the version.
"""

from __future__ import annotations

import dataclasses
import functools
import hashlib
import itertools
import json
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, NamedTuple, Optional

from app.services import versioning

try:  # the regex parser is only used to lint pack patterns
    from re import _constants as _sre_c, _parser as _sre_p  # Python 3.11+
except ImportError:  # pragma: no cover - older interpreters
    try:
        import sre_constants as _sre_c  # type: ignore[no-redef]
        import sre_parse as _sre_p  # type: ignore[no-redef]
    except ImportError:
        _sre_c = _sre_p = None  # type: ignore[assignment]

logger = logging.getLogger("zenplus.inventory_normalize")

__all__ = [
    "RULEPACK_REV", "CATEGORIES", "MAX_INPUT_LEN", "PACK_ERRORS",
    "Identity", "SoftwareKey",
    "normalize_server", "normalize_device", "normalize_software", "canonical_vendor",
    "packs_healthy", "packs_healthy_for", "pack_fingerprint", "compiled_patterns",
    "PACK_FILES", "PACK_OS", "PACK_VENDORS", "PACK_SOFTWARE",
]

RULEPACK_REV = 1

MAX_INPUT_LEN = 512
_MAX_PATTERN_LEN = 600

CATEGORIES = (
    "browser", "security", "remote-access", "database", "web-server", "runtime",
    "development", "productivity", "monitoring", "backup", "virtualization",
    "networking", "driver", "os-component", "update", "other",
)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data" / "inventory"

# Column widths of inventory_identity (migrate-157) and server_software_inventory.
_CAPS = {
    "vendor_key": 64, "vendor_label": 128, "os_family": 32, "os_product": 128,
    "os_release": 64, "os_version": 128, "os_build": 64, "os_edition": 64,
    "os_source": 16, "hw_vendor": 128, "hw_model": 255, "hw_serial": 128,
    "hw_source": 16, "lifecycle_slug": 64,
}
_NAME_KEY_MAX = 160
_PUBLISHER_KEY_MAX = 96
_PRODUCT_NAME_MAX = 255
_LABEL_MAX = 255

PACK_ERRORS: list[str] = []


# ── Public result types ──────────────────────────────────────────────

@dataclass(frozen=True)
class Identity:
    """Normalised identity of one asset; field names equal inventory_identity columns."""

    vendor_key: Optional[str] = None
    vendor_label: Optional[str] = None
    os_family: str = "unknown"
    os_product: Optional[str] = None
    os_release: Optional[str] = None
    os_version: Optional[str] = None
    os_build: Optional[str] = None
    os_edition: Optional[str] = None
    os_source: Optional[str] = None
    hw_vendor: Optional[str] = None
    hw_model: Optional[str] = None
    hw_serial: Optional[str] = None
    hw_source: Optional[str] = None
    lifecycle_slug: Optional[str] = None  # filled by the lifecycle phase (P4), never here


@dataclass(frozen=True)
class SoftwareKey:
    """Normalised grouping key of one installed title.

    ``status`` is ``normalized`` (a pack rule matched), ``publisher_only`` (no
    product rule, but the publisher is a known alias) or ``unmatched``.
    ``rule_id`` names the rule that fired (None for the generic fallback).
    """

    name_key: str
    publisher_key: Optional[str]
    product_name: str
    publisher_label: Optional[str]
    category: str
    edition: Optional[str]
    cycle: Optional[str]
    status: str
    rule_id: Optional[str] = None


_UNKNOWN = Identity()
_UNNAMED = SoftwareKey(
    name_key="unnamed-software", publisher_key=None, product_name="(unnamed)",
    publisher_label=None, category="other", edition=None, cycle=None, status="unmatched",
)


# ── Version helpers (the only place that talks to versioning.py) ─────

def _canon(scheme: str, raw: str) -> Optional[tuple[str, Optional[str]]]:
    """``(version, build_or_None)`` or None when ``raw`` is not a valid version."""
    return versioning.canonicalize(scheme, raw)  # type: ignore[arg-type]


def _cycle(scheme: str, version: str) -> Optional[str]:
    return versioning.release_cycle(scheme, version)  # type: ignore[arg-type]


# ── Safe regular expressions ─────────────────────────────────────────

_FLAGS = re.IGNORECASE | re.DOTALL | re.ASCII
_REGISTRY: list[tuple[str, "re.Pattern[str]"]] = []
_QUANT_BRACE = re.compile(r"\{(\d{0,5})(,?)(\d{0,5})\}")


def _unbounded_nested(pattern: str) -> bool:
    """True when a group containing an unbounded quantifier is itself repeated
    without an upper bound, the shape behind exponential backtracking
    (``(a+)+``, ``(\\d+\\.?)*``). Bounded repeats such as ``{0,4}`` are fine."""
    stack: list[bool] = []
    closed = False
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "\\":
            i += 2
            closed = False
            continue
        if c == "[":
            i += 1
            if i < n and pattern[i] == "^":
                i += 1
            if i < n and pattern[i] == "]":
                i += 1
            while i < n and pattern[i] != "]":
                i += 2 if pattern[i] == "\\" else 1
            i += 1
            closed = False
            continue
        if c == "(":
            stack.append(False)
            i += 1
            if i < n and pattern[i] == "?":
                if pattern.startswith("?P<", i):
                    end = pattern.find(">", i)
                    i = n if end < 0 else end + 1
                elif pattern.startswith(("?<=", "?<!"), i):
                    i += 3
                else:
                    i += 2  # (?: (?= (?! and the first flag letter
            closed = False
            continue
        if c == ")":
            flag = stack.pop() if stack else False
            if flag and stack:
                stack[-1] = True
            closed = flag
            i += 1
            continue
        unbounded: Optional[bool] = None
        if c in "*+?":
            unbounded = c != "?"
            i += 1
        elif c == "{":
            m = _QUANT_BRACE.match(pattern, i)
            if m and (m.group(1) or m.group(3)):
                unbounded = bool(m.group(2)) and not m.group(3)
                i = m.end()
            else:
                i += 1
                closed = False
                continue
        else:
            i += 1
            closed = False
            continue
        if i < n and pattern[i] in "?+":  # lazy or possessive suffix
            i += 1
        if unbounded:
            if closed:
                return True
            if stack:
                stack[-1] = True
        closed = False
    return False


# The structural lint works on the parsed pattern. Character sets are bitmasks over the 128 ASCII
# code points plus one bit for "any other character"; two sets overlap when their masks intersect.
_ALL_CHARS = (1 << 129) - 1
_NON_ASCII = 1 << 128


def _chars(text: str) -> int:
    mask = 0
    for ch in text:
        mask |= 1 << (ord(ch) if ord(ch) < 128 else 128)
    return mask


_DIGIT_SET = _chars("0123456789")
_SPACE_SET = _chars(" \t\n\r\f\v")
_WORD_SET = _chars("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_SET_BY_CATEGORY = {"DIGIT": _DIGIT_SET, "SPACE": _SPACE_SET, "WORD": _WORD_SET}


class _Shape(NamedTuple):
    nullable: bool    # can match the empty string
    first: int        # characters a match can start with
    last: int         # characters a match can end with
    unbounded: bool   # contains a quantifier without an upper bound


def _code_mask(code: int, icase: bool) -> int:
    if code >= 128:
        return _NON_ASCII
    mask = 1 << code
    if icase and chr(code).isalpha():
        mask |= 1 << ord(chr(code).swapcase())
    return mask


def _category_mask(cat: Any) -> int:
    name = getattr(cat, "name", str(cat))
    negated = "NOT_" in name
    core = _SET_BY_CATEGORY.get(name.replace("CATEGORY_", "").replace("UNI_", "").replace("NOT_", ""))
    if core is None:
        return _ALL_CHARS
    if negated:
        return (~core & _ALL_CHARS) | _NON_ASCII
    return core | (_NON_ASCII if "UNI_" in name else 0)


def _in_mask(items: Any, icase: bool) -> int:
    mask, negate = 0, False
    for op, av in items:
        if op == _sre_c.NEGATE:
            negate = True
        elif op == _sre_c.LITERAL:
            mask |= _code_mask(av, icase)
        elif op == _sre_c.RANGE:
            for code in range(av[0], min(av[1], 127) + 1):
                mask |= _code_mask(code, icase)
            if av[1] >= 128:
                mask |= _NON_ASCII
        elif op == _sre_c.CATEGORY:
            mask |= _category_mask(av)
        else:
            mask |= _ALL_CHARS
    return (~mask & _ALL_CHARS) if negate else mask


def _lint_seq(seq: Any, repeated: bool, icase: bool) -> _Shape:
    shapes = [_lint_node(op, av, repeated, icase) for op, av in seq]
    first = last = 0
    for shape in shapes:  # a match starts with the leading elements up to the first one that cannot be empty
        first |= shape.first
        if not shape.nullable:
            break
    for shape in reversed(shapes):
        last |= shape.last
        if not shape.nullable:
            break
    return _Shape(all(sh.nullable for sh in shapes), first, last, any(sh.unbounded for sh in shapes))


def _literal_text(branch: Any, icase: bool) -> Optional[tuple]:
    """The characters of a branch that is nothing but a literal string, else None."""
    if not all(op == _sre_c.LITERAL for op, _ in branch):
        return None
    return tuple(chr(av).lower() if icase else chr(av) for _, av in branch)


def _lint_branch(branches: list, shapes: list[_Shape], icase: bool) -> None:
    """Alternatives inside a repeated group must be distinguishable at their first character, or be
    distinct literal strings of which none is a prefix of another."""
    for i, left in enumerate(shapes):
        if left.nullable:
            raise ValueError("alternation with an empty branch inside a repeated group")
        for j in range(i):
            if not (left.first & shapes[j].first):
                continue
            a, b = _literal_text(branches[i], icase), _literal_text(branches[j], icase)
            if a is None or b is None or a[: len(b)] == b or b[: len(a)] == a:
                raise ValueError("alternation branches overlap inside a repeated group")


def _lint_node(op: Any, av: Any, repeated: bool, icase: bool) -> _Shape:
    c = _sre_c
    if op == c.LITERAL:
        mask = _code_mask(av, icase)
        return _Shape(False, mask, mask, False)
    if op in (c.NOT_LITERAL, c.ANY):
        return _Shape(False, _ALL_CHARS, _ALL_CHARS, False)
    if op == c.IN:
        mask = _in_mask(av, icase)
        return _Shape(False, mask, mask, False)
    if op == c.AT:
        return _Shape(True, 0, 0, False)
    if op == c.SUBPATTERN:
        return _lint_seq(av[-1], repeated, icase)
    if op == c.BRANCH:
        shapes = [_lint_seq(branch, repeated, icase) for branch in av[1]]
        if repeated:
            _lint_branch(av[1], shapes, icase)
        first = last = 0
        for shape in shapes:
            first |= shape.first
            last |= shape.last
        return _Shape(any(sh.nullable for sh in shapes), first, last, any(sh.unbounded for sh in shapes))
    if op in _REPEAT_OPS:
        low, high, body = av
        shape = _lint_seq(body, repeated or high > 1, icase)
        if high > 1:
            if shape.nullable:
                raise ValueError("repeated group can match the empty string")
            # Each pass must be able to end where the next one cannot begin; otherwise an unbounded
            # quantifier inside can split the same text between passes in exponentially many ways.
            if shape.unbounded and shape.first & shape.last:
                raise ValueError("repeated group contains an unbounded quantifier")
        return _Shape(low == 0 or shape.nullable, shape.first, shape.last, high == c.MAXREPEAT or shape.unbounded)
    if op == getattr(c, "ATOMIC_GROUP", None):
        return _lint_seq(av, repeated, icase)
    if op in (c.ASSERT, c.ASSERT_NOT):
        _lint_seq(av[1], False, icase)
        return _Shape(True, 0, 0, False)
    return _Shape(True, _ALL_CHARS, _ALL_CHARS, False)  # back-references, conditionals: assume anything


_REPEAT_OPS = (
    tuple(getattr(_sre_c, name) for name in ("MAX_REPEAT", "MIN_REPEAT", "POSSESSIVE_REPEAT") if hasattr(_sre_c, name))
    if _sre_c is not None else ()
)


def _backtracking_risk(pattern: str, flags: int = _FLAGS) -> Optional[str]:
    r"""Why the parsed pattern may backtrack badly, or None.

    Rejects a group that is repeated more than once and contains an unbounded
    quantifier when a pass can end with a character the next pass can start with
    (``(a+)+``, and also with a bounded outer repeat as in
    ``(?:\w+\s{0,3}){1,40}``; ``(?:\.\d+){0,3}`` is fine), a repeated group that
    can match nothing, and an alternation inside a repeated group whose branches
    can start with the same character unless they are distinct literals none of
    which prefixes another (``(?:a|aa)+``, ``(?:\d|\d\d)+``). A heuristic lint,
    not a proof of linear time.
    """
    if _sre_p is None:
        return None
    try:
        tree = _sre_p.parse(pattern, flags)
        _lint_seq(tree, False, bool(flags & re.IGNORECASE))
    except ValueError as exc:
        return str(exc)
    except RecursionError:
        return "pattern nesting is too deep"
    except Exception:  # not parseable: re.compile reports the details
        return None
    return None


def _rx(pattern: str, label: str, flags: int = _FLAGS, *, groups: int = 0) -> "re.Pattern[str]":
    """Compile a pattern once, refusing shapes that can backtrack badly.

    ``groups`` is the number of capture groups the caller reads, so a pack
    pattern that lost its group is rejected at load instead of failing later."""
    if len(pattern) > _MAX_PATTERN_LEN:
        raise ValueError(f"pattern longer than {_MAX_PATTERN_LEN} characters")
    if _unbounded_nested(pattern):
        raise ValueError("repeated group contains an unbounded quantifier")
    risk = _backtracking_risk(pattern, flags)
    if risk:
        raise ValueError(risk)
    rx = re.compile(pattern, flags)
    if rx.groups < groups:
        raise ValueError(f"expected {groups} capture group(s), found {rx.groups}")
    _REGISTRY.append((label, rx))
    return rx


_TEMPLATE_REF_RE = re.compile(r"\\(?:(\\)|([1-9][0-9]?)|g<([^>]{1,64})>)", re.ASCII)


def _check_template(rx: "re.Pattern[str]", template: str) -> None:
    """Reject a ``match.expand`` template that refers to a group the pattern does not have."""
    for _escaped_backslash, num, name in _TEMPLATE_REF_RE.findall(template):
        if num or name.isdigit():
            if int(num or name) > rx.groups:
                raise ValueError(f"template {template!r} refers to group {num or name} of {rx.groups}")
        elif name and name not in rx.groupindex:
            raise ValueError(f"template {template!r} refers to unknown group {name!r}")


def compiled_patterns() -> list[tuple[str, "re.Pattern[str]"]]:
    """Every regex this module compiled (engine and packs), for the ReDoS test."""
    return list(_REGISTRY)


# ── Text helpers ─────────────────────────────────────────────────────

_MARKS_RE = _rx(r"[™®]|\((?:r|tm)\)", "marks")
# Control characters (Cc) and the line/paragraph separators separate words. Invisible format characters
# (Cf: soft hyphen, zero-width and joiner characters, bidirectional marks and isolates, word joiner,
# Arabic letter mark, tags, BOM) and lone surrogates (Cs) are removed so that visually identical
# values compare equal. The ranges are explicit so that a Python upgrade cannot change any key.
_CTRL_RE = _rx(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]", "control")
_INVISIBLE_RE = _rx(
    r"[\u00ad\u0600-\u0605\u061c\u06dd\u070f\u0890\u0891\u08e2\u180e\u200b-\u200f\u202a-\u202e"
    r"\u2060-\u2064\u2066-\u206f\ufeff\ufff9-\ufffb\ud800-\udfff\U000110bd\U000110cd"
    r"\U00013430-\U0001343f\U0001bca0-\U0001bca3\U0001d173-\U0001d17a\U000e0001\U000e0020-\U000e007f]",
    "invisible",
)
_SLUG_RE = _rx(r"[^a-z0-9]+", "slug")
_SLUG_MAP = (("++", " plus plus "), ("+", " plus "), ("#", " sharp "), ("&", " and "), ("@", " at "))


def _clean(value: Any, limit: int = MAX_INPUT_LEN) -> Optional[str]:
    """Bounded, whitespace-collapsed text without marks or control characters."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, bytes):
        value = value[:limit].decode("utf-8", "replace")
    if not isinstance(value, str):
        return None
    s = value[:limit].replace("©", " (c) ")
    s = _MARKS_RE.sub("", s)
    s = unicodedata.normalize("NFKC", s)
    s = _INVISIBLE_RE.sub("", s)
    s = _CTRL_RE.sub(" ", s)
    s = " ".join(s.split())[:limit].strip()
    return s or None


def _cap(value: Optional[str], limit: int) -> Optional[str]:
    if value is None:
        return None
    v = value.strip()[:limit].rstrip()
    return v or None


def _slug(text: str, maxlen: int) -> str:
    """Lower-case ASCII key. Characters that ASCII folding cannot express (CJK, Cyrillic, ß ...) would
    otherwise vanish and let different names collide, so a hash of the full text is appended whenever
    any letter or digit was lost. Diacritics fold (é gives e) and never trigger the hash; names that
    are pure ASCII keep exactly the key they always had."""
    decomposed = unicodedata.normalize("NFKD", text)
    t = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    lost = any(c.isalnum() and not c.isascii() for c in decomposed)
    for a, b in _SLUG_MAP:
        t = t.replace(a, b)
    t = _SLUG_RE.sub("-", t).strip("-")
    full = unicodedata.normalize("NFKC", text).strip().lower()
    if not t and full:
        # Non-Latin names still get a stable, distinct key.
        t = "n-" + hashlib.sha1(full.encode("utf-8", "replace")).hexdigest()[:10]
    elif lost:
        suffix = "-" + hashlib.sha1(full.encode("utf-8", "replace")).hexdigest()[:8]
        return t[: maxlen - len(suffix)].rstrip("-") + suffix
    if len(t) > maxlen:
        t = t[: maxlen - 9].rstrip("-") + "-" + hashlib.sha1(t.encode("ascii")).hexdigest()[:8]
    return t


# ── Pack loading ─────────────────────────────────────────────────────

_PACKS: dict[str, dict] = {}

PACK_OS = "os_patterns.json"
PACK_VENDORS = "vendor_aliases.json"
PACK_SOFTWARE = "software_rules.json"
PACK_FILES = (PACK_OS, PACK_VENDORS, PACK_SOFTWARE)


def _reject_duplicate_keys(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate JSON key {key!r}")
        out[key] = value
    return out


def _pack_error(pack: str, msg: str) -> None:
    PACK_ERRORS.append(f"{pack}: {msg}")
    logger.error("inventory rule pack %s: %s", pack, msg)


def _read_pack(name: str) -> Optional[dict]:
    """The parsed pack, or None (error recorded) when it is missing, unreadable, corrupt or has duplicate keys."""
    try:
        with open(_DATA_DIR / name, "r", encoding="utf-8") as fh:
            data = json.load(fh, object_pairs_hook=_reject_duplicate_keys)
        if not isinstance(data, dict):
            raise ValueError("top level is not an object")
    except Exception as exc:  # missing from a release, unreadable, or corrupt
        _pack_error(name, f"not loaded: {exc}")
        return None
    _PACKS[name] = data
    return data


def _typed(node: Any, key: str, kind: type, required: bool = True) -> Any:
    """``node[key]`` as a ``kind`` (list or dict); ValueError when missing (if required) or wrong-typed."""
    if not isinstance(node, dict):
        raise ValueError(f"expected an object, found {type(node).__name__}")
    if key not in node:
        if required:
            raise ValueError(f"{key!r} is missing")
        return kind()
    value = node[key]
    if not isinstance(value, kind):
        raise ValueError(f"{key!r} must be a {kind.__name__}, not {type(value).__name__}")
    return value


def _text(node: Any, key: str, required: bool = True) -> Optional[str]:
    """``node[key]`` as a non-empty string; None when optional and absent or null."""
    if not isinstance(node, dict):
        raise ValueError(f"expected an object, found {type(node).__name__}")
    value = node.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key!r} is missing")
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key!r} must be a non-empty string")
    return value


def _int(node: Any, key: str) -> int:
    value = node.get(key) if isinstance(node, dict) else None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key!r} must be an integer")
    return value


def _strings(node: Any, key: str, required: bool = False) -> list[str]:
    values = _typed(node, key, list, required)
    for v in values:
        if not isinstance(v, str):
            raise ValueError(f"{key!r} must hold only strings")
    return values


def _section(pack: str, node: Any, key: str, kind: type, where: str = "", required: bool = True) -> Any:
    """Like ``_typed`` for a whole container: the error is recorded in PACK_ERRORS and an empty container returned."""
    try:
        return _typed(node, key, kind, required)
    except ValueError as exc:
        _pack_error(pack, f"{where + ' ' if where else ''}{exc}")
        return kind()


def _ident(item: Any, index: int) -> str:
    """Label for an item in an error message; never fails on a wrong-typed item."""
    if isinstance(item, dict) and isinstance(item.get("id"), str):
        return str(item["id"])[:60]
    return f"#{index}"


def _guard(pack: str, name: str, loader: Any, data: Optional[dict]) -> None:
    """Run one loader; a failure it did not anticipate is recorded and never stops the import."""
    if data is None:
        return
    try:
        loader(data)
    except Exception as exc:
        _pack_error(pack, f"{name} loader failed: {exc!r}")


def packs_healthy() -> bool:
    """False when any pack failed to load or a rule was rejected."""
    return not PACK_ERRORS


def packs_healthy_for(pack: str) -> bool:
    """``packs_healthy`` for one pack file (``software_rules.json`` ...): errors are recorded as ``"<file>: ..."``."""
    prefix = pack + ":"
    return not any(e.startswith(prefix) for e in PACK_ERRORS)


def _behavioural(node: Any) -> Any:
    """The pack without documentation-only keys, so editing prose never forces a revision bump."""
    if isinstance(node, dict):
        return {k: _behavioural(v) for k, v in node.items() if k not in ("description", "examples")}
    if isinstance(node, list):
        return [_behavioural(v) for v in node]
    return node


def pack_fingerprint() -> str:
    """Stable digest of the parsed packs: formatting, descriptions and examples do not alter it."""
    h = hashlib.sha256()
    for name in sorted(_PACKS):
        h.update(name.encode("utf-8"))
        h.update(json.dumps(_behavioural(_PACKS[name]), sort_keys=True, separators=(",", ":"),
                            ensure_ascii=True).encode("ascii"))
    return h.hexdigest()


# ── Vendors ──────────────────────────────────────────────────────────

_VENDOR_BY_KEY: dict[str, str] = {}
_ALIAS_PREFIX: dict[str, str] = {}
_ALIAS_EXACT: dict[str, str] = {}
_ENTERPRISE: dict[str, str] = {}
_VENDOR_DEFAULT_FAMILY: dict[str, str] = {}
_MAX_ALIAS_TOKENS = 1
_ALIAS_TOKEN_RE = _rx(r"[a-z0-9]+", "alias-token")
_CORP_SUFFIX_RE = _rx(
    r"[\s,.]{1,3}(?:inc|incorporated|llc|ltd|limited|corp|corporation|co|company|gmbh|ag|sa|plc|lp|llp"
    r"|pte|pty|bv|nv|oy|ab|srl|spa|kk|ltda|sdn|bhd|sro|sas)\.?\s{0,2}$",
    "corp-suffix",
)
_BUILTIN_PLACEHOLDERS: frozenset[str] = frozenset(
    {"unknown", "n a", "na", "none", "null", "nil", "undefined", "not specified", "not available",
     "not applicable", "default string", "oem", "o e m", "other", "generic", "tbd"}
)
_PLACEHOLDERS: frozenset[str] = _BUILTIN_PLACEHOLDERS


def _tokens(text: str) -> list[str]:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return _ALIAS_TOKEN_RE.findall(folded)


def _load_vendors(pack: dict) -> None:
    global _MAX_ALIAS_TOKENS
    for i, v in enumerate(_section(PACK_VENDORS, pack, "vendors", list)):
        try:
            key, label = _text(v, "key"), _text(v, "label")
            if key in _VENDOR_BY_KEY:
                raise ValueError(f"duplicate vendor key {key}")
            aliases, exact = _strings(v, "aliases"), _strings(v, "exact_aliases")
            _VENDOR_BY_KEY[key] = label
            for alias in aliases + [label]:
                toks = _tokens(alias)
                if not toks:
                    continue
                joined = " ".join(toks)
                owner = _ALIAS_PREFIX.setdefault(joined, key)
                if owner != key:
                    raise ValueError(f"alias {alias!r} claimed by {owner} and {key}")
                _MAX_ALIAS_TOKENS = max(_MAX_ALIAS_TOKENS, len(toks))
            for alias in exact:
                joined = " ".join(_tokens(alias))
                if joined:
                    owner = _ALIAS_EXACT.setdefault(joined, key)
                    if owner != key:
                        raise ValueError(f"exact alias {alias!r} claimed by {owner} and {key}")
        except Exception as exc:
            _pack_error(PACK_VENDORS, f"vendor {v.get('key') if isinstance(v, dict) else '#' + str(i)}: {exc}")
    for num, key in _section(PACK_VENDORS, pack, "enterprises", dict).items():
        if isinstance(key, str) and key in _VENDOR_BY_KEY and str(num).isdigit():
            _ENTERPRISE[str(num)] = key
        else:
            _pack_error(PACK_VENDORS, f"enterprise {num}: unknown vendor {key!r}")
    for vendor, family in _section(PACK_VENDORS, pack, "vendor_default_family", dict).items():
        if vendor in _VENDOR_BY_KEY and isinstance(family, str):
            _VENDOR_DEFAULT_FAMILY[vendor] = family
        else:
            _pack_error(PACK_VENDORS, f"vendor_default_family {vendor}: unknown vendor or family {family!r}")


@functools.lru_cache(maxsize=4096)
def _vendor_cached(raw: str) -> tuple[Optional[str], Optional[str], bool]:
    """``(key, label, known)``; known is True when an alias matched."""
    toks = _tokens(raw)
    if toks:
        joined = " ".join(toks)
        if joined in _PLACEHOLDERS:
            return None, None, False
        hit = _ALIAS_EXACT.get(joined)
        if hit is None:
            for k in range(min(len(toks), _MAX_ALIAS_TOKENS), 0, -1):
                hit = _ALIAS_PREFIX.get(" ".join(toks[:k]))
                if hit is not None:
                    break
        if hit is not None:
            return hit, _VENDOR_BY_KEY[hit], True
    # Unknown vendor: keep it distinct instead of dropping it. Strip corporate suffixes
    # from the original spelling for the label and slug the result for the key.
    label = raw.strip(" ,.;")
    for _ in range(4):
        stripped = _CORP_SUFFIX_RE.sub("", label)
        if stripped == label or not stripped:
            break
        label = stripped
    label = label.strip(" ,.;")
    key = _slug(label, _CAPS["vendor_key"]) if label else ""
    if not key:
        return None, None, False
    return key, label[: _CAPS["vendor_label"]], False


def canonical_vendor(raw: Any, *, strict: bool = False) -> tuple[Optional[str], Optional[str]]:
    """Canonical ``(key, label)`` for a vendor or publisher string, or ``(None, None)``.

    Known aliases map to one key ("Google LLC" and "Google Inc." both give
    ``google``). An unrecognised but non-empty value keeps its own slug key and a
    suffix-stripped label. Placeholders ("Unknown", "To Be Filled By O.E.M.")
    and non-text input give ``(None, None)``. Never raises unless ``strict``: then an
    internal failure propagates instead of giving ``(None, None)``.
    """
    try:
        s = _clean(raw)
        if s is None:
            return None, None
        key, label, _known = _vendor_cached(s)
        return key, label
    except Exception as exc:
        if strict:
            raise
        logger.warning("canonical_vendor failed: %r", exc)
        return None, None


def _is_placeholder(text: str) -> bool:
    toks = " ".join(_tokens(text))
    if toks in _PLACEHOLDERS:
        return True
    low = text.lower()
    return len(low) >= 4 and len(set(low)) == 1  # 0000000, FFFFFFFF


# ── Windows ──────────────────────────────────────────────────────────

class _Windows:
    builds: dict[int, dict] = {}
    client_floor: list[tuple[int, str]] = []
    server_year_rx: "re.Pattern[str]"
    client_name_rx: "re.Pattern[str]"
    client_release_rx: "re.Pattern[str]"
    edition_prefix_rx: "re.Pattern[str]"
    version_rx: "re.Pattern[str]"
    build_rx: "re.Pattern[str]"
    ok = False


_SERVER_WORD_RE = _rx(r"\bserver\b", "win-server-word")
_WINDOWS_WORD_RE = _rx(r"\bwindows\b", "win-word")
# gopsutil appends CSDVersion to the product name ("Windows 7 Professional Service Pack 1").
_SERVICE_PACK_RE = _rx(r"[\s,.-]{0,3}\bservice\s{1,3}pack\s{1,3}(\d{1,2})\b", "win-service-pack")


def _load_windows(pack: dict) -> None:
    w = _section(PACK_OS, pack, "windows", dict)
    try:
        builds: dict[int, dict] = {}
        for i, b in enumerate(_typed(w, "builds", list)):
            if not isinstance(b, dict):
                raise ValueError(f"builds[{i}] is not an object")
            number = _int(b, "build")
            if number in builds:
                raise ValueError(f"duplicate build {number}")
            for side in ("client", "server"):
                if side in b:
                    _text(b[side], "release", required=side == "client")
                    _text(b[side], "product", required=side == "client")
            builds[number] = b
        floor = []
        for i, f in enumerate(_typed(w, "client_floor", list)):
            floor.append((_int(f, "min_build"), _text(f, "product")))
        server_year = _rx(_text(w, "server_year_re"), "win-server-year", groups=1)
        client_name = _rx(_text(w, "client_name_re"), "win-client-name", groups=1)
        client_release = _rx(_text(w, "client_release_re"), "win-client-release")
        edition_prefix = _rx(_text(w, "edition_prefix_re"), "win-edition-prefix")
        version = _rx(_text(w, "version_re"), "win-version", groups=1)
        build = _rx(_text(w, "build_re"), "win-build", groups=1)
        _Windows.builds = builds
        _Windows.client_floor = sorted(floor, reverse=True)
        _Windows.server_year_rx, _Windows.client_name_rx = server_year, client_name
        _Windows.client_release_rx, _Windows.edition_prefix_rx = client_release, edition_prefix
        _Windows.version_rx, _Windows.build_rx = version, build
        _Windows.ok = True
    except Exception as exc:
        _pack_error(PACK_OS, f"windows: {exc}")


def _windows_identity(name: str, os_version: Optional[str], kernel: Optional[str]) -> dict:
    W = _Windows
    is_server = bool(_SERVER_WORD_RE.search(name))
    version = build = None
    for cand in (kernel, os_version):
        if not cand:
            continue
        m = W.version_rx.search(cand)
        canon = _canon("win_build", m.group(1)) if m else None
        if canon is None:
            continue
        version = canon[0]
        bm = W.build_rx.search(cand)
        build = bm.group(1) if bm else canon[1]
        break
    parts = version.split(".") if version else []
    bnum = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else None
    if build is None and bnum is not None:
        build = parts[2] + ("." + parts[3] if len(parts) > 3 else "")

    product: Optional[str] = None
    release: Optional[str] = None
    entry = W.builds.get(bnum) if bnum is not None else None
    if entry is not None:
        side = entry.get("server" if is_server else "client")
        if side:
            release = side.get("release")
            product = "Windows Server" if is_server else side.get("product")
    if product is None and bnum is not None and not is_server:
        for floor, prod in W.client_floor:
            if bnum >= floor:
                product = prod
                break
    # The marketing string is only a fallback when the build is missing or not in the table.
    if product is None:
        if is_server:
            product = "Windows Server"
        else:
            cm = W.client_name_rx.search(name)
            product = f"Windows {cm.group(1)}" if cm else "Windows"
    sp = _SERVICE_PACK_RE.search(name)
    if release is None:
        if is_server:
            ym = W.server_year_rx.search(name)
            release = ym.group(1) if ym else None
        elif os_version and W.client_release_rx.match(os_version):
            release = os_version.upper()
        elif sp:
            release = f"SP{sp.group(1)}"

    em = W.edition_prefix_rx.match(name)
    edition = name[em.end():].strip() if em else None
    if edition:
        edition = _SERVICE_PACK_RE.sub("", edition).strip()
        edition = _EDITION_WORD_RE.sub("", edition).strip(" -,") or None
    return {
        "os_family": "windows", "os_product": product, "os_release": release,
        "os_version": version, "os_build": build, "os_edition": edition,
    }


# ── Linux and other server operating systems ─────────────────────────

@dataclass(frozen=True)
class _Distro:
    id: str
    product: str
    family: str
    match_rx: "re.Pattern[str]"
    version_rx: Optional["re.Pattern[str]"]
    sp_rx: Optional["re.Pattern[str]"]
    edition_rx: Optional["re.Pattern[str]"]
    release: str
    os_version_rx: Optional["re.Pattern[str]"] = None  # the entry applies only to os_versions that match


_DISTROS: list[_Distro] = []
_GENERIC_LINUX_RX: Optional["re.Pattern[str]"] = None
_DISTRO_RELEASE_MODES = ("none", "major", "major_minor", "macos")
# Up to four numeric parts, each up to four digits: CentOS reports 7.9.2009.
_DOTTED_RE = _rx(r"^\d{1,4}(?:\.\d{1,4}){0,3}$", "dotted-version")


def _load_distros(pack: dict) -> None:
    global _GENERIC_LINUX_RX
    linux = _section(PACK_OS, pack, "linux", dict)
    entries = [("linux", d) for d in _section(PACK_OS, linux, "distros", list, "linux")]
    systems = _section(PACK_OS, _section(PACK_OS, pack, "other_os", dict), "systems", list, "other_os")
    entries += [(d.get("family", "other") if isinstance(d, dict) else "other", d) for d in systems]
    seen: set[str] = set()
    for i, (family, d) in enumerate(entries):
        try:
            did = _text(d, "id")
            if did in seen:
                raise ValueError("duplicate distribution id")
            if not isinstance(family, str) or not family:
                raise ValueError("'family' must be a non-empty string")
            release = d.get("release", "none")
            if release not in _DISTRO_RELEASE_MODES:
                raise ValueError(f"unknown release mode {release!r}")
            version_re, sp_re, edition_re, os_version_re = (
                _text(d, "version_re", required=False), _text(d, "sp_re", required=False),
                _text(d, "edition_re", required=False), _text(d, "os_version_re", required=False),
            )
            _DISTROS.append(_Distro(
                id=did, product=_text(d, "product"), family=family,
                match_rx=_rx(_text(d, "match"), f"distro-{did}-match"),
                version_rx=_rx(version_re, f"distro-{did}-version", groups=1) if version_re else None,
                sp_rx=_rx(sp_re, f"distro-{did}-sp", groups=1) if sp_re else None,
                edition_rx=_rx(edition_re, f"distro-{did}-edition", groups=1) if edition_re else None,
                release=release,
                os_version_rx=_rx(os_version_re, f"distro-{did}-os-version") if os_version_re else None,
            ))
            seen.add(did)
        except Exception as exc:
            _pack_error(PACK_OS, f"distro {_ident(d, i)}: {exc}")
    try:
        gen = _text(linux, "generic_re", required=False)
        _GENERIC_LINUX_RX = _rx(gen, "linux-generic") if gen else None
    except Exception as exc:
        _pack_error(PACK_OS, f"linux generic_re: {exc}")


def _release_of(version: Optional[str], mode: str) -> Optional[str]:
    if not version or mode == "none":
        return None
    parts = version.split(".")
    if mode == "major":
        return parts[0]
    if mode == "major_minor":
        return ".".join(parts[:2]) if len(parts) > 1 else parts[0]
    if mode == "macos":  # 10.15.7 -> 10.15, 14.4.1 -> 14
        return ".".join(parts[:2]) if parts[0] == "10" and len(parts) > 1 else parts[0]
    return None


def _distro_identity(name: str, os_version: Optional[str]) -> Optional[dict]:
    for d in _DISTROS:
        if not d.match_rx.search(name):
            continue
        if d.os_version_rx is not None and not (os_version and d.os_version_rx.search(os_version)):
            continue
        version: Optional[str] = None
        if d.version_rx is not None:
            m = d.version_rx.search(name)
            version = m.group(1) if m else None
        if os_version and _DOTTED_RE.match(os_version):
            # os_version (VERSION_ID) is more exact when it extends what the name says.
            if version is None or (os_version != version and os_version.startswith(version + ".")):
                version = os_version
        if version and d.sp_rx is not None:
            sp = d.sp_rx.search(name)
            if sp:
                version = f"{version}.{sp.group(1)}"
        edition = None
        if d.edition_rx is not None:
            em = d.edition_rx.search(name)
            edition = em.group(1) if em else None
        return {
            "os_family": d.family, "os_product": d.product,
            "os_release": _release_of(version, d.release),
            "os_version": version, "os_build": None, "os_edition": edition,
        }
    if _GENERIC_LINUX_RX is not None and _GENERIC_LINUX_RX.search(name):
        version = os_version if os_version and _DOTTED_RE.match(os_version) else None
        return {
            "os_family": "linux", "os_product": name[:_CAPS["os_product"]], "os_release": None,
            "os_version": version, "os_build": None, "os_edition": None,
        }
    return None


# ── Hardware ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _ModelRule:
    vendor: str
    rx: "re.Pattern[str]"
    template: str
    underscore_to_hyphen: bool


_MODEL_RULES: list[_ModelRule] = []


def _load_hardware(pack: dict) -> None:
    global _PLACEHOLDERS
    hw = _section(PACK_OS, pack, "hardware", dict)
    for i, r in enumerate(_section(PACK_OS, hw, "model_rules", list, "hardware")):
        try:
            rx = _rx(_text(r, "pattern"), f"model-rule-{i}")
            template = _text(r, "template")
            _check_template(rx, template)
            _MODEL_RULES.append(_ModelRule(
                vendor=_text(r, "vendor"), rx=rx, template=template,
                underscore_to_hyphen=bool(r.get("underscore_to_hyphen")),
            ))
        except Exception as exc:
            _pack_error(PACK_OS, f"hardware model_rules[{i}]: {exc}")
    extra: set[str] = set()
    for v in _section(PACK_OS, hw, "placeholder_values", list, "hardware"):
        if not isinstance(v, str):
            _pack_error(PACK_OS, f"hardware placeholder_values must hold only strings, found {type(v).__name__}")
        elif _tokens(v):
            extra.add(" ".join(_tokens(v)))
    _PLACEHOLDERS = _BUILTIN_PLACEHOLDERS | frozenset(extra)


def _clean_model(vendor_key: Optional[str], model: Any) -> Optional[str]:
    m = _clean(model, _CAPS["hw_model"])
    if m is None or _is_placeholder(m):
        return None
    if vendor_key:
        for rule in _MODEL_RULES:
            if rule.vendor != vendor_key:
                continue
            hit = rule.rx.match(m)
            if hit:
                out = hit.expand(rule.template)
                return out.replace("_", "-") if rule.underscore_to_hyphen else out
    return m


def _clean_serial(serial: Any) -> Optional[str]:
    s = _clean(serial, _CAPS["hw_serial"])
    if s is None or _is_placeholder(s):
        return None
    return s


# ── Network OS ───────────────────────────────────────────────────────

@dataclass(frozen=True)
class _NetFamily:
    key: str
    vendor: Optional[str]
    product: str
    scheme: Optional[str]
    version_rx: "re.Pattern[str]"
    descr_rx: Optional["re.Pattern[str]"]
    release_rx: Optional["re.Pattern[str]"]
    template_keys: tuple[str, ...]


class _Net:
    families: dict[str, _NetFamily] = {}
    profile_rules: list[tuple["re.Pattern[str]", str]] = []
    oid_rules: list[tuple[str, str]] = []
    descr_rules: list[tuple["re.Pattern[str]", str]] = []
    model_rules: list[tuple["re.Pattern[str]", str, Optional[str]]] = []
    shape_rules: list[tuple["re.Pattern[str]", str]] = []
    template_family: dict[str, str] = {}
    template_serial_keys: tuple[str, ...] = ()
    cisco_ios_rx: Optional["re.Pattern[str]"] = None
    cisco_ios_majors: frozenset[int] = frozenset()
    cisco_xe_rx: Optional["re.Pattern[str]"] = None
    cisco_xe_majors: frozenset[int] = frozenset()
    cisco_descr_rx: Optional["re.Pattern[str]"] = None


# A sysObjectID prefix mapped to this marker is positively not one of the pack's network OS families
# (ClearPass, ScreenOS, F5OS ...): it stops the search so that no broader prefix or vendor default applies.
_NO_FAMILY = "none"
_OID_RE = _rx(r"^[0-9]{1,10}(?:\.[0-9]{1,10}){0,40}$", "oid")
_MAJOR_MINOR_RE = _rx(r"(\d{1,2})\.\d{1,2}", "major-minor")
_HAS_DIGIT_RE = _rx(r"\d", "has-digit")
_TRAILING_PUNCT = " ,;"


def _load_network(pack: dict) -> None:
    net = _section(PACK_OS, pack, "network", dict)
    for key, f in _section(PACK_OS, net, "families", dict, "network").items():
        try:
            product, scheme, vendor = _text(f, "product"), _text(f, "scheme", False), _text(f, "vendor", False)
            if scheme is not None and scheme not in versioning.SCHEMES:
                raise ValueError(f"unknown version scheme {scheme}")
            if vendor is not None and vendor not in _VENDOR_BY_KEY:
                raise ValueError(f"unknown vendor {vendor}")
            descr_re, release_re = _text(f, "descr_version_re", False), _text(f, "release_re", False)
            _Net.families[key] = _NetFamily(
                key=key, vendor=vendor, product=product, scheme=scheme,
                version_rx=_rx(_text(f, "version_re"), f"net-{key}-version"),
                descr_rx=_rx(descr_re, f"net-{key}-descr", groups=1) if descr_re else None,
                release_rx=_rx(release_re, f"net-{key}-release", groups=1) if release_re else None,
                template_keys=tuple(_strings(f, "template_keys")),
            )
        except Exception as exc:
            _pack_error(PACK_OS, f"network family {key}: {exc}")

    def _family_ok(fam: Any, allow_none: bool = False) -> bool:
        return isinstance(fam, str) and (fam in _Net.families or fam == "?cisco" or (allow_none and fam == _NO_FAMILY))

    for kind, target, label in (("profile_rules", _Net.profile_rules, "net-profile"),
                                ("sys_descr_rules", _Net.descr_rules, "net-descr"),
                                ("version_shapes", _Net.shape_rules, "net-shape")):
        for i, r in enumerate(_section(PACK_OS, net, kind, list, "network")):
            try:
                fam = _text(r, "family")
                if not _family_ok(fam):
                    raise ValueError(f"unknown family {fam}")
                target.append((_rx(_text(r, "pattern"), f"{label}-{i}"), fam))
            except Exception as exc:
                _pack_error(PACK_OS, f"network {kind}[{i}]: {exc}")
    for i, r in enumerate(_section(PACK_OS, net, "model_rules", list, "network")):
        try:
            fam = _text(r, "family")
            if not _family_ok(fam):
                raise ValueError(f"unknown family {fam}")
            _Net.model_rules.append((_rx(_text(r, "pattern"), f"net-model-{i}"), fam, _text(r, "vendor", False)))
        except Exception as exc:
            _pack_error(PACK_OS, f"network model_rules[{i}]: {exc}")
    rules: list[tuple[str, str]] = []
    for i, r in enumerate(_section(PACK_OS, net, "sys_object_id_prefixes", list, "network")):
        try:
            prefix, fam = _text(r, "prefix"), _text(r, "family")
            if not _family_ok(fam, allow_none=True) or not _OID_RE.match(prefix):
                raise ValueError("invalid prefix or unknown family")
            if any(prefix == known for known, _ in rules):
                raise ValueError(f"duplicate prefix {prefix}")
            rules.append((prefix, fam))
        except Exception as exc:
            _pack_error(PACK_OS, f"network sys_object_id_prefixes[{i}]: {exc}")
    _Net.oid_rules = sorted(rules, key=lambda t: len(t[0]), reverse=True)
    for tkey, fam in _section(PACK_OS, net, "template_family_keys", dict, "network").items():
        if isinstance(fam, str) and fam in _Net.families:
            _Net.template_family[tkey] = fam
        else:
            _pack_error(PACK_OS, f"network template_family_keys {tkey}: unknown family {fam!r}")
    try:
        _Net.template_serial_keys = tuple(_strings(net, "template_serial_keys", True))
    except Exception as exc:
        _pack_error(PACK_OS, f"network template_serial_keys: {exc}")
    try:
        cr = _typed(net, "cisco_resolve", dict)
        ios_majors = frozenset(_int({"v": x}, "v") for x in _typed(cr, "ios_majors", list))
        xe_majors = frozenset(_int({"v": x}, "v") for x in _typed(cr, "iosxe_majors", list))
        _Net.cisco_ios_rx = _rx(_text(cr, "ios_re"), "net-cisco-ios")
        _Net.cisco_xe_rx = _rx(_text(cr, "iosxe_re"), "net-cisco-xe")
        _Net.cisco_descr_rx = _rx(_text(cr, "descr_version_re"), "net-cisco-descr", groups=1)
        _Net.cisco_ios_majors, _Net.cisco_xe_majors = ios_majors, xe_majors
    except Exception as exc:
        _pack_error(PACK_OS, f"network cisco_resolve: {exc}")
    for vendor, fam in list(_VENDOR_DEFAULT_FAMILY.items()):
        if not _family_ok(fam):
            _pack_error(PACK_VENDORS, f"vendor_default_family {vendor}: unknown family {fam}")
            del _VENDOR_DEFAULT_FAMILY[vendor]


def _norm_oid(raw: Any) -> Optional[str]:
    s = _clean(raw, 256)
    if not s:
        return None
    s = s.strip().lstrip(".")
    if s.lower().startswith("iso."):
        s = "1." + s[4:]
    return s if _OID_RE.match(s) else None


def _oid_family(oid: str) -> Optional[str]:
    for prefix, fam in _Net.oid_rules:
        if oid == prefix or oid.startswith(prefix + "."):
            return fam
    return None


def _oid_enterprise(oid: str) -> Optional[str]:
    parts = oid.split(".")
    if len(parts) > 6 and parts[:6] == ["1", "3", "6", "1", "4", "1"]:
        return _ENTERPRISE.get(parts[6])
    return None


def _cisco_from_shape(texts: list[str]) -> Optional[str]:
    for t in texts:
        m = _MAJOR_MINOR_RE.match(t)
        if not m:
            continue
        major = int(m.group(1))
        if _Net.cisco_ios_rx is not None and _Net.cisco_ios_rx.match(t) and major in _Net.cisco_ios_majors:
            return "ios"
        if _Net.cisco_xe_rx is not None and _Net.cisco_xe_rx.match(t) and major in _Net.cisco_xe_majors:
            return "ios-xe"
    return None


def _first_rule(rules: list[tuple["re.Pattern[str]", str]], text: str) -> Optional[str]:
    for rx, fam in rules:
        if rx.search(text):
            return fam
    return None


def _detect_family(profile: Optional[str], oid: Optional[str], descr: Optional[str], model: Optional[str],
                   vendor_key: Optional[str], version_texts: list[str],
                   cisco_texts: list[str]) -> tuple[Optional[str], bool]:
    """``(family, ambiguous)`` from sysObjectID prefix, sysDescr, profile name, model, vendor, version shape.

    The device's own words (sysObjectID, then sysDescr) outrank the profile name: the profile is operator
    text, so it only seeds the family when the device does not name a different one. ``ambiguous`` is True
    when the evidence says "some Cisco OS" and nothing narrowed it down. ``version_texts`` are every version
    string the caller has (template values included); ``cisco_texts`` only the ones the device itself
    reported (profile extract, chassis firmware, the 'Version x' token of sysDescr), which is what decides
    between the Cisco trains."""
    ambiguous = False

    hard: Optional[str] = None
    for fam in (_oid_family(oid) if oid else None, _first_rule(_Net.descr_rules, descr) if descr else None):
        if fam == "?cisco":
            ambiguous = True
        elif fam is not None:
            hard = fam
            break
    if hard == _NO_FAMILY:
        return None, False

    chosen = _first_rule(_Net.profile_rules, profile) if profile else None
    if chosen == "?cisco":
        ambiguous = True
    elif chosen is not None:
        if hard is not None and hard != chosen:
            return hard, False
        if hard is None and chosen in ("ios", "ios-xe"):
            # Neither sysObjectID nor sysDescr decided between the two Cisco trains; the version format does.
            shape = _cisco_from_shape(cisco_texts)
            if shape is not None and shape != chosen:
                return shape, False
        return chosen, False
    if hard is not None:
        return hard, False

    if model:
        for rx, fam, vendor in _Net.model_rules:
            if (vendor is None or vendor == vendor_key) and rx.search(model):
                if fam == "?cisco":
                    ambiguous = True
                    break
                return fam, False
    if vendor_key:
        fam = _VENDOR_DEFAULT_FAMILY.get(vendor_key)
        if fam == "?cisco":
            ambiguous = True
        elif fam:
            return fam, False
    if ambiguous:
        # The device says "some Cisco OS": a stray template value must not turn it into another vendor's OS.
        resolved = _cisco_from_shape(cisco_texts)
        return resolved, resolved is None
    for t in version_texts:
        for rx, fam in _Net.shape_rules:
            if rx.search(t):
                return fam, False
    return None, False


def _parse_net_version(fam: _NetFamily, raw: str) -> Optional[tuple[str, Optional[str], Optional[str]]]:
    """``(version, build, release)`` from a raw version string for one family."""
    text = raw.strip(_TRAILING_PUNCT)
    if fam.scheme:
        canon = _canon(fam.scheme, text)
        if canon:
            return canon[0], canon[1], _cycle(fam.scheme, canon[0])
    m = fam.version_rx.search(text)
    if not m:
        return None
    token = m.group(0)
    if fam.scheme:
        canon = _canon(fam.scheme, token)
        if canon:
            return canon[0], canon[1], _cycle(fam.scheme, canon[0])
    release = None
    if fam.release_rx is not None:
        rm = fam.release_rx.search(token)
        release = rm.group(1) if rm else None
    return token, None, release


def _descr_version(fam: _NetFamily, descr: Optional[str]) -> Optional[str]:
    if not descr or fam.descr_rx is None:
        return None
    m = fam.descr_rx.search(descr)
    return m.group(1) if m else None


# ── normalize_server ─────────────────────────────────────────────────

def _normalize_server(os_name: Any, os_version: Any, kernel_or_build: Any, architecture: Any, cpu_model: Any,
                      hw_manufacturer: Any, hw_model: Any, hw_serial: Any, strict: bool) -> Identity:
    name = _clean(os_name, 256)
    osv = _clean(os_version, 128)
    kernel = _clean(kernel_or_build, 128)
    os_fields: dict = {}
    if name:
        if _WINDOWS_WORD_RE.search(name) and _Windows.ok:
            os_fields = _windows_identity(name, osv, kernel)
        else:
            os_fields = _distro_identity(name, osv) or {
                "os_family": "other", "os_product": name[:_CAPS["os_product"]], "os_release": None,
                "os_version": osv if osv and _DOTTED_RE.match(osv) else None, "os_build": None, "os_edition": None,
            }
    vendor_key, vendor_label = canonical_vendor(hw_manufacturer, strict=strict)
    model = _clean_model(vendor_key, hw_model)
    serial = _clean_serial(hw_serial)
    hw_present = bool(vendor_key or model or serial)
    return _identity(
        vendor_key=vendor_key, vendor_label=vendor_label,
        os_family=os_fields.get("os_family", "unknown"),
        os_product=os_fields.get("os_product"), os_release=os_fields.get("os_release"),
        os_version=os_fields.get("os_version"), os_build=os_fields.get("os_build"),
        os_edition=os_fields.get("os_edition"), os_source="agent" if name else None,
        hw_vendor=vendor_label, hw_model=model, hw_serial=serial, hw_source="agent" if hw_present else None,
    )


def _identity(**fields: Any) -> Identity:
    """Identity with every value cut to its column width."""
    out: dict[str, Any] = {}
    for k, v in fields.items():
        out[k] = (v or "unknown")[: _CAPS["os_family"]] if k == "os_family" else _cap(v, _CAPS[k])
    return Identity(**out)


def normalize_server(os_name: Any = None, os_version: Any = None, kernel_or_build: Any = None,
                     architecture: Any = None, *, cpu_model: Any = None, hw_manufacturer: Any = None,
                     hw_model: Any = None, hw_serial: Any = None, strict: bool = False) -> Identity:
    """Identity of a server from agent-reported strings.

    The Windows release comes from the build number, not the marketing string:
    ``('Microsoft Windows Server 2022 Standard Evaluation', '21H2',
    '10.0.20348.587 Build 20348.587', 'x86_64')`` gives product ``Windows
    Server``, release ``2022``, edition ``Standard Evaluation``, version
    ``10.0.20348.587`` and build ``20348.587``. ``vendor_key`` and the ``hw_*``
    fields describe the hardware maker and stay empty until the agent reports
    them. ``architecture`` and ``cpu_model`` are accepted for interface
    stability and do not change the identity today. Never raises unless
    ``strict``: then an internal failure propagates instead of giving the
    ``unknown`` identity, so a caller that persists the result can retry the row.
    """
    try:
        return _normalize_server(os_name, os_version, kernel_or_build, architecture, cpu_model,
                                 hw_manufacturer, hw_model, hw_serial, strict)
    except Exception as exc:
        if strict:
            raise
        logger.warning("normalize_server failed: %r", exc)
        return _UNKNOWN


# ── normalize_device ─────────────────────────────────────────────────

def _clean_templates(values: Optional[Mapping[str, Any]]) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(values, Mapping):
        for k, v in itertools.islice(values.items(), 64):
            cv = _clean(v, 128)
            if isinstance(k, str) and cv:
                out[k[:64]] = cv
    return out


def _normalize_device(vendor: Any, model: Any, os_version: Any, firmware_version: Any, serial_number: Any,
                      sys_object_id: Any, sys_descr: Any, profile_name: Any,
                      template_values: Optional[Mapping[str, Any]],
                      stale_template_values: Optional[Mapping[str, Any]], strict: bool) -> Identity:
    profile = _clean(profile_name, 256)
    oid = _norm_oid(sys_object_id)
    descr = _clean(sys_descr)
    osv = _clean(os_version, 128)
    fwv = _clean(firmware_version, 128)
    tpl = _clean_templates(template_values)
    stale = {k: v for k, v in _clean_templates(stale_template_values).items() if k not in tpl}

    vendor_key, vendor_label = canonical_vendor(vendor, strict=strict)
    if vendor_key is None and oid:
        ent = _oid_enterprise(oid)
        if ent:
            vendor_key, vendor_label = ent, _VENDOR_BY_KEY[ent]
    model_raw = _clean(model, _CAPS["hw_model"])

    descr_version = None
    if descr and _Net.cisco_descr_rx is not None:
        dm = _Net.cisco_descr_rx.search(descr)
        descr_version = dm.group(1).rstrip(".,;") if dm else None
    cisco_texts = [t for t in (osv, fwv, descr_version) if t]
    version_texts = [t for t in (
        *(tpl[k] for k in _Net.template_family if k in tpl), osv, fwv,
        *(stale[k] for k in _Net.template_family if k in stale), descr_version,
    ) if t]
    family, ambiguous = _detect_family(profile, oid, descr, model_raw, vendor_key, version_texts, cisco_texts)
    if family is None:
        for key, fam in _Net.template_family.items():
            if key in tpl or key in stale:
                family = fam
                break
    fam_def = _Net.families.get(family) if family else None
    if vendor_key is None and fam_def is not None and fam_def.vendor in _VENDOR_BY_KEY:
        vendor_key, vendor_label = fam_def.vendor, _VENDOR_BY_KEY[fam_def.vendor]  # type: ignore[index]

    os_fields: dict = {"os_family": "unknown"}
    os_source: Optional[str] = None
    if fam_def is not None:
        # Fresh template, profile extract, chassis firmware, sysDescr; a template value older than the
        # caller's freshness window ranks last but is still used rather than dropped.
        candidates: list[tuple[str, Optional[str]]] = [("template", tpl.get(k)) for k in fam_def.template_keys]
        candidates += [("profile", osv), ("entity", fwv), ("sysdescr", _descr_version(fam_def, descr))]
        candidates += [("template_stale", stale.get(k)) for k in fam_def.template_keys]
        fallback: Optional[tuple[str, str]] = None
        parsed = None
        for source, raw in candidates:
            if not raw:
                continue
            parsed = _parse_net_version(fam_def, raw)
            if parsed:
                os_source = source
                break
            if fallback is None and _HAS_DIGIT_RE.search(raw):
                fallback = (source, raw)
        os_fields = {"os_family": fam_def.key, "os_product": fam_def.product}
        if parsed:
            os_fields.update(os_version=parsed[0], os_build=parsed[1], os_release=parsed[2])
        elif fallback:
            os_source, os_fields["os_version"] = fallback[0], fallback[1]
    else:
        # Version known, product not decided: keep the evidence. 'other' means an OS outside the
        # tables, 'unknown' means undecided between known ones (a Cisco version that fits several).
        for source, raw in (("profile", osv), ("entity", fwv)):
            if raw and _HAS_DIGIT_RE.search(raw):
                os_fields = {"os_family": "unknown" if ambiguous else "other", "os_version": raw}
                os_source = source
                break

    model_clean = _clean_model(vendor_key, model_raw)
    serial = _clean_serial(serial_number)
    serial_from_template = False
    if serial is None:
        for source_values in (tpl, stale):
            for key in _Net.template_serial_keys:
                serial = _clean_serial(source_values.get(key))
                if serial:
                    serial_from_template = True
                    break
            if serial:
                break
    hw_source = None
    if model_clean or serial:
        hw_source = "template" if serial_from_template and not model_clean else "device"
    return _identity(
        vendor_key=vendor_key, vendor_label=vendor_label,
        os_family=os_fields.get("os_family", "unknown"), os_product=os_fields.get("os_product"),
        os_release=os_fields.get("os_release"), os_version=os_fields.get("os_version"),
        os_build=os_fields.get("os_build"), os_edition=None, os_source=os_source,
        hw_vendor=vendor_label, hw_model=model_clean, hw_serial=serial, hw_source=hw_source,
    )


def normalize_device(*, vendor: Any = None, model: Any = None, os_version: Any = None, firmware_version: Any = None,
                     serial_number: Any = None, sys_object_id: Any = None, sys_descr: Any = None,
                     profile_name: Any = None, template_values: Optional[Mapping[str, Any]] = None,
                     stale_template_values: Optional[Mapping[str, Any]] = None, strict: bool = False) -> Identity:
    """Identity of an SNMP-managed network device.

    ``os_family`` comes from the device's own evidence first: the sysObjectID
    prefix, then sysDescr. The profile name is operator text, so it only decides
    when neither names a family (and, between IOS and IOS XE, when the format of
    the reported version does not say otherwise); after it come the model (same
    vendor only), the vendor's only OS, and the shape of the version string. A
    plain "Cisco IOS Software" banner does not decide between IOS and IOS XE on
    its own: the version format does. The OS version is taken in the precedence
    of doc 02 section 3.2: a fresh template value (``fgt_fw_version``,
    ``pan_sw_version``, ``f5_version``), then ``os_version`` (profile extract),
    then ``firmware_version`` (chassis ENTITY-MIB), then a version found in
    sysDescr, then a template value older than the caller's window;
    ``os_source`` records which one won (``template``, ``profile``, ``entity``,
    ``sysdescr``, ``template_stale``). ``template_values`` must hold only the
    values the caller considers fresh (24 hours) and ``stale_template_values``
    the older ones, because this function has no clock: old data is a last
    resort, never a reason to erase a known version.
    ``hw_serial`` prefers ``serial_number``, then a fresh and then a stale
    ``f5_serial``; ``hw_source`` is ``template`` or ``device`` and the caller may
    replace it when it knows the model came from ENTITY-MIB. Never raises unless
    ``strict``: then an internal failure propagates instead of giving the
    ``unknown`` identity, so a caller that persists the result can retry the row.
    """
    try:
        return _normalize_device(vendor, model, os_version, firmware_version, serial_number, sys_object_id,
                                 sys_descr, profile_name, template_values, stale_template_values, strict)
    except Exception as exc:
        if strict:
            raise
        logger.warning("normalize_device failed: %r", exc)
        return _UNKNOWN


# ── Software ─────────────────────────────────────────────────────────

@dataclass(frozen=True)
class _SoftRule:
    id: str
    rx: "re.Pattern[str]"
    publisher_rx: Optional["re.Pattern[str]"]
    product: str
    product_lc: str
    key: str
    pub_key: Optional[str]
    pub_label: Optional[str]
    category: str
    edition: Optional[str]
    cycle: Optional[str]
    cycle_from_version: Optional[str]
    sources: Optional[frozenset[str]]


@dataclass(frozen=True)
class _NoiseRule:
    id: str
    rx: "re.Pattern[str]"
    category: str
    key_rx: Optional["re.Pattern[str]"]
    sources: Optional[frozenset[str]]


_SOFT_RULES: list[_SoftRule] = []
_NOISE_RULES: list[_NoiseRule] = []
_PRODUCT_EXACT: dict[str, _SoftRule] = {}
_LINUX_SOURCES = frozenset({"dpkg", "rpm"})

_ARCH_TOKEN = (r"(?:x64_arm64|x86_arm64|x86_x64|x86_64|x86-64|amd64|arm64|aarch64|x64|x86|win64|win32"
               r"|64[- ]?bit|32[- ]?bit)")
_ARCH_RE = _rx(r"(?<![a-z0-9])" + _ARCH_TOKEN + r"(?![a-z0-9])", "sw-arch")
_EMPTY_BRACKET_RE = _rx(r"\(\s{0,3}(?:edition|version)?\s{0,3}\)|\[\s{0,3}\]|\(\s{0,3}[a-z]{2}-[a-z]{2,4}\s{0,3}\)", "sw-empty-bracket")
_TRAILING_VERSION_RE = _rx(
    r"\s{1,4}(?:[-:]\s{1,4})?(?:v|ver\.?\s{0,2}|version\s{1,3}|release\s{1,3}|build\s{1,3}|rev\.?\s{0,2})?"
    r"\d{1,6}(?:\.\d{1,6}){1,4}\b(?P<tail>.*)$",
    "sw-trailing-version",
)
# What follows the version is usually decoration (locale, build, update words) and is dropped. A short run of
# plain words that is not decoration is part of the product name ("Studio 2.0 Server" is not "Studio 2.0 Client"),
# so it is kept: "Acme Studio 2.0 Server" gives "Acme Studio Server".
_TAIL_WORDS_RE = _rx(r"^\s{1,4}[a-z]{3,15}(?:\s{1,3}[a-z]{3,15}){0,2}\s{0,4}$", "sw-tail-words")
_TAIL_DECORATION = frozenset({
    "update", "updates", "upgrade", "hotfix", "patch", "beta", "alpha", "preview", "dev", "nightly", "canary",
    "insiders", "release", "final", "build", "version", "setup", "installer", "install", "service", "lts",
    "stable", "portable", "edition", "language", "for", "with", "and", "the", "windows", "win", "linux",
})
_TRAILING_WORD_RE = _rx(r"\s{1,4}(?:release|version|ver\.?|build|rev\.?|v)$", "sw-trailing-word")
_COPYRIGHT_RE = _rx(r"\s{0,3}(?:copyright|\(c\))\s{0,3}(?:19|20)\d\d(?:\s{0,2}[-,]\s{0,2}(?:19|20)\d\d)?\b.*$", "sw-copyright")
_EDITION_RE = _rx(
    r"\s{1,3}(?P<e>community|professional|enterprise|standard|express|home|pro|ultimate|premium|free|lite|developer"
    r"|personal|business|server|workstation|trial|evaluation)\s{1,3}edition\s{0,2}$",
    "sw-edition",
)
_EDITION_WORD_RE = _rx(r"\s{1,3}edition$", "sw-edition-word")
_LOCALE_RE = _rx(r"\s{1,3}[-:]\s{1,3}[a-z]{2}-[a-z]{2,4}$", "sw-locale")
_LINUX_ARCH_RE = _rx(r":[a-z0-9_]{2,10}$", "sw-linux-arch")
_LINUX_MAINTAINER_RE = _rx(r"\s{0,3}<[^>]{0,200}>", "sw-maintainer")
_EDGE_CHARS = " ,;:._-/"


_CYCLE_MODES = ("major", "major_minor")


def _load_software(pack: dict) -> None:
    cats = {c for c in _section(PACK_SOFTWARE, pack, "categories", list) if isinstance(c, str)} or set(CATEGORIES)
    seen: set[str] = set()
    for i, r in enumerate(_section(PACK_SOFTWARE, pack, "rules", list)):
        try:
            rid = _text(r, "id")
            if rid in seen:
                raise ValueError("duplicate rule id")
            category = _text(r, "category")
            if category not in cats:
                raise ValueError(f"unknown category {category}")
            pub = _text(r, "publisher", False)
            if pub is not None and pub not in _VENDOR_BY_KEY:
                raise ValueError(f"unknown publisher key {pub}")
            product = _text(r, "product")
            rx = _rx(_text(r, "name_re"), f"sw-{rid}")
            edition, cycle, mode = _text(r, "edition", False), _text(r, "cycle", False), _text(r, "cycle_from_version", False)
            for template in (edition, cycle):
                if template:
                    _check_template(rx, template)
            if mode is not None and mode not in _CYCLE_MODES:
                raise ValueError(f"unknown cycle_from_version {mode!r}")
            pub_re = _text(r, "publisher_re", False)
            sources = _strings(r, "sources")
            rule = _SoftRule(
                id=rid, rx=rx,
                publisher_rx=_rx(pub_re, f"sw-{rid}-pub") if pub_re else None,
                product=product, product_lc=product.lower(),
                key=_slug(_text(r, "key", False) or product, _NAME_KEY_MAX),
                pub_key=pub, pub_label=_VENDOR_BY_KEY.get(pub) if pub else None,
                category=category, edition=edition, cycle=cycle, cycle_from_version=mode,
                sources=frozenset(sources) if sources else None,
            )
            _SOFT_RULES.append(rule)
            seen.add(rid)
            if rule.sources is None:
                _PRODUCT_EXACT.setdefault(rule.product_lc, rule)
        except Exception as exc:
            _pack_error(PACK_SOFTWARE, f"rule {_ident(r, i)}: {exc}")
    for i, r in enumerate(_section(PACK_SOFTWARE, pack, "noise_rules", list)):
        try:
            rid = _text(r, "id")
            if rid in seen:
                raise ValueError("duplicate rule id")
            category = _text(r, "category")
            if category not in cats:
                raise ValueError(f"unknown category {category}")
            key_re = _text(r, "key_re", False)
            sources = _strings(r, "sources")
            _NOISE_RULES.append(_NoiseRule(
                id=rid, rx=_rx(_text(r, "name_re"), f"sw-{rid}"), category=category,
                key_rx=_rx(key_re, f"sw-{rid}-key", groups=1) if key_re else None,
                sources=frozenset(sources) if sources else None,
            ))
            seen.add(rid)
        except Exception as exc:
            _pack_error(PACK_SOFTWARE, f"noise rule {_ident(r, i)}: {exc}")


def _strip_arch(name: str) -> str:
    s = _ARCH_RE.sub(" ", name)
    for _ in range(2):
        s = _EMPTY_BRACKET_RE.sub(" ", s)
    return " ".join(s.split()).strip(" -,:;")


def _kept_tail(tail: str) -> str:
    """The words after a version that belong to the product name, or an empty string."""
    if not _TAIL_WORDS_RE.match(tail) or any(w in _TAIL_DECORATION for w in tail.lower().split()):
        return ""
    return " " + tail.strip()


def _generic_residual(name: str) -> tuple[str, Optional[str]]:
    """Name without trailing version, build words, locale, edition and copyright years."""
    s = name
    edition: Optional[str] = None
    for _ in range(4):
        before = s
        s = _COPYRIGHT_RE.sub("", s)
        em = _EDITION_RE.search(s)
        if em:
            edition = em.group("e").title()
            s = s[: em.start()]
        s = _EDITION_WORD_RE.sub("", s)
        vm = _TRAILING_VERSION_RE.search(s)
        if vm:
            s = s[: vm.start()] + _kept_tail(vm.group("tail"))
        s = _TRAILING_WORD_RE.sub("", s)
        s = _LOCALE_RE.sub("", s)
        s = s.strip(_EDGE_CHARS)
        if s == before:
            break
    return s, edition


def _generic_key(residual: str, pub_key: Optional[str]) -> str:
    slug = _slug(residual, _NAME_KEY_MAX)
    if not slug:
        return "unnamed-software"
    if pub_key and slug != pub_key and not slug.startswith(pub_key + "-"):
        return _slug(f"{pub_key}-{slug}", _NAME_KEY_MAX)
    return slug


def _expand(rule_template: Optional[str], match: Optional["re.Match[str]"], limit: int) -> Optional[str]:
    if not rule_template or match is None:
        return None
    try:
        return _cap(match.expand(rule_template), limit)
    except (re.error, IndexError):
        return None


_MAJOR_RE = _rx(r"^[^0-9]{0,2}(\d{1,5})", "sw-major")


def _cycle_from_version(mode: Optional[str], version: Optional[str]) -> Optional[str]:
    if not mode or not version:
        return None
    if mode == "major":
        m = _MAJOR_RE.match(version)
        return m.group(1) if m else None
    if mode == "major_minor":
        return _cycle("semverish", version)
    return None


def _publisher(raw: Optional[str]) -> tuple[Optional[str], Optional[str], bool]:
    if raw is None:
        return None, None, False
    return _vendor_cached(raw)


def _build_matched(rule: _SoftRule, match: Optional["re.Match[str]"],
                   pub: tuple[Optional[str], Optional[str], bool]) -> tuple[SoftwareKey, Optional[str]]:
    """The key for a rule hit, plus the rule's ``cycle_from_version`` mode when the cycle is still open."""
    pub_key, pub_label = (rule.pub_key, rule.pub_label) if rule.pub_key else (pub[0], pub[1])
    cycle = _expand(rule.cycle, match, _CAPS["os_release"])
    key = SoftwareKey(
        name_key=rule.key,
        publisher_key=_cap(pub_key, _PUBLISHER_KEY_MAX), publisher_label=_cap(pub_label, _LABEL_MAX),
        product_name=rule.product, category=rule.category,
        edition=_expand(rule.edition, match, _CAPS["os_edition"]), cycle=cycle,
        status="normalized", rule_id=rule.id,
    )
    return key, (rule.cycle_from_version if cycle is None else None)


# The version is deliberately not part of the cache key: a fleet has thousands of versions of
# the same title, and only the few rules with cycle_from_version look at it.
@functools.lru_cache(maxsize=16384)
def _software_cached(name: Optional[str], publisher: Optional[str],
                     source: Optional[str]) -> tuple[SoftwareKey, Optional[str]]:
    if not name:
        return _UNNAMED, None
    linux = source in _LINUX_SOURCES
    rules_name = _LINUX_ARCH_RE.sub("", name) if linux else _strip_arch(name)
    if linux and publisher:
        publisher = _LINUX_MAINTAINER_RE.sub("", publisher).strip() or None
    pub = _publisher(publisher)
    if not rules_name:
        return _UNNAMED, None

    # 1. Product rules. A name equal to a canonical product name maps to that product, which
    #    keeps normalisation idempotent (a stored product_name keys the same way again).
    exact = _PRODUCT_EXACT.get(rules_name.lower())
    if exact is not None:
        return _build_matched(exact, None, pub)
    for rule in _SOFT_RULES:
        if rule.sources is not None and source not in rule.sources:
            continue
        m = rule.rx.search(rules_name)
        if m is None:
            continue
        if rule.publisher_rx is not None and not (
            (publisher and rule.publisher_rx.search(publisher)) or (pub[1] and rule.publisher_rx.search(pub[1]))
        ):
            continue
        return _build_matched(rule, m, pub)

    # 2. Noise rules: updates, drivers, language packs.
    for nrule in _NOISE_RULES:
        if nrule.sources is not None and source not in nrule.sources:
            continue
        if nrule.rx.search(rules_name) is None:
            continue
        key = None
        product = rules_name  # a KB update keeps its full title: the KB number is its identity
        if nrule.key_rx is not None:
            km = nrule.key_rx.search(rules_name)
            key = _slug(km.group(1), _NAME_KEY_MAX) if km else None
        if not key and linux:
            key = _slug(rules_name, _NAME_KEY_MAX)
        elif not key:
            product, _ = _generic_residual(rules_name)
            product = product or rules_name
            key = _generic_key(product, pub[0])
        return SoftwareKey(
            name_key=key, publisher_key=_cap(pub[0], _PUBLISHER_KEY_MAX), product_name=product[:_PRODUCT_NAME_MAX],
            publisher_label=_cap(pub[1], _LABEL_MAX), category=nrule.category, edition=None, cycle=None,
            status="normalized", rule_id=nrule.id,
        ), None

    # 3. Generic fallback. Linux package names are already clean identifiers.
    if linux:
        residual, edition = rules_name, None
        key = _slug(rules_name, _NAME_KEY_MAX) or "unnamed-software"
    else:
        residual, edition = _generic_residual(rules_name)
        residual = residual or rules_name
        key = _generic_key(residual, pub[0])
    return SoftwareKey(
        name_key=key, publisher_key=_cap(pub[0], _PUBLISHER_KEY_MAX), product_name=residual[:_PRODUCT_NAME_MAX],
        publisher_label=_cap(pub[1], _LABEL_MAX), category="other", edition=_cap(edition, _CAPS["os_edition"]),
        cycle=None, status="publisher_only" if pub[2] else "unmatched", rule_id=None,
    ), None


def normalize_software(name: Any, publisher: Any = None, version: Any = None, *, source: Any = None,
                       strict: bool = False) -> SoftwareKey:
    """Normalised grouping key for one installed title.

    The pipeline runs in order and stops at the first match: product rules from
    ``software_rules.json`` (product, publisher, category, edition, cycle),
    then noise rules (KB updates, drivers, language packs), then the generic
    fallback (strip trailing version, architecture markers, copyright years,
    locale and edition words, then slug ``publisher_key-residual``). x64 and
    x86 builds of one product share a key, and ``Google LLC`` and ``Google
    Inc.`` share a publisher key. ``source`` (``hklm``, ``hkcu``, ``dpkg``,
    ``rpm`` ...) switches Linux package names to identifier handling. Words that
    follow the version and belong to the name ("Studio 2.0 Server") are kept in the
    key; update, build and locale words are dropped. Never raises
    unless ``strict``: then an internal failure propagates instead of giving the
    ``unnamed-software`` key, so a caller that persists the result can retry the row.
    """
    try:
        src = _clean(source, 32)
        key, cycle_mode = _software_cached(_clean(name), _clean(publisher, 256), src.lower() if src else None)
        if cycle_mode:
            cycle = _cycle_from_version(cycle_mode, _clean(version, 128))
            if cycle:
                key = dataclasses.replace(key, cycle=cycle)
        return key
    except Exception as exc:
        if strict:
            raise
        logger.warning("normalize_software failed: %r", exc)
        return _UNNAMED


# ── Import-time load ─────────────────────────────────────────────────

def _load_all() -> None:
    os_pack, vendor_pack, software_pack = _read_pack(PACK_OS), _read_pack(PACK_VENDORS), _read_pack(PACK_SOFTWARE)
    _guard(PACK_VENDORS, "vendors", _load_vendors, vendor_pack)
    for name, loader in (("windows", _load_windows), ("distros", _load_distros),
                         ("hardware", _load_hardware), ("network", _load_network)):
        _guard(PACK_OS, name, loader, os_pack)
    _guard(PACK_SOFTWARE, "software", _load_software, software_pack)


_load_all()
