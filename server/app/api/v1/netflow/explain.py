"""NetFlow "what changed" explainer (CR-15, WP B1).

``GET /netflow/explain`` (window + filter in the query string, like every read)
and ``POST /netflow/explain`` (the same as a JSON body, for saved or composed
questions) compare the window with a baseline period and rank, per dimension,
the items whose traffic changed most:

* baselines: ``previous_equal`` (the immediately preceding window of the same
  length) or ``same_time_last_week`` (the same span seven days earlier; windows
  of at most seven days). Both periods are resolved from the one server-side
  window and carry the identical filter (NF-02).
* dimensions: source and destination hosts, conversations (host pairs), IP
  protocols, services (classified service port, both directions), applications
  and exporters. Each item says how much it changed, its share of the total
  change and the filter that isolates it (``filter``), so the UI can apply it
  or open the raw flows behind it.
* both periods go through the source planner (hourly rollup interior, raw
  edges, retention-aware); a baseline that is partly beyond retention or poorly
  covered is disclosed, and no percentage is stated for it.

Contributors are ranked by absolute byte change. Hosts and conversations count
bytes once per flow record; a flow observed at several exporters counts at each
(use ``count=edge`` to count each flow once at its edge collection point).
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.services import netflow_classify, netflow_groups

from .access import FlowVisibility, canonical_ip, flow_visibility, resolve_exporter_owners
from .common import (
    CONVERSATIONS_1H,
    MAX_SPAN_ROLLUP,
    Window,
    _port_name,
    _protocol_name,
    _to_iso_utc,
    current_meta,
    invalid_filter,
    resolve_window,
    run_query,
    window_params,
)
from .filters import (
    FILTER_ALIASES,
    FLOW_FILTER_KEYS,
    FlowFilter,
    flow_filter,
    load_counting_policy,
    parse_flow_filter,
    require_filter_exporters_visible,
    warn_exporter_notes,
)
from .meta import expected_intervals
from .overview import BASELINES, baseline_window, compare_delta, window_coverage
from .sql import DST, EXPORTER, SRC, SourcePlan, display, plan_source

router = APIRouter()

MAX_LIMIT = 20
DEFAULT_LIMIT = 5


def _classifier(element: str) -> tuple[str, str]:
    """(rollup, raw) expressions of the shared classifier's ``element``."""
    kw = dict(app_id_expr="app_id", src_addr="src_addr", dst_addr="dst_addr")
    rollup = netflow_classify.classification_sql(netflow_classify.ROLLUP_SRC_PORT_SQL, "dst_port", "protocol", **kw)
    raw = netflow_classify.classification_sql("src_port", "dst_port", "protocol", **kw)
    return rollup[element], raw[element]


# Dimension -> (label, [(key alias, expression | (rollup, raw), is_address)], filter keys)
def _dimensions() -> dict[str, tuple[str, list[tuple[str, Any, bool]], tuple[str, ...]]]:
    return {
        "src": ("Source hosts", [("k1", SRC, True)], ("src",)),
        "dst": ("Destination hosts", [("k1", DST, True)], ("dst",)),
        "conversation": ("Conversations", [("k1", SRC, True), ("k2", DST, True)], ("src", "dst")),
        "protocol": ("IP protocols", [("k1", "protocol", False)], ("protocol",)),
        "service": ("Services", [("k1", _classifier("service_port"), False)], ("service_port",)),
        "app": ("Applications", [("k1", _classifier("application"), False)], ("app",)),
        "exporter": ("Exporters", [("k1", EXPORTER, True)], ("exporter",)),
    }


DIMENSION_KEYS = ("src", "dst", "conversation", "protocol", "service", "app", "exporter")

_TIME_PARAM = re.compile(r"%\((nf_t\d+_(?:from|to))\)s")


def prefix_time_params(sql: str, params: dict, prefix: str) -> tuple[str, dict]:
    """Rename a plan's segment-bound parameters so two plans' subqueries can
    share one query (their filter parameters are identical and shared)."""
    out_sql = _TIME_PARAM.sub(lambda m: f"%({prefix}{m.group(1)})s", sql)
    out = {}
    for k, v in params.items():
        out[f"{prefix}{k}" if re.fullmatch(r"nf_t\d+_(?:from|to)", k) else k] = v
    return out_sql, out


