"""Fleet-wide link / interface utilization — SNMP traffic with optional NetFlow overlay."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user
from app.models.user import User

logger = logging.getLogger("zenplus.link_utilization")

router = APIRouter(prefix="/link-utilization", tags=["Link Utilization"])


def _clean_ip(ip: str | None) -> str:
    if not ip:
        return ""
    return str(ip).split("/")[0]


def _naive_utc(dt: datetime) -> datetime:
    """ClickHouse stores naive UTC; strip the tzinfo FastAPI parsed."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _window_filter(
    hours: int, from_time: datetime | None, to_time: datetime | None, params: dict,
) -> str:
    """ClickHouse WHERE fragment for the active window, filling `params`.

    Presets stay relative (now() - N hours) so auto-refresh tracks the clock.
    An explicit from/to pins the window — previously both endpoints ignored it,
    so the Custom range picker silently showed "the last N hours" instead of
    the chosen window.
    """
    if from_time is not None and to_time is not None:
        params["from_ts"] = _naive_utc(from_time)
        params["to_ts"] = _naive_utc(to_time)
        return "timestamp >= %(from_ts)s AND timestamp < %(to_ts)s"
    params["hours"] = hours
    return "timestamp >= now() - INTERVAL %(hours)s HOUR"


def _bucket_seconds(hours: int) -> int:
    if hours <= 6:
        return 0
    if hours <= 24:
        return 300
    if hours <= 24 * 7:
        return 1800
    return 7200


def _effective_speed(row: dict) -> int:
    manual = row.get("configured_speed_bps")
    if manual and int(manual) > 0:
        return int(manual)
    return int(row.get("if_speed") or 0)


def _util_pct(in_bps: float, out_bps: float, speed: int) -> float | None:
    if speed <= 0:
        return None
    return round(max(in_bps, out_bps) / speed * 100.0, 1)


# ── Interface health: errors, discards, stability ────────────────────────────
#
# ifInErrors/ifOutErrors/ifInDiscards/ifOutDiscards and the packet counters are
# CUMULATIVE SNMP counters, so the increase over a window — not the reading —
# is the number of events.
#
# `max(x) - min(x)` is exact while the counter rises monotonically, which is
# almost always. It breaks when an agent restart or 32-bit wrap resets the
# counter to zero: max-min then charges the whole pre-reset peak as new errors.
# One interface in the field read 23,956 errors that way against 3,393 real ones.
#
# The reset-safe answer is to sum only the positive step between consecutive
# samples, but that needs `lagInFrame`, whose PARTITION BY ... ORDER BY sort
# costs ~4.3s across an 18M-row 7-day fleet scan versus 0.3s for plain
# aggregates. So this runs in two tiers:
#
#   Pass 1 (always, cheap): plain aggregates for every interface, plus two
#     flags — `has_reset` (some sample sits below the first one, which a
#     monotonic counter can never do) and `maybe_flap` (more than one distinct
#     oper_status, so at least one transition happened).
#   Pass 2 (only for flagged interfaces): the precise windowed pass.
#
# In the field that is ~70 of 3,174 interfaces, so the pair costs ~0.35s and
# returns exactly what the expensive query would have.

_COUNTER_COLUMNS = (
    ("in_errors", "ie"),
    ("out_errors", "oe"),
    ("in_discards", "id"),
    ("out_discards", "od"),
    ("in_ucast_pkts", "ip"),
    ("out_ucast_pkts", "op"),
)


def _health_cheap_columns() -> str:
    """Aggregate-only health columns — exact unless a counter reset (flagged)."""
    deltas = ",\n                   ".join(
        f"max({col}) - min({col}) AS d_{alias}" for col, alias in _COUNTER_COLUMNS
    )
    # A monotonic counter's first sample is also its smallest. Anything else
    # means the counter went backwards somewhere in the window.
    resets = "\n                    OR ".join(
        f"(argMin({col}, timestamp) != min({col}))" for col, _ in _COUNTER_COLUMNS
    )
    return f"""{deltas},
                   uniqExact(oper_status) > 1 AS maybe_flap,
                   avg(oper_status) * 100 AS availability,
                   ({resets}) AS has_reset"""


def _health_precise_sql(window: str, pairs_sql: str) -> str:
    """Reset-safe deltas + exact flap counts, scoped to the flagged interfaces.

    row_number() = 1 has no predecessor (lagInFrame yields 0 there, which would
    score the entire counter as one delta), so every delta is gated on rn > 1.
    """
    lags = ",\n                       ".join(
        f"lagInFrame({col}) OVER w AS p_{alias}" for col, alias in _COUNTER_COLUMNS
    )
    deltas = ",\n                   ".join(
        f"sum(if(rn > 1, greatest(toInt64({col}) - toInt64(p_{alias}), 0), 0)) AS d_{alias}"
        for col, alias in _COUNTER_COLUMNS
    )
    cols = ", ".join(col for col, _ in _COUNTER_COLUMNS)
    return f"""
        SELECT device_id, if_index,
               {deltas},
               countIf(rn > 1 AND oper_status != p_os) AS flaps
        FROM (
            SELECT device_id, if_index, timestamp, oper_status, {cols},
                   {lags},
                   lagInFrame(oper_status) OVER w AS p_os,
                   row_number() OVER w AS rn
            FROM zenplus.snmp_if_metrics
            WHERE {window} AND (device_id, if_index) IN ({pairs_sql})
            WINDOW w AS (PARTITION BY device_id, if_index ORDER BY timestamp)
        )
        GROUP BY device_id, if_index
    """


