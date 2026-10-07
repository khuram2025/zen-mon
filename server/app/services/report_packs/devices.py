"""Device (node) sections: current state, availability by group/site,
latency/loss, stability, reboots and planned maintenance.

Canonical example of a section pack: see report_packs/__init__.py for the
authoring rules and tests/test_report_packs_contract.py for the contract.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _section,
    register_section,
    top_n_option,
)

STATUS_ORDER = ("down", "degraded", "maintenance", "unknown", "up")


@register_section(
    "device_status_now", title="Current Device Status", category="Availability",
    description="Up, down, degraded and unknown nodes right now, and the nodes that are down.",
    scope="device", requires=("devices.view",), semantics="snapshot",
    options=[top_n_option(25, 200)],
)
async def device_status_now(ctx: SectionCtx) -> dict:
    clause, params = ctx.pg_device_clause("d.id")
    rows = (await ctx.db.execute(text(
        "SELECT d.hostname, host(d.ip_address), COALESCE(NULLIF(d.location, ''), '—'), "
        "COALESCE(d.status, 'unknown'), d.last_seen "
        f"FROM devices d WHERE TRUE{clause} ORDER BY d.hostname"), params)).fetchall()
    if not rows:
        return _section("device_status_now", "Current Device Status",
                        notes=["No monitored devices in scope."])

    counts: dict[str, int] = {}
    for r in rows:
        counts[r[3]] = counts.get(r[3], 0) + 1
    down = [r for r in rows if r[3] in ("down", "degraded")]
    down.sort(key=lambda r: (r[3] != "down", r[4] or ctx.frm))
    top_n = int(ctx.opt("top_n"))
    total = len(rows)
    up = counts.get("up", 0)

    def share(n: int) -> str:
        return f"{n / total * 100:.1f}% of {total}" if total else "—"

    return _section(
        "device_status_now", "Current Device Status",
        description=f"State of {total} monitored node(s) when the report was generated.",
        kpis=[
            {"label": "Up", "value": f"{up:,}", "accent": "success", "subtitle": share(up)},
            {"label": "Down", "value": f"{counts.get('down', 0):,}",
             "accent": "danger" if counts.get("down") else "success", "subtitle": share(counts.get("down", 0))},
            {"label": "Degraded", "value": f"{counts.get('degraded', 0):,}",
             "accent": "warning" if counts.get("degraded") else "neutral"},
            {"label": "Unknown", "value": f"{counts.get('unknown', 0):,}", "accent": "neutral",
             "subtitle": "Not measured"},
        ],
        charts=[{"title": "Nodes by state",
                 "series": {"kind": "donut", "unit": "nodes",
                            "points": [{"label": s.capitalize(), "value": counts[s]}
                                       for s in STATUS_ORDER if counts.get(s)]}}],
        tables=[{"title": "Nodes that are down or degraded",
                 "headers": ["Node", "IP address", "Location", "Status", "Last seen"],
                 "styles": ["text", "mono", "text", "status", "text"],
                 "rows": [[r[0] or r[1] or "—", r[1] or "—", r[2], r[3], ctx.fmt_short(r[4])]
                          for r in down[:top_n]]}] if down else [],
        notes=[] if down else ["Every node in scope is up or not yet measured."],
    )


def _weighted(groups: dict[str, dict[str, Any]], key: str, node: dict) -> None:
    g = groups.setdefault(key, {"nodes": 0, "measured": 0, "avail_sum": 0.0, "down_min": 0.0,
                                "outages": 0, "worst": None})
    g["nodes"] += 1
    g["outages"] += node.get("outages") or 0
    if node.get("avail") is None:
        return
    g["measured"] += 1
    g["avail_sum"] += node["avail"]
    g["down_min"] += node.get("downtime_min") or 0.0
    if g["worst"] is None or node["avail"] < g["worst"][1]:
        g["worst"] = (node["hostname"], node["avail"])