def _pair_sql(cur: SourcePlan, base: SourcePlan, columns: dict, f: FlowFilter, vis: FlowVisibility) -> tuple[str, dict]:
    """``SELECT <keys>, b, n, 0 AS period`` over both plans, UNION ALL."""
    c_sql, c_params = cur.from_sql(columns, f, vis)
    b_sql, b_params = base.from_sql(columns, f, vis)
    c_sql, c_params = prefix_time_params(c_sql, c_params, "cur_")
    b_sql, b_params = prefix_time_params(b_sql, b_params, "base_")
    keys = [k for k in columns if k not in ("b", "n")]
    sel = ", ".join(keys + ["b", "n"])
    sql = (f"(SELECT {sel}, 0 AS period FROM {c_sql} AS c "
           f"UNION ALL SELECT {sel}, 1 AS period FROM {b_sql} AS p)")
    return sql, {**c_params, **b_params}


def contributor(item_keys: list[Any], cur_b: int, base_b: int, cur_n: int, base_n: int,
                total_delta: int, cur_total: int) -> dict:
    delta = cur_b - base_b
    if base_b == 0 and cur_b > 0:
        trend = "new"
    elif cur_b == 0 and base_b > 0:
        trend = "gone"
    else:
        trend = "up" if delta > 0 else "down" if delta < 0 else "flat"
    return {
        "current_bytes": cur_b,
        "baseline_bytes": base_b,
        "delta_bytes": delta,
        "delta_pct": round(delta / base_b * 100, 1) if base_b else None,
        "current_flows": cur_n,
        "baseline_flows": base_n,
        "share_of_change_pct": round(delta / total_delta * 100, 1) if total_delta else None,
        "share_of_current_pct": round(cur_b / cur_total * 100, 1) if cur_total else None,
        "trend": trend,
    }


def _label(dim: str, values: list[Any], owners: dict) -> tuple[str, dict]:
    """(display label, filter patch) for one contributor."""
    if dim in ("src", "dst"):
        ip = canonical_ip(values[0]) or str(values[0])
        return ip, {dim: ip}
    if dim == "conversation":
        a = canonical_ip(values[0]) or str(values[0])
        b = canonical_ip(values[1]) or str(values[1])
        return f"{a} → {b}", {"src": a, "dst": b}
    if dim == "protocol":
        n = int(values[0])
        return f"{_protocol_name(n)} ({n})", {"protocol": n}
    if dim == "service":
        port = int(values[0])
        if port == 0:
            return "No service port (portless or unclassified)", {"service_port": 0}
        return f"{_port_name(port)} ({port})", {"service_port": port}
    if dim == "app":
        return str(values[0]), {"app": str(values[0])}
    ip = canonical_ip(values[0]) or str(values[0])
    owner = owners.get(ip)
    return (f"{owner.hostname} ({ip})" if owner is not None and owner.hostname else ip), {"exporter": ip}