def _pairs_sql(pairs: list[tuple[str, int]]) -> str:
    """Inline (uuid, ifindex) tuple list. Both halves are re-typed from values
    ClickHouse itself returned, so there is nothing to inject."""
    return ",".join(f"(toUUID('{UUID(d)}'),{int(i)})" for d, i in pairs)


def _ppm(events: int, packets: int) -> float | None:
    """Events per million packets. None when the device reports no packet
    counters — a bare count with no denominator can't be judged."""
    if packets <= 0:
        return None
    return round(events * 1_000_000.0 / packets, 2)


# Error/discard severity is a RATE, not a count. 517 errors against 256M packets
# (2 ppm) is a healthy link; 517 against 50k packets is a broken one. Thresholds
# follow the usual operational rule of thumb — 0.1% of frames erroring is a
# fault, 0.01% is worth watching. Discards tolerate more: a policed or congested
# port drops by design, so only a sustained 1% counts as critical.
_ERR_PPM_CRITICAL = 1_000     # 0.1 %
_ERR_PPM_WARNING = 100        # 0.01 %
_DISCARD_PPM_CRITICAL = 10_000  # 1 %
_DISCARD_PPM_WARNING = 1_000    # 0.1 %
# Without packet counters there is no denominator; fall back to an absolute
# floor high enough that a handful of stray events doesn't flag every port.
_ERR_ABS_FLOOR = 1_000
_FLAP_CRITICAL = 5


def _link_health(errors: int, discards: int, err_ppm: float | None,
                 disc_ppm: float | None, flaps: int) -> tuple[str, list[str]]:
    """Classify a link as ok/warning/critical and name the reasons.

    The reason list drives the UI badges, so it stays empty for healthy links
    rather than reporting "0 errors" as a finding.
    """
    issues: list[str] = []
    severity = "ok"

    def _raise(level: str) -> None:
        nonlocal severity
        if level == "critical" or (level == "warning" and severity == "ok"):
            severity = level

    if errors > 0:
        if err_ppm is not None:
            if err_ppm >= _ERR_PPM_CRITICAL:
                issues.append("errors"); _raise("critical")
            elif err_ppm >= _ERR_PPM_WARNING:
                issues.append("errors"); _raise("warning")
        elif errors >= _ERR_ABS_FLOOR:
            issues.append("errors"); _raise("warning")
    if discards > 0:
        if disc_ppm is not None:
            if disc_ppm >= _DISCARD_PPM_CRITICAL:
                issues.append("discards"); _raise("critical")
            elif disc_ppm >= _DISCARD_PPM_WARNING:
                issues.append("discards"); _raise("warning")
        elif discards >= _ERR_ABS_FLOOR:
            issues.append("discards"); _raise("warning")
    if flaps >= _FLAP_CRITICAL:
        issues.append("flapping"); _raise("critical")
    elif flaps > 0:
        issues.append("flapping"); _raise("warning")

    return severity, issues


def _health_payload(d: dict, window_seconds: float) -> dict:
    """Build the per-link health block from one row of delta aggregates."""
    in_err, out_err = int(d.get("d_ie", 0)), int(d.get("d_oe", 0))
    in_disc, out_disc = int(d.get("d_id", 0)), int(d.get("d_od", 0))
    in_pkts, out_pkts = int(d.get("d_ip", 0)), int(d.get("d_op", 0))
    flaps = int(d.get("flaps", 0))

    errors = in_err + out_err
    discards = in_disc + out_disc
    # Errored/discarded frames never reached the ucast counter, so add them back
    # to get the attempted-frame denominator.
    in_total = in_pkts + in_err + in_disc
    out_total = out_pkts + out_err + out_disc
    err_ppm = _ppm(errors, in_total + out_total)
    disc_ppm = _ppm(discards, in_total + out_total)
    severity, issues = _link_health(errors, discards, err_ppm, disc_ppm, flaps)

    secs = max(window_seconds, 1.0)
    return {
        "in_errors": in_err,
        "out_errors": out_err,
        "in_discards": in_disc,
        "out_discards": out_disc,
        "errors": errors,
        "discards": discards,
        "in_pkts": in_pkts,
        "out_pkts": out_pkts,
        "in_pps": round(in_pkts / secs, 1),
        "out_pps": round(out_pkts / secs, 1),
        "error_ppm": err_ppm,
        "discard_ppm": disc_ppm,
        "flaps": flaps,
        "availability_pct": round(float(d.get("availability") or 0), 1),
        "health": severity,
        "issues": issues,
    }


async def _favorite_keys(db: AsyncSession, user_id) -> set[tuple[str, int]]:
    """(device_id, if_index) pairs this user has starred.

    Favourites are a display concern, so a missing table — an appliance that
    hasn't run migration 067 yet — degrades to "nobody has favourites" rather
    than failing the page.
    """
    try:
        rows = (await db.execute(text("""
            SELECT device_id::text, if_index FROM link_favorites WHERE user_id = :uid
        """), {"uid": str(user_id)})).all()
    except Exception:
        logger.exception("link favourites lookup failed")
        return set()
    return {(r[0], int(r[1])) for r in rows}


