"""Software-baseline (compliance) evaluation engine.

A *baseline* declares software expectations for a class of servers — scoped
by os_type / site / tags — through rules:

* ``required``    package must be installed (optionally at ``min_version``+)
* ``prohibited``  package must NOT be installed

Evaluation compares rules against ``server_software_inventory`` (which the
agent refreshes with every inventory snapshot), stores one outcome row per
(server, rule) in ``server_baseline_results``, and raises/resolves alerts for
violations on baselines with ``alerting`` enabled.

Triggered from: software-inventory ingest (agents), baseline/rule CRUD, and
the manual *Evaluate now* endpoints.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.server_health_service import create_server_alert, resolve_server_alerts

logger = logging.getLogger("zenplus.baselines")


# ── Version comparison ───────────────────────────────────────────────

_CHUNK_RE = re.compile(r"(\d+|[a-zA-Z]+)")


def _version_key(version: str) -> list[tuple[int, Any]]:
    """Tokenize '10.0.19045 Build 19045' → comparable mixed-type key.

    Numeric chunks sort numerically and rank above alphabetic chunks so
    '1.2.10' > '1.2.9' and '1.2' > '1.2-beta'.
    """
    chunks = _CHUNK_RE.findall(version or "")
    key: list[tuple[int, Any]] = []
    for c in chunks:
        if c.isdigit():
            key.append((1, int(c)))
        else:
            key.append((0, c.lower()))
    return key


def compare_versions(a: str, b: str) -> int:
    """Return -1/0/1 for a<b / a==b / a>b using tokenized comparison."""
    ka, kb = _version_key(a), _version_key(b)
    # Pad the shorter key with zero-chunks so '1.2' == '1.2.0'.
    n = max(len(ka), len(kb))
    ka += [(1, 0)] * (n - len(ka))
    kb += [(1, 0)] * (n - len(kb))
    if ka < kb:
        return -1
    if ka > kb:
        return 1
    return 0


# ── Package matching ─────────────────────────────────────────────────

def match_package(package_match: str, match_type: str, package_name: str) -> bool:
    name = (package_name or "").strip().lower()
    pat = (package_match or "").strip().lower()
    if not pat or not name:
        return False
    if match_type == "exact":
        return name == pat
    if match_type == "regex":
        try:
            return re.search(package_match, package_name, re.IGNORECASE) is not None
        except re.error:
            return False
    return pat in name  # contains (default)


# ── Evaluation ───────────────────────────────────────────────────────

def _tags_list(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(t) for t in v]
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return [str(t) for t in parsed] if isinstance(parsed, list) else []
        except Exception:
            return []
    return []


async def _applicable_baselines(db: AsyncSession, server: dict) -> list[dict]:
    rows = (await db.execute(
        text("""SELECT * FROM software_baselines
                WHERE enabled = TRUE
                  AND (os_type IS NULL OR os_type = :os)
                  AND (site_id IS NULL OR site_id = :site)"""),
        {"os": server.get("os_type"), "site": server.get("site_id")},
    )).mappings().all()

    server_tags = set(_tags_list(server.get("tags")))
    out = []
    for b in rows:
        need = set(_tags_list(b.get("match_tags")))
        if need and not need.issubset(server_tags):
            continue
        out.append(dict(b))
    return out


def _evaluate_rule(rule: dict, software: list[tuple[str, str]]) -> dict:
    """Evaluate one rule against [(package_name, version)] → result fields."""
    matches = [(n, v) for n, v in software
               if match_package(rule["package_match"], rule["match_type"], n)]

    if rule["rule_type"] == "prohibited":
        if matches:
            name, ver = matches[0]
            return {"status": "prohibited", "found_package": name, "found_version": ver,
                    "expected": f"must not be installed ({rule['package_match']})"}
        return {"status": "compliant", "found_package": None, "found_version": None,
                "expected": f"must not be installed ({rule['package_match']})"}

    # required
    if not matches:
        exp = rule["package_match"]
        if rule.get("min_version"):
            exp += f" ≥ {rule['min_version']}"
        return {"status": "missing", "found_package": None, "found_version": None,
                "expected": exp}

    # Best (highest) version among matches wins.
    best = max(matches, key=lambda m: _version_key(m[1] or ""))
    name, ver = best
    if rule.get("min_version") and compare_versions(ver or "0", rule["min_version"]) < 0:
        return {"status": "outdated", "found_package": name, "found_version": ver,
                "expected": f"{rule['package_match']} ≥ {rule['min_version']}"}
    exp = rule["package_match"]
    if rule.get("min_version"):
        exp += f" ≥ {rule['min_version']}"
    return {"status": "compliant", "found_package": name, "found_version": ver,
            "expected": exp}


async def evaluate_server(db: AsyncSession, server_id: str, commit: bool = True) -> dict:
    """Evaluate every applicable baseline rule for one server.

    Returns a summary dict {evaluated, compliant, missing, outdated, prohibited}.
    """
    server = (await db.execute(
        text("SELECT id, display_name, hostname, os_type, site_id, tags, status FROM servers WHERE id = :id"),
        {"id": server_id},
    )).mappings().first()
    if not server:
        return {"evaluated": 0}
    server = dict(server)

    baselines = await _applicable_baselines(db, server)
    rules: list[dict] = []
    for b in baselines:
        b_rules = (await db.execute(
            text("SELECT * FROM software_baseline_rules WHERE baseline_id = :bid"),
            {"bid": b["id"]},
        )).mappings().all()
        for r in b_rules:
            r = dict(r)
            r["_baseline"] = b
            rules.append(r)

    software = [(r[0], r[1] or "") for r in (await db.execute(
        text("SELECT package_name, version FROM server_software_inventory WHERE server_id = :sid"),
        {"sid": server_id},
    )).all()]

    summary = {"evaluated": len(rules), "compliant": 0, "missing": 0, "outdated": 0, "prohibited": 0}
    applicable_rule_ids: list[str] = []

    for rule in rules:
        outcome = _evaluate_rule(rule, software)
        summary[outcome["status"]] += 1
        applicable_rule_ids.append(str(rule["id"]))

        await db.execute(
            text("""INSERT INTO server_baseline_results
                        (server_id, rule_id, baseline_id, status, found_package,
                         found_version, expected, severity, first_failed_at, evaluated_at)
                    VALUES (:sid, :rid, :bid, :st, :fp, :fv, :exp, :sev,
                            CASE WHEN :ok THEN NULL ELSE NOW() END, NOW())
                    ON CONFLICT (server_id, rule_id) DO UPDATE SET
                        status = EXCLUDED.status,
                        found_package = EXCLUDED.found_package,
                        found_version = EXCLUDED.found_version,
                        expected = EXCLUDED.expected,
                        severity = EXCLUDED.severity,
                        first_failed_at = CASE
                            WHEN EXCLUDED.first_failed_at IS NULL THEN NULL
                            WHEN server_baseline_results.status = 'compliant'
                              OR server_baseline_results.first_failed_at IS NULL THEN NOW()
                            ELSE server_baseline_results.first_failed_at
                        END,
                        evaluated_at = NOW()"""),
            {
                "sid": server_id, "rid": rule["id"], "bid": rule["baseline_id"],
                "st": outcome["status"], "fp": outcome["found_package"],
                "fv": outcome["found_version"], "exp": outcome["expected"],
                "sev": rule["severity"], "ok": outcome["status"] == "compliant",
            },
        )

        # Alerting
        baseline = rule["_baseline"]
        dedupe = f"baseline:{rule['id']}"
        server_label = server.get("display_name") or server.get("hostname") or server_id
        if outcome["status"] == "compliant" or not baseline.get("alerting", True):
            await resolve_server_alerts(db, server_id, dedupe)
        else:
            if outcome["status"] == "missing":
                msg = f"Baseline '{baseline['name']}': required software '{rule['package_match']}' missing on {server_label}"
            elif outcome["status"] == "outdated":
                msg = (f"Baseline '{baseline['name']}': {outcome['found_package']} "
                       f"{outcome['found_version']} on {server_label} is below required {rule['min_version']}")
            else:  # prohibited
                msg = (f"Baseline '{baseline['name']}': prohibited software "
                       f"'{outcome['found_package']}' present on {server_label}")
            await create_server_alert(
                db, server_id,
                severity=rule["severity"], message=msg, source="baseline",
                dedupe=dedupe,
                metadata={
                    "baseline_id": str(rule["baseline_id"]),
                    "baseline_name": baseline["name"],
                    "rule_id": str(rule["id"]),
                    "status": outcome["status"],
                },
            )

    # Drop results (and resolve alerts) for rules that no longer apply.
    stale_rows = (await db.execute(
        text("""DELETE FROM server_baseline_results
                WHERE server_id = :sid AND NOT (rule_id = ANY(CAST(:ids AS uuid[])))
                RETURNING rule_id"""),
        {"sid": server_id, "ids": applicable_rule_ids or ["00000000-0000-0000-0000-000000000000"]},
    )).fetchall()
    for (rule_id,) in stale_rows:
        await resolve_server_alerts(db, server_id, f"baseline:{rule_id}")

    if commit:
        await db.commit()
    return summary


async def evaluate_baseline(db: AsyncSession, baseline_id: str) -> int:
    """Re-evaluate every server a baseline could apply to. Returns server count.

    Evaluation is whole-server (a server may sit in several baselines), so we
    just collect candidate servers by scope and run the standard path.
    """
    b = (await db.execute(
        text("SELECT * FROM software_baselines WHERE id = :id"),
        {"id": baseline_id},
    )).mappings().first()
    if not b:
        return 0

    where = ["status != 'disabled'"]
    params: dict[str, Any] = {}
    if b.get("os_type"):
        where.append("os_type = :os")
        params["os"] = b["os_type"]
    if b.get("site_id"):
        where.append("site_id = :site")
        params["site"] = b["site_id"]

    rows = (await db.execute(
        text(f"SELECT id FROM servers WHERE {' AND '.join(where)}"), params,
    )).fetchall()

    for (sid,) in rows:
        await evaluate_server(db, str(sid), commit=False)
    await db.commit()
    return len(rows)


async def evaluate_all(db: AsyncSession) -> int:
    rows = (await db.execute(
        text("SELECT id FROM servers WHERE status != 'disabled'"),
    )).fetchall()
    for (sid,) in rows:
        await evaluate_server(db, str(sid), commit=False)
    await db.commit()
    return len(rows)