async def explain_change(
    win: Window,
    f: FlowFilter,
    vis: FlowVisibility,
    db: AsyncSession | None,
    *,
    baseline: str = "previous_equal",
    limit: int = DEFAULT_LIMIT,
    dimensions: list[str] | None = None,
) -> dict:
    require_filter_exporters_visible(f, vis)
    if not 1 <= limit <= MAX_LIMIT:
        raise invalid_filter(f"limit must be between 1 and {MAX_LIMIT}", {"limit": "Out of range"})
    dims = list(dict.fromkeys(dimensions or DIMENSION_KEYS))
    unknown = [d for d in dims if d not in DIMENSION_KEYS]
    if unknown:
        raise invalid_filter(f"Unknown dimension '{unknown[0][:40]}' (expected: {', '.join(DIMENSION_KEYS)})",
                             {"dimensions": "Unknown dimension"})
    base_win = baseline_window(win, baseline)
    cur_plan = await plan_source(win, f, rollup=CONVERSATIONS_1H)
    base_plan = await plan_source(base_win, f, rollup=CONVERSATIONS_1H, record=False)
    meta = current_meta()
    meta.tables.update(base_plan.tables)
    base_limited = base_plan.retention_limited or base_plan.effective_from > base_win.start

    flows = ("flows", "toUInt64(1)")
    total_sql, total_params = _pair_sql(cur_plan, base_plan, {"b": "bytes", "n": flows}, f, vis)
    totals_q = run_query(
        f"SELECT sumIf(b, period = 0), sumIf(b, period = 1), sumIf(n, period = 0), sumIf(n, period = 1) "
        f"FROM {total_sql} AS both",
        total_params, label="explain-totals",
    )
    specs = _dimensions()

    async def rank(dim: str):
        _, keys, _ = specs[dim]
        columns: dict[str, Any] = {alias: expr for alias, expr, _ in keys}
        columns.update({"b": "bytes", "n": flows})
        sql, params = _pair_sql(cur_plan, base_plan, columns, f, vis)
        group = ", ".join(alias for alias, _, _ in keys)
        shown = ", ".join(display(alias) if is_addr else alias for alias, _, is_addr in keys)
        res = await run_query(
            f"""
            SELECT {shown}, sumIf(b, period = 0) AS cur, sumIf(b, period = 1) AS base,
                   sumIf(n, period = 0), sumIf(n, period = 1)
            FROM {sql} AS both
            GROUP BY {group}
            ORDER BY abs(toInt64(cur) - toInt64(base)) DESC, cur DESC
            LIMIT {int(limit)}
            """,
            params, label=f"explain-{dim}",
        )
        return dim, res.result_rows or []

    intervals = await expected_intervals(db, vis)
    results = await asyncio.gather(
        totals_q,
        window_coverage(win.start, win.end, f, vis, label="explain-coverage", intervals=intervals),
        window_coverage(base_win.start, base_win.end, f, vis, label="explain-baseline-coverage",
                        intervals=intervals),
        *(rank(d) for d in dims),
    )
    totals_res, cur_cov, base_cov, ranked = results[0], results[1], results[2], results[3:]
    t = (totals_res.result_rows or [(0, 0, 0, 0)])[0]
    cur_total, base_total = int(t[0] or 0), int(t[1] or 0)
    cur_flows, base_flows = int(t[2] or 0), int(t[3] or 0)
    total_delta = cur_total - base_total
    delta_pct, reason = compare_delta(
        cur_total, base_total, prior_limited=base_limited,
        prior_coverage_pct=base_cov["coverage_pct"], cur_coverage_pct=None,
    )
    if base_limited:
        meta.warn("baseline_retention", "Part of the baseline period is older than the data kept; "
                                        "its totals are incomplete and no change percentage is stated")
    if base_cov["coverage_pct"] is not None and base_cov["coverage_pct"] < 50:
        meta.warn("baseline_low_coverage",
                  f"Records were received in only {base_cov['coverage_pct']}% of the baseline's minutes; "
                  "part of the change may be collection gaps rather than traffic")

    owners: dict = {}
    if "exporter" in dims and db is not None:
        try:
            owners = await resolve_exporter_owners(db)
        except Exception:  # names are a nicety; never fail the explanation for them
            owners = {}
    out_dims = []
    for dim, rows in ranked:
        label, keys, _ = specs[dim]
        width = len(keys)
        items = []
        for r in rows:
            values = list(r[:width])
            cur_b, base_b, cur_n, base_n = (int(r[width + i] or 0) for i in range(4))
            name, patch = _label(dim, values, owners)
            items.append({"key": " / ".join(str(v) for v in values), "label": name, "filter": patch,
                          **contributor(values, cur_b, base_b, cur_n, base_n, total_delta, cur_total)})
        listed = sum(i["delta_bytes"] for i in items)
        out_dims.append({
            "dimension": dim,
            "label": label,
            "items": items,
            "explained_pct": round(listed / total_delta * 100, 1) if total_delta else None,
            "other_delta_bytes": total_delta - listed,
        })
    return {
        "baseline": baseline,
        "window": {"from": _to_iso_utc(win.start), "to": _to_iso_utc(win.end),
                   "effective_from": _to_iso_utc(cur_plan.effective_from),
                   "effective_to": _to_iso_utc(cur_plan.effective_to), "source": cur_plan.source},
        "baseline_window": {"from": _to_iso_utc(base_win.start), "to": _to_iso_utc(base_win.end),
                            "effective_from": _to_iso_utc(base_plan.effective_from),
                            "effective_to": _to_iso_utc(base_plan.effective_to), "source": base_plan.source,
                            "retention_limited": base_limited},
        "totals": {
            "current_bytes": cur_total, "baseline_bytes": base_total,
            "current_flows": cur_flows, "baseline_flows": base_flows,
            "delta_bytes": total_delta, "delta_pct": delta_pct, "delta_reason": reason,
        },
        "coverage": {"current": cur_cov, "baseline": base_cov},
        "dimensions": out_dims,
        "filters": f.canonical(),
        "method": "Items ranked by absolute byte change between the two periods, same filter in both",
    }


