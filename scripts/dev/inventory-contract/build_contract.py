#!/usr/bin/env python3
"""Build Documentation/Inventory/05-P1-API-CONTRACT.md.

The document is generated from four inputs that all live in the repository:

* ``contract.template.md``  the hand-written prose (conventions, TypeScript interfaces, rules, the drill-down map);
* the golden fixtures       ``dashboard/src/pages/inventory/fixtures/*.json``, the real responses of the API for the
                            seeded fleet (``server/tests/test_inventory_fixtures.py`` keeps them true). Every JSON example
                            in the document is one of them, cut short (``examples.py``); the numbers in the prose that
                            describe the fixtures are read from them too (``@@G:<fixture>.<path>@@``);
* the server code           limits, enums, defaults and column lists are imported from ``server/app`` (``facts.py``), so a
                            constant that changes in the code changes in the document (``@@F:<name>@@``);
* ``perf-core.md``, ``perf-software.md``  the measured-performance tables (static text: see README.md).

Usage (from the repository root, with the server's Python requirements installed)::

    python scripts/dev/inventory-contract/build_contract.py            # rewrite the document
    python scripts/dev/inventory-contract/build_contract.py --check    # exit 1 when the committed document is stale
    python scripts/dev/inventory-contract/build_contract.py --out /tmp/05.md

No database, no network, no running service is needed. Output is deterministic: two runs give identical bytes.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
OUT = REPO / "Documentation" / "Inventory" / "05-P1-API-CONTRACT.md"

sys.path.insert(0, str(HERE))

import examples  # noqa: E402  (after the path tweak)
import facts  # noqa: E402

PLACEHOLDER = re.compile(r"@@([A-Z_]+)(?::([A-Za-z0-9_.\-]+))?@@")


def build() -> str:
    """The document text. Raises when the template names an example, a fact or a fixture value that does not exist."""
    template = (HERE / "contract.template.md").read_text(encoding="utf-8")
    ex = examples.build()
    fx = facts.collect()
    perf = {
        "PERF": (HERE / "perf-core.md").read_text(encoding="utf-8").strip(),
        "PERF_SW": (HERE / "perf-software.md").read_text(encoding="utf-8").strip(),
    }

    def sub(m: re.Match) -> str:
        kind, arg = m.group(1), m.group(2)
        if kind == "EX":
            if arg not in ex:
                raise KeyError(f"template names the example {arg!r}; examples.py builds {sorted(ex)}")
            return ex[arg]
        if kind == "F":
            if arg not in fx:
                raise KeyError(f"template names the fact {arg!r}; facts.py provides {sorted(fx)}")
            return fx[arg]
        if kind == "G":
            return examples.golden_value(arg)
        if kind in perf and arg is None:
            return perf[kind]
        raise KeyError(f"unknown placeholder {m.group(0)!r}")

    text = template
    for _ in range(3):                                   # the performance partials may carry placeholders too
        text = PLACEHOLDER.sub(sub, text)
    leftover = re.findall(r"@@[^@\n]*@@", text)
    if leftover:
        raise ValueError(f"unresolved placeholders: {leftover[:5]}")
    if not text.endswith("\n"):
        text += "\n"
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", type=Path, default=OUT, help="where to write (default: Documentation/Inventory/05-P1-API-CONTRACT.md)")
    ap.add_argument("--check", action="store_true", help="do not write; exit 1 when the document on disk differs from the generated text")
    args = ap.parse_args(argv)
    text = build()
    if args.check:
        current = args.out.read_text(encoding="utf-8") if args.out.exists() else ""
        if current == text:
            print(f"{args.out.relative_to(REPO) if args.out.is_relative_to(REPO) else args.out} is up to date")
            return 0
        diff = difflib.unified_diff(current.splitlines(), text.splitlines(), "committed", "generated", lineterm="", n=1)
        for i, line in enumerate(diff):
            if i >= 60:
                print("... (diff cut)")
                break
            print(line)
        print("\nThe contract is stale: run python scripts/dev/inventory-contract/build_contract.py", file=sys.stderr)
        return 1
    args.out.write_text(text, encoding="utf-8")
    print(f"wrote {args.out} ({len(text.splitlines())} lines, {len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