@router.get("")
async def list_links(
    hours: int = Query(default=24, ge=1, le=720),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=200, ge=1, le=500),
    search: str | None = Query(default=None),
    status: str | None = Query(default=None, pattern="^(up|down)$"),
    favorites_only: bool = Query(
        default=False, description="Only links this user has starred"
    ),
    min_util: float | None = Query(default=None, ge=0, le=200),
    issue: str | None = Query(
        default=None,
        pattern="^(any|errors|discards|flapping)$",
        description="Only links with this health problem",
    ),
    sort: str = Query(
        default="util",
        pattern="^(util|peak|traffic|in|out|name|errors|discards|error_rate|flaps)$",
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Rank monitored interfaces by SNMP utilization and health across the fleet."""
    from app.core.database import get_clickhouse_client

    client = get_clickhouse_client()

    # Pass 1: bps stats + aggregate-only health for every interface (see the
    # two-tier note above _health_cheap_columns).
    ch_params: dict = {}
    window = _window_filter(hours, from_time, to_time, ch_params)
    try:
        ch = client.query(
            f"""
            SELECT
                device_id,
                if_index,
                avg(in_bps) AS avg_in,
                avg(out_bps) AS avg_out,
                max(in_bps) AS max_in,
                max(out_bps) AS max_out,
                argMax(in_bps, timestamp) AS cur_in,
                argMax(out_bps, timestamp) AS cur_out,
                -- Must NOT be aliased `oper_status`: ClickHouse would resolve
                -- the oper_status references inside the health columns to this
                -- alias and reject them as nested aggregates.
                argMax(oper_status, timestamp) AS cur_oper,
                {_health_cheap_columns()},
                min(timestamp) AS first_ts,
                max(timestamp) AS last_ts
            FROM zenplus.snmp_if_metrics
            WHERE {window}
            GROUP BY device_id, if_index
            """,
            parameters=ch_params,
        )
    except Exception as e:
        raise HTTPException(500, f"clickhouse query failed: {e}")

    # Both passes read by column name: the health column lists are generated,
    # so positional indexing silently shifts every time one is added.
    raw: dict[tuple[str, int], dict] = {}
    needs_precise: list[tuple[str, int]] = []
    for r in ch.named_results():
        did, idx = str(r["device_id"]), int(r["if_index"])
        if r["maybe_flap"] or r["has_reset"]:
            needs_precise.append((did, idx))
        raw[(did, idx)] = {
            "bps": (
                float(r["avg_in"] or 0), float(r["avg_out"] or 0),
                float(r["max_in"] or 0), float(r["max_out"] or 0),
                float(r["cur_in"] or 0), float(r["cur_out"] or 0),
            ),
            "oper_status_ch": int(r["cur_oper"] or 0),
            **{f"d_{a}": r[f"d_{a}"] for _, a in _COUNTER_COLUMNS},
            # A single distinct oper_status over the window means no transition.
            "flaps": 0,
            "availability": r["availability"],
            "first_ts": r["first_ts"], "last_ts": r["last_ts"],
        }

    # Pass 2: exact deltas + flap counts for the few flagged interfaces.
    if needs_precise:
        try:
            precise = client.query(
                _health_precise_sql(window, _pairs_sql(needs_precise)),
                parameters=ch_params,
            )
            for r in precise.named_results():
                entry = raw.get((str(r["device_id"]), int(r["if_index"])))
                if entry is None:
                    continue
                entry.update({f"d_{a}": r[f"d_{a}"] for _, a in _COUNTER_COLUMNS})
                entry["flaps"] = int(r["flaps"] or 0)
        except Exception:
            # Falling back to pass-1 numbers is better than failing the page:
            # they're right for every interface without a counter reset, and
            # flap counts stay at 0 rather than becoming wrong.
            logger.exception("precise interface health pass failed")

    metrics: dict[tuple[str, int], dict] = {}
    for key, d in raw.items():
        avg_in, avg_out, max_in, max_out, cur_in, cur_out = d["bps"]
        # Rates are per-second over the interface's own observed span, not the
        # nominal window — an interface that only started reporting an hour ago
        # would otherwise read as near-idle across a 24h window.
        first_ts, last_ts = d["first_ts"], d["last_ts"]
        span = (last_ts - first_ts).total_seconds() if first_ts and last_ts else 0.0
        metrics[key] = {
            "avg_in_bps": avg_in,
            "avg_out_bps": avg_out,
            "max_in_bps": max_in,
            "max_out_bps": max_out,
            "in_bps": cur_in,
            "out_bps": cur_out,
            "oper_status_ch": d["oper_status_ch"],
            "health": _health_payload(d, span),
        }

    favorites = await _favorite_keys(db, user.id)

    if not metrics:
        # `total_favorites` still reports the user's stars: the fleet having no
        # samples in this window doesn't mean they have no favourites, and the
        # UI's empty state says different things for the two cases.
        return {
            "items": [],
            "summary": {
                "total": 0, "high_util": 0, "with_netflow": 0, "avg_util": None,
                "favorites": 0, "total_favorites": len(favorites),
            },
        }

    # Inventory from Postgres.
    #
    # Scope by device, not by (device, interface) pair. Enumerating every pair
    # meant one placeholder per interface — ~6.3k bound parameters for a 35
    # device fleet — and the device_id::text cast blocked the index, costing
    # 3.2s of a 3.6s response. Filtering on the ~35 device UUIDs is 0.05s; the
    # extra interfaces it returns are dropped by the metrics lookup below.
    device_ids = sorted({k[0] for k in metrics})

    inv_rows = (await db.execute(text("""
        SELECT di.device_id::text, di.if_index, di.if_name, di.if_descr, di.if_alias,
               di.if_speed, di.configured_speed_bps, di.oper_status, di.admin_status,
               di.monitored, d.hostname, d.ip_address::text, d.id::text
        FROM device_interfaces di
        JOIN devices d ON d.id = di.device_id
        WHERE di.device_id = ANY(CAST(:device_ids AS uuid[]))
          AND di.monitored = TRUE
    """), {"device_ids": device_ids})).all()

    # NetFlow-enabled interfaces in the same window.
    nf_set: set[tuple[str, int]] = set()
    nf_params: dict = {}
    nf_window = _window_filter(hours, from_time, to_time, nf_params)
    try:
        nf = client.query(
            f"""
            SELECT DISTINCT toString(exporter_ip) AS ip, ifindex
            FROM (
                SELECT exporter_ip, input_snmp AS ifindex
                FROM zenplus.flow_records
                WHERE {nf_window} AND input_snmp != 0
                UNION ALL
                SELECT exporter_ip, output_snmp AS ifindex
                FROM zenplus.flow_records
                WHERE {nf_window} AND output_snmp != 0
            )
            """,
            parameters=nf_params,
        )
        for r in nf.result_rows:
            nf_set.add((str(r[0]), int(r[1])))
    except Exception:
        logger.exception("netflow interface lookup failed")

    items = []
    for row in inv_rows:
        did, idx = row[0], int(row[1])
        m = metrics.get((did, idx))
        # Device-scoped inventory also returns interfaces with no samples in
        # the window; they cannot be ranked, so leave them out as before.
        if m is None:
            continue
        speed = _effective_speed({"configured_speed_bps": row[6], "if_speed": row[5]})
        in_bps = m.get("in_bps", 0.0)
        out_bps = m.get("out_bps", 0.0)
        util = _util_pct(in_bps, out_bps, speed)
        peak_util = _util_pct(m.get("max_in_bps", 0), m.get("max_out_bps", 0), speed)
        oper = row[7] or ("up" if m.get("oper_status_ch") == 1 else "down")
        # SPAN/mirror destination ports (and some LAG members) report
        # ifOperStatus=down while forwarding real traffic — Cisco marks SPAN
        # destinations "down (monitoring)". Moving counters are ground truth:
        # a genuinely down port cannot produce a non-zero rate in its latest
        # sample, so present these as up instead of the contradictory
        # "Down + 240 Mbps".
        oper_unreliable = False
        if oper != "up" and (in_bps > 0 or out_bps > 0):
            oper = "up"
            # Everything derived from ifOperStatus — availability, flap count —
            # is meaningless on a port whose agent reports "down" while it
            # forwards. Flag it so the UI doesn't show a contradictory
            # "Up · 0% available" instead of hiding the number.
            oper_unreliable = True
        # ip_address is inet, so ::text yields "192.168.100.102/32", but the
        # flow exporter IPs from ClickHouse carry no prefix. Comparing the raw
        # values never matched, so has_netflow was always False.
        ip = _clean_ip(row[11])
        has_nf = (ip, idx) in nf_set
        health = m.get("health") or {}
        # An administratively shut port isn't a fault — don't let its flap
        # count or stale counters read as a health problem.
        if (row[8] or "").lower() == "down":
            health = {**health, "health": "ok", "issues": []}
        if oper_unreliable:
            health = {
                **health,
                "oper_status_reliable": False,
                "availability_pct": None,
                "flaps": 0,
                "issues": [i for i in health.get("issues", []) if i != "flapping"],
            }
            if not health["issues"]:
                health["health"] = "ok"
        else:
            health = {**health, "oper_status_reliable": True}

        item = {
            "device_id": did,
            "hostname": row[10],
            "device_ip": ip,
            "if_index": idx,
            "if_name": row[2],
            "if_descr": row[3],
            "if_alias": row[4],
            "if_speed": row[5],
            "configured_speed_bps": row[6],
            "effective_speed_bps": speed,
            "oper_status": oper,
            "admin_status": row[8],
            "in_bps": in_bps,
            "out_bps": out_bps,
            "total_bps": in_bps + out_bps,
            "avg_in_bps": m.get("avg_in_bps", 0),
            "avg_out_bps": m.get("avg_out_bps", 0),
            "max_in_bps": m.get("max_in_bps", 0),
            "max_out_bps": m.get("max_out_bps", 0),
            "util_pct": util,
            "peak_util_pct": peak_util,
            "has_netflow": has_nf,
            "is_favorite": (did, idx) in favorites,
            **health,
        }
        items.append(item)

    # Filters
    if favorites_only:
        items = [i for i in items if i["is_favorite"]]
    q = (search or "").strip().lower()
    if q:
        items = [
            i for i in items
            if q in (i["hostname"] or "").lower()
            or q in (i["if_name"] or "").lower()
            or q in (i["if_descr"] or "").lower()
            or q in (i["if_alias"] or "").lower()
            or q in (i["device_ip"] or "").lower()
        ]
    if status == "up":
        items = [i for i in items if i["oper_status"] == "up"]
    elif status == "down":
        items = [i for i in items if i["oper_status"] != "up"]
    if min_util is not None:
        # Filter on the window's peak, not the instantaneous sample: a bursty
        # uplink that hit 64% five minutes ago but idles at 4% right now is
        # exactly what a ">= 50%" filter is trying to find.
        items = [
            i for i in items
            if max(i["util_pct"] or 0, i["peak_util_pct"] or 0) >= min_util
        ]
    if issue == "any":
        items = [i for i in items if i.get("issues")]
    elif issue:
        items = [i for i in items if issue in (i.get("issues") or [])]

    # Sort
    if sort == "name":
        items.sort(key=lambda x: (x["hostname"] or "", x["if_name"] or ""))
    elif sort == "in":
        items.sort(key=lambda x: x["in_bps"], reverse=True)
    elif sort == "out":
        items.sort(key=lambda x: x["out_bps"], reverse=True)
    elif sort == "traffic":
        items.sort(key=lambda x: x["total_bps"], reverse=True)
    elif sort == "peak":
        items.sort(key=lambda x: (x["peak_util_pct"] or 0), reverse=True)
    elif sort == "errors":
        items.sort(key=lambda x: x.get("errors") or 0, reverse=True)
    elif sort == "discards":
        items.sort(key=lambda x: x.get("discards") or 0, reverse=True)
    elif sort == "flaps":
        items.sort(key=lambda x: x.get("flaps") or 0, reverse=True)
    elif sort == "error_rate":
        # Links with no packet counters have no rate; sort them last rather
        # than letting a None rank above a measured 900 ppm.
        items.sort(key=lambda x: x.get("error_ppm") or -1, reverse=True)
    else:
        items.sort(key=lambda x: (x["util_pct"] or 0), reverse=True)

    # Favourites float to the top of whatever the chosen sort produced. Python's
    # sort is stable, so this only lifts the starred links — their order among
    # themselves, and everything below them, still follows `sort`. Pinning runs
    # after the filters, so a favourite the user filtered out stays out.
    items.sort(key=lambda x: not x["is_favorite"])

    # Summarise the whole filtered set, not just the page. These drive KPIs
    # labelled "Fleet avg util" and "interfaces with flow data"; computing them
    # after the cut reported the top-N slice as if it were the fleet.
    utils = [i["util_pct"] for i in items if i["util_pct"] is not None]
    summary = {
        "total": len(items),
        "high_util": sum(1 for u in utils if u >= 80),
        "warning_util": sum(1 for u in utils if 50 <= u < 80),
        "with_netflow": sum(1 for i in items if i["has_netflow"]),
        # Favourites still reporting in this window. `total_favorites` counts
        # every star the user holds, so the filter chip doesn't read "0" when
        # a favoured link simply went quiet.
        "favorites": sum(1 for i in items if i["is_favorite"]),
        "total_favorites": len(favorites),
        "avg_util": round(sum(utils) / len(utils), 1) if utils else None,
        "returned": min(len(items), limit),
        "with_errors": sum(1 for i in items if "errors" in (i.get("issues") or [])),
        "with_discards": sum(1 for i in items if "discards" in (i.get("issues") or [])),
        "flapping": sum(1 for i in items if "flapping" in (i.get("issues") or [])),
        "critical_health": sum(1 for i in items if i.get("health") == "critical"),
        "unhealthy": sum(1 for i in items if i.get("issues")),
        "total_errors": sum(i.get("errors") or 0 for i in items),
        "total_discards": sum(i.get("discards") or 0 for i in items),
    }

    items = items[:limit]
    return {"items": items, "summary": summary, "hours": hours}


# ── Favourites ───────────────────────────────────────────────────────────────
#
# Declared before the /{device_id}/{if_index} drill-down: the paths differ in
# segment count so FastAPI would not confuse them, but keeping the literal route
# first makes that independent of declaration order.


@router.get("/favorites")
async def list_favorites(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """This user's starred links, newest first.

    Joined against the live inventory so a stale star — an interface that
    discovery has since removed — is reported with `exists: false` rather than
    as a nameless row.
    """
    rows = (await db.execute(text("""
        SELECT f.device_id::text, f.if_index, f.created_at,
               d.hostname, di.if_name, di.if_descr, di.if_alias
        FROM link_favorites f
        JOIN devices d ON d.id = f.device_id
        LEFT JOIN device_interfaces di
               ON di.device_id = f.device_id AND di.if_index = f.if_index
        WHERE f.user_id = :uid
        ORDER BY f.created_at DESC
    """), {"uid": str(user.id)})).all()
    return {
        "items": [
            {
                "device_id": r[0],
                "if_index": int(r[1]),
                "created_at": r[2],
                "hostname": r[3],
                "if_name": r[4],
                "if_descr": r[5],
                "if_alias": r[6],
                "exists": r[4] is not None or r[5] is not None,
            }
            for r in rows
        ]
    }


@router.put("/favorites/{device_id}/{if_index}", status_code=204)
async def add_favorite(
    device_id: UUID,
    if_index: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Star a link. Idempotent — re-starring keeps the original timestamp."""
    known = (await db.execute(text("""
        SELECT 1 FROM device_interfaces WHERE device_id = :did AND if_index = :idx
    """), {"did": device_id, "idx": if_index})).first()
    if not known:
        raise HTTPException(404, "interface not found")
    await db.execute(text("""
        INSERT INTO link_favorites (user_id, device_id, if_index)
        VALUES (:uid, :did, :idx)
        ON CONFLICT (user_id, device_id, if_index) DO NOTHING
    """), {"uid": str(user.id), "did": device_id, "idx": if_index})
    await db.commit()


@router.delete("/favorites/{device_id}/{if_index}", status_code=204)
async def remove_favorite(
    device_id: UUID,
    if_index: int,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Unstar a link. Succeeds whether or not it was starred, so the UI toggle
    can't get stuck on a favourite that another tab already cleared."""
    await db.execute(text("""
        DELETE FROM link_favorites
        WHERE user_id = :uid AND device_id = :did AND if_index = :idx
    """), {"uid": str(user.id), "did": device_id, "idx": if_index})
    await db.commit()


@router.get("/{device_id}/{if_index}")
async def link_detail(
    device_id: UUID,
    if_index: int,
    hours: int = Query(default=24, ge=1, le=720),
    from_time: datetime | None = Query(default=None, alias="from"),
    to_time: datetime | None = Query(default=None, alias="to"),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """Per-link drill-down: SNMP traffic/errors + optional NetFlow overlay."""
    from app.core.database import get_clickhouse_client

    inv = (await db.execute(text("""
        SELECT di.if_name, di.if_descr, di.if_alias, di.if_speed, di.configured_speed_bps,
               di.oper_status, di.admin_status, di.mac_address,
               d.hostname, d.ip_address::text, d.id::text
        FROM device_interfaces di
        JOIN devices d ON d.id = di.device_id
        WHERE di.device_id = :did AND di.if_index = :idx
    """), {"did": device_id, "idx": if_index})).first()
    if not inv:
        raise HTTPException(404, "interface not found")

    speed = _effective_speed({"configured_speed_bps": inv[4], "if_speed": inv[3]})
    client = get_clickhouse_client()
    bucket = _bucket_seconds(hours)
    did = str(device_id)

    params: dict = {"id": did, "if": if_index}
    window = _window_filter(hours, from_time, to_time, params)

    # SNMP traffic series
    if bucket == 0:
        # Raw view still has to difference the counters — plotting the running
        # total would draw a monotonic staircase, not the errors per sample.
        snmp_sql = f"""
            SELECT ts, in_bps, out_bps,
                   if(rn > 1, greatest(toInt64(in_errors) - toInt64(p_ie), 0), 0) AS d_in_err,
                   if(rn > 1, greatest(toInt64(out_errors) - toInt64(p_oe), 0), 0) AS d_out_err,
                   if(rn > 1, greatest(toInt64(in_discards) - toInt64(p_id), 0), 0) AS d_in_disc,
                   if(rn > 1, greatest(toInt64(out_discards) - toInt64(p_od), 0), 0) AS d_out_disc,
                   in_bps AS in_peak, out_bps AS out_peak
            FROM (
                SELECT toUnixTimestamp64Milli(timestamp) AS ts, timestamp,
                       in_bps, out_bps, in_errors, out_errors, in_discards, out_discards,
                       lagInFrame(in_errors) OVER w AS p_ie,
                       lagInFrame(out_errors) OVER w AS p_oe,
                       lagInFrame(in_discards) OVER w AS p_id,
                       lagInFrame(out_discards) OVER w AS p_od,
                       row_number() OVER w AS rn
                FROM zenplus.snmp_if_metrics
                WHERE device_id = %(id)s AND if_index = %(if)s AND {window}
                WINDOW w AS (ORDER BY timestamp)
            )
            ORDER BY ts
        """
    else:
        # Carry the per-bucket peak alongside the average: the chart plots the
        # average, but a summary built from averages hides the real peaks.
        # Errors/discards are cumulative counters, so the in-bucket increase is
        # max-min — summing the counter readings produced nonsense totals.
        # Aliases must not reuse the source column names: `avg(in_bps) AS in_bps`
        # makes a later max(in_bps) resolve to the alias, which ClickHouse
        # rejects as a nested aggregate (ILLEGAL_AGGREGATION).
        snmp_sql = f"""
            SELECT toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {bucket} SECOND)) * 1000 AS ts,
                   avg(in_bps) AS avg_in, avg(out_bps) AS avg_out,
                   sum(if(rn > 1, greatest(toInt64(in_errors) - toInt64(p_ie), 0), 0)) AS d_in_err,
                   sum(if(rn > 1, greatest(toInt64(out_errors) - toInt64(p_oe), 0), 0)) AS d_out_err,
                   sum(if(rn > 1, greatest(toInt64(in_discards) - toInt64(p_id), 0), 0)) AS d_in_disc,
                   sum(if(rn > 1, greatest(toInt64(out_discards) - toInt64(p_od), 0), 0)) AS d_out_disc,
                   max(in_bps) AS peak_in, max(out_bps) AS peak_out
            FROM (
                SELECT timestamp, in_bps, out_bps,
                       in_errors, out_errors, in_discards, out_discards,
                       lagInFrame(in_errors) OVER w AS p_ie,
                       lagInFrame(out_errors) OVER w AS p_oe,
                       lagInFrame(in_discards) OVER w AS p_id,
                       lagInFrame(out_discards) OVER w AS p_od,
                       row_number() OVER w AS rn
                FROM zenplus.snmp_if_metrics
                WHERE device_id = %(id)s AND if_index = %(if)s AND {window}
                WINDOW w AS (ORDER BY timestamp)
            )
            GROUP BY ts ORDER BY ts
        """

    try:
        snmp_res = client.query(snmp_sql, parameters=params)
    except Exception as e:
        raise HTTPException(500, f"snmp query failed: {e}")

    traffic = []
    errors = []
    for r in snmp_res.result_rows:
        traffic.append({
            "ts": int(r[0]),
            "in_bps": float(r[1]), "out_bps": float(r[2]),
            # Per-bucket peak. Equals the average on unbucketed ranges; on wider
            # windows it's what keeps the chart's ceiling honest against IN MAX.
            "in_peak_bps": float(r[7] or 0), "out_peak_bps": float(r[8] or 0),
        })
        errors.append({
            "ts": int(r[0]),
            "in_errors": int(r[3] or 0), "out_errors": int(r[4] or 0),
            "in_discards": int(r[5] or 0), "out_discards": int(r[6] or 0),
        })

    # Summary comes from the raw samples, never from the plotted buckets.
    # Deriving max/current from bucket averages understated this interface's
    # peak by 27% over 24h and disagreed with the fleet table, which aggregates
    # raw. Both now use the same aggregation, so the row and the panel match.
    summary: dict = {}
    try:
        # One interface, so the precise windowed form is cheap — no need for
        # the fleet view's two-tier split.
        deltas = ",\n                   ".join(
            f"sum(if(rn > 1, greatest(toInt64({col}) - toInt64(p_{alias}), 0), 0)) AS d_{alias}"
            for col, alias in _COUNTER_COLUMNS
        )
        lags = ",\n                       ".join(
            f"lagInFrame({col}) OVER w AS p_{alias}" for col, alias in _COUNTER_COLUMNS
        )
        cols = ", ".join(col for col, _ in _COUNTER_COLUMNS)
        agg = client.query(
            f"""
            SELECT avg(in_bps) AS in_avg, max(in_bps) AS in_max,
                   argMax(in_bps, timestamp) AS in_cur,
                   avg(out_bps) AS out_avg, max(out_bps) AS out_max,
                   argMax(out_bps, timestamp) AS out_cur,
                   {deltas},
                   countIf(rn > 1 AND oper_status != p_os) AS flaps,
                   avg(oper_status) * 100 AS availability,
                   count() AS samples, min(timestamp) AS first_ts, max(timestamp) AS last_ts
            FROM (
                SELECT timestamp, in_bps, out_bps, oper_status, {cols},
                       {lags},
                       lagInFrame(oper_status) OVER w AS p_os,
                       row_number() OVER w AS rn
                FROM zenplus.snmp_if_metrics
                WHERE device_id = %(id)s AND if_index = %(if)s AND {window}
                WINDOW w AS (ORDER BY timestamp)
            )
            """,
            parameters=params,
        )
        rows = list(agg.named_results())
        row = rows[0] if rows else None
    except Exception as e:
        raise HTTPException(500, f"snmp summary query failed: {e}")

    health: dict = {}
    if row and int(row["samples"] or 0) > 0:
        in_avg, in_max = float(row["in_avg"] or 0), float(row["in_max"] or 0)
        in_cur = float(row["in_cur"] or 0)
        out_avg, out_max = float(row["out_avg"] or 0), float(row["out_max"] or 0)
        out_cur = float(row["out_cur"] or 0)
        first_ts, last_ts = row["first_ts"], row["last_ts"]
        span = (last_ts - first_ts).total_seconds() if first_ts and last_ts else 0.0
        health = _health_payload(row, span)
        if (inv[6] or "").lower() == "down":  # admin-down is not a fault
            health = {**health, "health": "ok", "issues": []}
        summary = {
            "in_avg_bps": in_avg,
            "in_max_bps": in_max,
            "in_current_bps": in_cur,
            "out_avg_bps": out_avg,
            "out_max_bps": out_max,
            "out_current_bps": out_cur,
            "util_pct": _util_pct(in_cur, out_cur, speed),
            "peak_util_pct": _util_pct(in_max, out_max, speed),
            "total_errors": health["errors"],
            "total_discards": health["discards"],
            "samples": int(row["samples"]),
            **health,
        }

    # Strip the inet prefix before querying ClickHouse — flow_records stores
    # bare exporter IPs, so "192.168.41.105/32" matched nothing and every
    # interface reported "no flow records".
    exporter_ip = _clean_ip(inv[9])
    netflow = {"has_flows": False, "timeseries": [], "top_talkers": [], "protocols": []}

    if exporter_ip:
        nf_bucket = 60 if hours <= 6 else (300 if hours <= 48 else 1800)
        nf_params: dict = {"ip": exporter_ip, "if": if_index}
        nf_window = _window_filter(hours, from_time, to_time, nf_params)
        try:
            # The inner aliases must not shadow `bytes`: aliasing sum(bytes) AS
            # bytes made the outer sum() resolve to sum(sum(bytes)), which
            # ClickHouse rejects with ILLEGAL_AGGREGATION.
            nf_ts = client.query(
                f"""
                SELECT ts, sum(bucket_in_bps) AS in_bps, sum(bucket_out_bps) AS out_bps,
                       sum(bucket_bytes) AS bytes
                FROM (
                    SELECT toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {nf_bucket} SECOND)) * 1000 AS ts,
                           sum(bytes) * 8.0 / {nf_bucket} AS bucket_in_bps,
                           0 AS bucket_out_bps,
                           sum(bytes) AS bucket_bytes
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND input_snmp = %(if)s
                      AND {nf_window}
                    GROUP BY ts
                    UNION ALL
                    SELECT toUnixTimestamp(toStartOfInterval(timestamp, INTERVAL {nf_bucket} SECOND)) * 1000 AS ts,
                           0 AS bucket_in_bps,
                           sum(bytes) * 8.0 / {nf_bucket} AS bucket_out_bps,
                           sum(bytes) AS bucket_bytes
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND output_snmp = %(if)s
                      AND {nf_window}
                    GROUP BY ts
                )
                GROUP BY ts ORDER BY ts
                """,
                parameters=nf_params,
            )
            ts_map: dict[int, dict] = {}
            for r in nf_ts.result_rows:
                ts = int(r[0])
                ts_map.setdefault(ts, {"ts": ts, "in_bps": 0.0, "out_bps": 0.0, "bytes": 0})
                ts_map[ts]["in_bps"] += float(r[1] or 0)
                ts_map[ts]["out_bps"] += float(r[2] or 0)
                ts_map[ts]["bytes"] += int(r[3] or 0)
            netflow["timeseries"] = sorted(ts_map.values(), key=lambda x: x["ts"])
            netflow["has_flows"] = len(netflow["timeseries"]) > 0

            talkers = client.query(
                f"""
                SELECT ip, sum(bytes) AS total_bytes, sum(packets) AS total_packets, count() AS flows
                FROM (
                    SELECT toString(src_addr) AS ip, bytes, packets
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND (input_snmp = %(if)s OR output_snmp = %(if)s)
                      AND {nf_window}
                    UNION ALL
                    SELECT toString(dst_addr) AS ip, bytes, packets
                    FROM zenplus.flow_records
                    WHERE exporter_ip = %(ip)s
                      AND (input_snmp = %(if)s OR output_snmp = %(if)s)
                      AND {nf_window}
                )
                GROUP BY ip ORDER BY total_bytes DESC LIMIT 10
                """,
                parameters=nf_params,
            )
            netflow["top_talkers"] = [
                {"ip": r[0], "bytes": int(r[1]), "packets": int(r[2]), "flows": int(r[3])}
                for r in talkers.result_rows
            ]

            protos = client.query(
                f"""
                SELECT protocol, sum(bytes) AS total_bytes
                FROM zenplus.flow_records
                WHERE exporter_ip = %(ip)s
                  AND (input_snmp = %(if)s OR output_snmp = %(if)s)
                  AND {nf_window}
                GROUP BY protocol ORDER BY total_bytes DESC LIMIT 8
                """,
                parameters=nf_params,
            )
            netflow["protocols"] = [
                {"protocol": int(r[0]), "bytes": int(r[1])} for r in protos.result_rows
            ]
        except Exception:
            # NetFlow is an optional overlay, so a failure here must not break
            # the SNMP view — but log it. Swallowing silently hid a malformed
            # query that made every interface report "no flow records".
            logger.exception("netflow overlay failed for %s/%s", exporter_ip, if_index)

    # Same SPAN/mirror-port correction as the list endpoint: ifOperStatus says
    # down, but the latest sample carries traffic — the port is forwarding.
    oper_status = inv[5]
    if oper_status != "up" and (
        summary.get("in_current_bps", 0) > 0 or summary.get("out_current_bps", 0) > 0
    ):
        oper_status = "up"
        # See the list endpoint: ifOperStatus-derived stability is meaningless
        # on a port that reports down while forwarding.
        for block in (health, summary):
            if block:
                block["oper_status_reliable"] = False
                block["availability_pct"] = None
                block["flaps"] = 0
                block["issues"] = [i for i in block.get("issues", []) if i != "flapping"]
                if not block["issues"]:
                    block["health"] = "ok"
    else:
        for block in (health, summary):
            if block:
                block["oper_status_reliable"] = True

    return {
        "device_id": did,
        "if_index": if_index,
        "hostname": inv[8],
        "device_ip": exporter_ip,
        "if_name": inv[0],
        "if_descr": inv[1],
        "if_alias": inv[2],
        "if_speed": inv[3],
        "configured_speed_bps": inv[4],
        "effective_speed_bps": speed,
        "oper_status": oper_status,
        "admin_status": inv[6],
        "mac_address": inv[7],
        "hours": hours,
        # Buckets wider than the poll interval mean the plotted line is an
        # average; the client says so rather than presenting it as raw usage.
        "bucket_seconds": bucket,
        "traffic": traffic,
        "errors": errors,
        "summary": summary,
        "health": health,
        "netflow": netflow,
    }