def _dims_param(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [d.strip() for d in value.split(",") if d.strip()]


@router.get("/explain")
async def netflow_explain(
    win: Window = Depends(window_params(24)),
    f: FlowFilter = Depends(flow_filter),
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
    baseline: str = Query(default="previous_equal", description=" | ".join(BASELINES)),
    limit: int = Query(default=DEFAULT_LIMIT, description=f"Items per dimension (1..{MAX_LIMIT})"),
    dimensions: str | None = Query(default=None, description="Comma-separated subset of " + ", ".join(DIMENSION_KEYS)),
):
    """What changed between the window and a baseline period, per dimension."""
    return await explain_change(win, f, vis, db, baseline=baseline, limit=limit, dimensions=_dims_param(dimensions))


class ExplainRequest(BaseModel):
    """The explainer question as JSON: window (relative ``hours``/``minutes`` or
    fixed ``from``/``to``), ``tz``, the canonical filter keys (contract §8) and
    the baseline."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    hours: int | None = None
    minutes: int | None = None
    from_ts: str | None = Field(default=None, alias="from")
    to_ts: str | None = Field(default=None, alias="to")
    tz: str | None = None
    filter: dict[str, Any] = Field(default_factory=dict)
    baseline: str = "previous_equal"
    limit: int = DEFAULT_LIMIT
    dimensions: list[str] | None = None


_BODY_FILTER_KEYS = frozenset(FLOW_FILTER_KEYS) | frozenset(FILTER_ALIASES)


def _filter_query(values: dict[str, Any], tz: str | None) -> dict[str, str]:
    """The body's filter object as query-string values for ``parse_flow_filter``.

    The body is the canonical §8 filter, so a key outside it (a typo such as
    ``protocl``) is a 400 naming the key -- never silently dropped, which would
    explain all traffic in scope as if it were the filtered slice."""
    unknown = sorted(str(k) for k in values if k not in _BODY_FILTER_KEYS)
    if unknown:
        names = ", ".join(f"'{k[:40]}'" for k in unknown[:5])
        raise invalid_filter(f"Unknown filter key {names} (expected one of: {', '.join(FLOW_FILTER_KEYS)})",
                             {k[:40]: "Unknown filter key" for k in unknown[:5]})
    out: dict[str, str] = {}
    for k, v in values.items():
        if v is None or v == "":
            continue
        if isinstance(v, (list, tuple)):
            out[k] = ",".join(str(x) for x in v)
        elif isinstance(v, bool):
            out[k] = "1" if v else "0"
        else:
            out[k] = str(v)
    if tz and "tz" not in out:
        out["tz"] = tz
    return out


@router.post("/explain")
async def netflow_explain_post(
    body: ExplainRequest,
    vis: FlowVisibility = Depends(flow_visibility),
    db: AsyncSession = Depends(get_db),
):
    """``GET /explain`` with the question in a JSON body."""
    await netflow_classify.load_rules()
    await netflow_groups.load_groups()
    f = parse_flow_filter(_filter_query(body.filter, body.tz))
    if f.count == "edge":
        from dataclasses import replace

        f = replace(f, counting=await load_counting_policy(db))
    await warn_exporter_notes(f, vis, db)   # the same notices as GET /explain (X-28, X-08)
    current_meta().filter = f
    win = resolve_window(body.hours, body.minutes, body.from_ts, body.to_ts, body.tz or f.tz,
                         max_span=MAX_SPAN_ROLLUP)
    return await explain_change(win, f, vis, db, baseline=body.baseline, limit=body.limit,
                                dimensions=body.dimensions)


__all__ = ["router", "explain_change", "prefix_time_params", "contributor", "DIMENSION_KEYS", "ExplainRequest"]
