"""Browser Real User Monitoring (RUM) sections: Core Web Vitals trend, audience
by country, release comparison, slow and failing resources, page-load
breakdown, funnel conversion, user actions and long tasks, and JavaScript
issue status.

Counting follows the RUM pages (app/api/v1/rum.py) exactly:

- a page view counts once (its ``view_start`` record; legacy SDKs send one
  record per view) and sessions and views are keyed by application +
  environment;
- Web Vitals take one finalized, sampled sample per page view
  (rum._per_view_vitals_sql) and are rated against the web.dev thresholds
  (LCP 2.5 s / 4 s, INP 200 ms / 500 ms, CLS 0.1 / 0.25);
- a period that starts before raw-event retention (14 days by default, read
  from the table TTL) is answered from the 5-minute rollup
  ``apm_rum_metrics_5m`` (90 days), whose good / poor shares divide by the
  *rated* samples so buckets written before migrate-106/107 show p75 only.
  RumWindow.rollup decides by window length alone; a report also has to
  handle a short window that lies in the past, so here the start decides.
- Resource, action, funnel, timing and issue detail exists only in raw
  events: those sections cover the retained part of the period and say so.
- client_ip and user ids are never read into a report; resource URLs lose
  their query string, fragment and credentials.

Every section is fleet-scoped, needs apm.view and honours the report's RUM
application filter (ctx.rum_app_clause / ctx.rum_params).
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from sqlalchemy import text

from app.services.report_sections import (
    SectionCtx,
    _fmt_bytes,
    _fmt_ms,
    _section,
    register_section,
    top_n_option,
)
from app.services.report_templates import S, template

logger = logging.getLogger("zenplus.reports")

REQUIRES = ("apm.view",)
CATEGORY = "Applications"
EVENTS = "zenplus.apm_rum_events"
ROLLUP = "zenplus.apm_rum_metrics_5m"
RAW_DAYS_DEFAULT = 14       # apm_rum_events TTL (admin-changeable)
ROLLUP_DAYS_DEFAULT = 90    # apm_rum_metrics_5m TTL (admin-changeable)
MAX_GROUPS = 2000           # safety cap on groups pulled into Python

# A view counts once: its view_start record (legacy SDKs: the only record).
VIEW_ONCE = ("sampled = 1 AND event_type = 'view' "
             "AND (sdk_version = '' OR (is_final = 0 AND end_reason = 'view_start'))")
SESSIONS = "uniqExactIf((application_id, env, session_id), sampled = 1)"
ERROR_SESSIONS = "uniqExactIf((application_id, env, session_id), event_type = 'error' AND sampled = 1)"
VIEWS = f"uniqExactIf((application_id, env, view_id), {VIEW_ONCE})"
# Per-view flag (inside rum._per_view_vitals_sql) marking views that started in scope.
VIEW_STARTED = ("max(raw.event_type = 'view' AND (raw.sdk_version = '' "
                "OR (raw.is_final = 0 AND raw.end_reason = 'view_start'))) AS started")
# Error-group identity, as the RUM error explorer groups (rum._ERROR_FINGERPRINT_SQL).
FINGERPRINT = ("if(error_fingerprint != '', error_fingerprint, "
               "lower(hex(MD5(concat(error_type, ':', error_message)))))")
FAILED = "(status_code >= 400 OR attributes['failed'] = 'true')"

THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcp": (2500.0, 4000.0), "inp": (200.0, 500.0), "cls": (0.1, 0.25)}
VITAL_LABEL = {"lcp": "LCP", "inp": "INP", "cls": "CLS"}
VITAL_CHOICES = [["lcp", "LCP (loading)"], ["inp", "INP (interactivity)"],
                 ["cls", "CLS (visual stability)"]]
THRESHOLD_NOTE = ("Ratings use the web.dev Core Web Vitals thresholds at the 75th percentile: "
                  "LCP good ≤ 2.5 s, poor > 4 s; INP good ≤ 200 ms, poor > 500 ms; "
                  "CLS good ≤ 0.1, poor > 0.25.")
VITALS_METHOD = ("Each page view contributes one finalized sample from sampled sessions, so "
                 "a view reported several times (checkpoints, retries) is counted once.")

_TTL_RE = re.compile(r"toIntervalDay\((\d+)\)|INTERVAL (\d+) DAY")


# ─── Country names (ISO 3166-1 alpha-2) ─────────────────────────────────────

_COUNTRY_TABLE = """
AD Andorra|AE United Arab Emirates|AF Afghanistan|AG Antigua and Barbuda|AI Anguilla|AL Albania|
AM Armenia|AO Angola|AQ Antarctica|AR Argentina|AS American Samoa|AT Austria|AU Australia|AW Aruba|
AX Åland Islands|AZ Azerbaijan|BA Bosnia and Herzegovina|BB Barbados|BD Bangladesh|BE Belgium|
BF Burkina Faso|BG Bulgaria|BH Bahrain|BI Burundi|BJ Benin|BL Saint Barthélemy|BM Bermuda|BN Brunei|
BO Bolivia|BQ Caribbean Netherlands|BR Brazil|BS Bahamas|BT Bhutan|BV Bouvet Island|BW Botswana|
BY Belarus|BZ Belize|CA Canada|CC Cocos (Keeling) Islands|CD DR Congo|CF Central African Republic|
CG Congo|CH Switzerland|CI Côte d'Ivoire|CK Cook Islands|CL Chile|CM Cameroon|CN China|CO Colombia|
CR Costa Rica|CU Cuba|CV Cabo Verde|CW Curaçao|CX Christmas Island|CY Cyprus|CZ Czechia|DE Germany|
DJ Djibouti|DK Denmark|DM Dominica|DO Dominican Republic|DZ Algeria|EC Ecuador|EE Estonia|EG Egypt|
EH Western Sahara|ER Eritrea|ES Spain|ET Ethiopia|FI Finland|FJ Fiji|FK Falkland Islands|
FM Micronesia|FO Faroe Islands|FR France|GA Gabon|GB United Kingdom|GD Grenada|GE Georgia|
GF French Guiana|GG Guernsey|GH Ghana|GI Gibraltar|GL Greenland|GM Gambia|GN Guinea|GP Guadeloupe|
GQ Equatorial Guinea|GR Greece|GS South Georgia|GT Guatemala|GU Guam|GW Guinea-Bissau|GY Guyana|
HK Hong Kong|HM Heard and McDonald Islands|HN Honduras|HR Croatia|HT Haiti|HU Hungary|ID Indonesia|
IE Ireland|IL Israel|IM Isle of Man|IN India|IO British Indian Ocean Territory|IQ Iraq|IR Iran|
IS Iceland|IT Italy|JE Jersey|JM Jamaica|JO Jordan|JP Japan|KE Kenya|KG Kyrgyzstan|KH Cambodia|
KI Kiribati|KM Comoros|KN Saint Kitts and Nevis|KP North Korea|KR South Korea|KW Kuwait|
KY Cayman Islands|KZ Kazakhstan|LA Laos|LB Lebanon|LC Saint Lucia|LI Liechtenstein|LK Sri Lanka|
LR Liberia|LS Lesotho|LT Lithuania|LU Luxembourg|LV Latvia|LY Libya|MA Morocco|MC Monaco|
MD Moldova|ME Montenegro|MF Saint Martin|MG Madagascar|MH Marshall Islands|MK North Macedonia|
ML Mali|MM Myanmar|MN Mongolia|MO Macao|MP Northern Mariana Islands|MQ Martinique|MR Mauritania|
MS Montserrat|MT Malta|MU Mauritius|MV Maldives|MW Malawi|MX Mexico|MY Malaysia|MZ Mozambique|
NA Namibia|NC New Caledonia|NE Niger|NF Norfolk Island|NG Nigeria|NI Nicaragua|NL Netherlands|
NO Norway|NP Nepal|NR Nauru|NU Niue|NZ New Zealand|OM Oman|PA Panama|PE Peru|PF French Polynesia|
PG Papua New Guinea|PH Philippines|PK Pakistan|PL Poland|PM Saint Pierre and Miquelon|
PN Pitcairn Islands|PR Puerto Rico|PS Palestine|PT Portugal|PW Palau|PY Paraguay|QA Qatar|
RE Réunion|RO Romania|RS Serbia|RU Russia|RW Rwanda|SA Saudi Arabia|SB Solomon Islands|
SC Seychelles|SD Sudan|SE Sweden|SG Singapore|SH Saint Helena|SI Slovenia|SJ Svalbard and Jan Mayen|
SK Slovakia|SL Sierra Leone|SM San Marino|SN Senegal|SO Somalia|SR Suriname|SS South Sudan|
ST São Tomé and Príncipe|SV El Salvador|SX Sint Maarten|SY Syria|SZ Eswatini|
TC Turks and Caicos Islands|TD Chad|TF French Southern Territories|TG Togo|TH Thailand|
TJ Tajikistan|TK Tokelau|TL Timor-Leste|TM Turkmenistan|TN Tunisia|TO Tonga|TR Türkiye|
TT Trinidad and Tobago|TV Tuvalu|TW Taiwan|TZ Tanzania|UA Ukraine|UG Uganda|
UM U.S. Outlying Islands|US United States|UY Uruguay|UZ Uzbekistan|VA Vatican City|
VC Saint Vincent and the Grenadines|VE Venezuela|VG British Virgin Islands|VI U.S. Virgin Islands|
VN Vietnam|VU Vanuatu|WF Wallis and Futuna|WS Samoa|XK Kosovo|YE Yemen|YT Mayotte|ZA South Africa|
ZM Zambia|ZW Zimbabwe
"""
COUNTRY_NAMES: dict[str, str] = dict(
    (item.strip()[:2], item.strip()[3:]) for item in _COUNTRY_TABLE.replace("\n", "").split("|")
    if item.strip())


def country_label(code: Any) -> str:
    """'Saudi Arabia (SA)'; an empty code (no GeoIP / CDN country) is 'Unknown'."""
    c = str(code or "").strip().upper()
    if not c:
        return "Unknown"
    name = COUNTRY_NAMES.get(c)
    return f"{name} ({c})" if name else c


# ─── helpers ────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _naive(dt: Any) -> Optional[datetime]:
    """Naive UTC (aware values are converted; naive values are UTC already)."""
    if not isinstance(dt, datetime):
        return None
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def _num(v: Any) -> Optional[float]:
    """float, or None for NULL / NaN / ±inf (an empty quantile is nan)."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def _int(v: Any) -> int:
    f = _num(v)
    return int(f) if f is not None else 0


def _count(n: Any) -> str:
    return f"{_int(n):,}"


def _pct(v: Optional[float]) -> str:
    return "—" if v is None else f"{v:.2f}%"


def _share(part: Any, whole: Any) -> Optional[float]:
    w = _num(whole) or 0.0
    return (_num(part) or 0.0) / w * 100.0 if w > 0 else None


def _fmt_vital(name: str, v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:.3f}" if name == "cls" else _fmt_ms(v)


def _rating(name: str, v: Optional[float]) -> tuple[str, str]:
    """(rating word, KPI accent) for a p75 vital."""
    if v is None:
        return "no data", "neutral"
    good, poor = THRESHOLDS[name]
    if v <= good:
        return "good", "success"
    if v <= poor:
        return "needs improvement", "warning"
    return "poor", "danger"


def _vital_change(name: str, now: Optional[float], before: Optional[float],
                  against: str = "previous period") -> str:
    if now is None:
        return "Not measured"
    if before is None:
        return f"No data for the {against}"
    d = now - before
    if name == "cls":
        if abs(d) < 0.0005:
            return f"No change vs {against}"
        return f"{'▲' if d > 0 else '▼'} {abs(d):.3f} vs {against}"
    if abs(d) < 0.5:
        return f"No change vs {against}"
    return f"{'▲' if d > 0 else '▼'} {_fmt_ms(abs(d))} vs {against}"


def _pts_change(now: Optional[float], before: Optional[float],
                against: str = "previous period") -> str:
    if now is None:
        return "Not measured"
    if before is None:
        return f"No data for the {against}"
    d = now - before
    if abs(d) < 0.005:
        return f"No change vs {against}"
    return f"{'▲' if d > 0 else '▼'} {abs(d):.2f} pts vs {against}"


def _duration_ms(v: Optional[float]) -> str:
    """Accumulated time: '850 ms', '12.40 s', '3.5 min', '2.1 h'."""
    if v is None:
        return "—"
    if v < 60_000:
        return _fmt_ms(v)
    if v < 3_600_000:
        return f"{v / 60_000:.1f} min"
    return f"{v / 3_600_000:.1f} h"


def _clip(s: Any, limit: int = 90) -> str:
    s = str(s or "").strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"


def clean_url(value: Any, limit: int = 96) -> str:
    """A resource URL fit for a report: no query string, fragment or
    credentials; long URLs keep their host and the end of the path."""
    raw = str(value or "").strip()
    if not raw:
        return "—"
    low = raw.lower()
    if low.startswith("data:"):
        return "data: URI (inline)"
    if low.startswith("blob:"):
        return "blob: URL"
    try:
        p = urlsplit(raw)
        if p.scheme in ("http", "https") and p.hostname:
            host = p.hostname.lower()
            if ":" in host:
                host = f"[{host}]"
            port = f":{p.port}" if p.port else ""
            out = urlunsplit((p.scheme, host + port, p.path, "", ""))
        else:
            out = p.path or raw.split("?", 1)[0].split("#", 1)[0]
    except ValueError:
        out = raw.split("?", 1)[0].split("#", 1)[0]
    if len(out) > limit:
        head = limit // 2 - 1
        out = out[:head] + "…" + out[-(limit - head - 1):]
    return out


def _title_n(title: str, shown: int, total: int, noun: str) -> str:
    return f"{title} — top {shown} of {total:,} {noun}" if total > shown else title


def _kpi(label: str, value: str, accent: str, subtitle: Optional[str] = None) -> dict:
    out = {"label": label, "value": value, "accent": accent}
    if subtitle:
        out["subtitle"] = subtitle
    return out


def _scope(ctx: SectionCtx, extra: str = "") -> str:
    """Time window + RUM application filter (parameters from ctx.rum_params)."""
    return f"timestamp >= %(f)s AND timestamp < %(t)s{ctx.rum_app_clause()}{extra}"


def _per_view(scope_sql: str, extra: str = "") -> str:
    """One row per page view with its finalized Web Vitals (rum.py's collapse)."""
    from app.api.v1.rum import _per_view_vitals_sql
    return _per_view_vitals_sql(scope_sql, extra_select=extra)


def _raw_vital_cols() -> str:
    """Per vital: p75, samples, good, poor, rated (rated = samples on raw)."""
    parts = []
    for n, (good, poor) in THRESHOLDS.items():
        p = f"{n}_present = 1"
        parts.append(f"quantileTDigestIf(0.75)({n}_value, {p}), countIf({p}), "
                     f"countIf({p} AND {n}_value <= {good}), countIf({p} AND {n}_value > {poor}), "
                     f"countIf({p})")
    return ", ".join(parts)


def _rollup_vital_cols() -> str:
    return ", ".join(f"quantileTDigestMerge(0.75)({n}), sum({n}_samples), sum({n}_good), "
                     f"sum({n}_poor), sum({n}_rated)" for n in THRESHOLDS)


def _vitals_from(row: Any, offset: int) -> dict[str, dict]:
    """{lcp|inp|cls: {p75, samples, good_pct, poor_pct, needs_pct, rated}}.

    Shares divide by *rated* samples (rollup buckets written before the band
    counters existed have p75 but no rating)."""
    out: dict[str, dict] = {}
    for i, n in enumerate(THRESHOLDS):
        base = offset + i * 5
        vals = list(row[base:base + 5]) if row is not None else []
        vals += [None] * (5 - len(vals))
        samples, good, poor, rated = _int(vals[1]), _int(vals[2]), _int(vals[3]), _int(vals[4])
        out[n] = {
            "p75": _num(vals[0]) if samples > 0 else None,
            "samples": samples, "rated": rated,
            "good_pct": good / rated * 100.0 if rated else None,
            "poor_pct": poor / rated * 100.0 if rated else None,
            "needs_pct": max(rated - good - poor, 0) / rated * 100.0 if rated else None,
        }
    return out


async def _retention(ctx: SectionCtx) -> dict[str, int]:
    """Raw and rollup retention in days, from the ClickHouse table TTLs
    (falls back to what the RUM API last read, then the defaults)."""
    key = "rum_pack:retention"
    if key not in ctx._cache:
        out = {"raw": RAW_DAYS_DEFAULT, "rollup": ROLLUP_DAYS_DEFAULT}
        try:
            from app.api.v1.rum import _RETENTION_CACHE
            out = {"raw": int(_RETENTION_CACHE.get("raw") or RAW_DAYS_DEFAULT),
                   "rollup": int(_RETENTION_CACHE.get("rollup") or ROLLUP_DAYS_DEFAULT)}
        except Exception:
            logger.debug("rum retention cache unavailable", exc_info=True)
        try:
            for name, engine in await ctx.ch_rows(
                    "SELECT name, engine_full FROM system.tables WHERE database = 'zenplus' "
                    "AND name IN ('apm_rum_events', 'apm_rum_metrics_5m')"):
                m = _TTL_RE.search(str(engine or ""))
                if m:
                    out["raw" if name == "apm_rum_events" else "rollup"] = int(m.group(1) or m.group(2))
        except Exception:
            logger.debug("rum retention lookup failed", exc_info=True)
        ctx._cache[key] = out
    return ctx._cache[key]


def _reaches_past(frm: datetime, days: int) -> bool:
    return _naive(frm) < _now() - timedelta(days=days)


async def _use_rollup(ctx: SectionCtx, frm: Optional[datetime] = None) -> bool:
    """Whether a window starting at ``frm`` reaches past raw retention (then the
    5-minute rollup answers it)."""
    return _reaches_past(frm or ctx.frm, (await _retention(ctx))["raw"])


async def _rollup_notes(ctx: SectionCtx) -> list[str]:
    ret = await _retention(ctx)
    notes = [f"This period starts more than {ret['raw']} days ago, beyond raw browser-event "
             "retention, so it is read from the 5-minute RUM aggregates: session and page-view "
             "counts are close approximations and vitals come from merged percentile digests."]
    n = ctx.window_note(ret["rollup"], "5-minute RUM aggregate data")
    if n:
        notes.append(n)
    return notes


async def _raw_only_note(ctx: SectionCtx, what: str) -> Optional[str]:
    return ctx.window_note((await _retention(ctx))["raw"], what)


def _as_dt(ctx: SectionCtx, v: Any) -> Optional[datetime]:
    """A ClickHouse bucket value as a datetime (a Date is local midnight)."""
    if isinstance(v, datetime):
        return v
    if isinstance(v, date):
        local = datetime(v.year, v.month, v.day, tzinfo=ZoneInfo(ctx.tz_name))
        return local.astimezone(timezone.utc).replace(tzinfo=None)
    return None


def _gapped(ctx: SectionCtx, pts: list[tuple[datetime, Optional[float]]], step_s: int,
            digits: int = 2) -> list[dict]:
    """Chart points with an explicit null where buckets are missing."""
    out: list[dict] = []
    prev: Optional[datetime] = None
    for ts, v in pts:
        cur = _naive(ts)
        if prev is not None and cur is not None and (cur - prev).total_seconds() > step_s * 1.5:
            out.append({"t": ctx.iso(prev + timedelta(seconds=step_s)), "v": None})
        out.append({"t": ctx.iso(ts), "v": None if v is None else round(v, digits)})
        prev = cur
    return out


async def _overall_vitals(ctx: SectionCtx, frm: datetime, to: datetime) -> dict:
    """Page views and p75 / rating shares of LCP, INP and CLS over [frm, to)."""
    params = ctx.rum_params(f=frm, t=to)
    if await _use_rollup(ctx, frm):
        rows = await ctx.ch_rows(
            f"SELECT uniqCombined64Merge(views), {_rollup_vital_cols()} "
            f"FROM {ROLLUP} WHERE {_scope(ctx)}", params)
    else:
        rows = await ctx.ch_rows(
            f"SELECT countIf(started = 1), {_raw_vital_cols()} "
            f"FROM ({_per_view(_scope(ctx), VIEW_STARTED)})", params)
    row = rows[0] if rows else None
    return {"views": _int(row[0]) if row else 0, **_vitals_from(row, 1)}


# ─── Core Web Vitals trend ──────────────────────────────────────────────────

INTERVALS = {"day": ("1 DAY", 86400), "hour": ("1 HOUR", 3600)}


@register_section(
    "rum_vitals_trend", title="Core Web Vitals Trend", category=CATEGORY,
    description="Daily 75th-percentile LCP, INP and CLS measured on real visitors' browsers, "
                "with the share of good and poor page views and the change against the "
                "previous period.",
    scope="fleet", requires=REQUIRES,
    options=[
        {"key": "rating_vital", "label": "Rating bands for", "type": "select", "default": "lcp",
         "choices": VITAL_CHOICES,
         "help": "Which vital's good / needs-improvement / poor shares to chart and tabulate."},
        {"key": "interval", "label": "Interval", "type": "select", "default": "auto",
         "choices": [["auto", "Automatic"], ["day", "Daily"], ["hour", "Hourly"]],
         "help": "Automatic is hourly for periods up to two days and daily otherwise."},
    ],
)
async def rum_vitals_trend(ctx: SectionCtx) -> dict:
    sid, title = "rum_vitals_trend", "Core Web Vitals Trend"
    band = str(ctx.opt("rating_vital"))
    band_label = VITAL_LABEL[band]
    choice = str(ctx.opt("interval"))
    notes: list[str] = []
    if choice == "auto":
        choice = "hour" if ctx.hours <= 48 else "day"
    elif choice == "hour" and ctx.hours > 14 * 24:
        choice = "day"
        notes.append("Hourly points are limited to periods of up to 14 days; this period is "
                     "shown by day.")
    interval, step_s = INTERVALS[choice]
    noun = "day" if choice == "day" else "hour"
    desc = (f"{'Daily' if choice == 'day' else 'Hourly'} 75th-percentile LCP, INP and CLS from "
            f"real visitors, with the share of good and poor page views ({band_label}).")

    rollup = await _use_rollup(ctx)
    params = ctx.rum_params()
    if rollup:
        bucket, bp = ctx.ch_bucket("timestamp", interval)
        rows = await ctx.ch_rows(
            f"SELECT {bucket} AS b, uniqCombined64Merge(views), {_rollup_vital_cols()} "
            f"FROM {ROLLUP} WHERE {_scope(ctx)} GROUP BY b ORDER BY b", {**params, **bp})
    else:
        bucket, bp = ctx.ch_bucket("view_ts", interval)
        per_view = _per_view(_scope(ctx), f"min(raw.timestamp) AS view_ts, {VIEW_STARTED}")
        rows = await ctx.ch_rows(
            f"SELECT {bucket} AS b, countIf(started = 1), {_raw_vital_cols()} "
            f"FROM ({per_view}) GROUP BY b ORDER BY b", {**params, **bp})

    buckets = []
    for r in rows:
        ts = _as_dt(ctx, r[0])
        if ts is not None:
            buckets.append({"t": ts, "views": _int(r[1]), **_vitals_from(r, 2)})
    if not any(b[n]["samples"] for b in buckets for n in THRESHOLDS):
        out_notes = ["No Core Web Vitals were collected in this period — vitals arrive once real "
                     "visitors load a page instrumented with the browser RUM SDK."]
        if rollup:
            out_notes += await _rollup_notes(ctx)
        return _section(sid, title, description=desc, notes=out_notes + notes)

    now = await _overall_vitals(ctx, ctx.frm, ctx.to)
    pf, pt = ctx.prior_window()
    before = await _overall_vitals(ctx, pf, pt)

    kpis = []
    for n in THRESHOLDS:
        word, accent = _rating(n, now[n]["p75"])
        kpis.append(_kpi(f"{VITAL_LABEL[n]} (p75)", _fmt_vital(n, now[n]["p75"]), accent,
                         f"{word.capitalize()} · {_vital_change(n, now[n]['p75'], before[n]['p75'])}"))
    good_now = now[band]["good_pct"]
    kpis.append(_kpi(f"Good {band_label} page views", _pct(good_now),
                     "neutral" if good_now is None else
                     "success" if good_now >= 75 else "warning" if good_now >= 50 else "danger",
                     _pts_change(good_now, before[band]["good_pct"])))

    charts = []
    colors = {"lcp": "primary", "inp": "info", "cls": "warning"}
    for n in THRESHOLDS:
        pts = [(b["t"], b[n]["p75"]) for b in buckets]
        if any(v is not None for _t, v in pts):
            charts.append({"title": f"{VITAL_LABEL[n]} p75 by {noun}",
                           "series": {"kind": "line", "unit": "" if n == "cls" else "ms",
                                      "color": colors[n],
                                      "points": _gapped(ctx, pts, step_s, 3 if n == "cls" else 0)}})
    if any(b[band]["rated"] for b in buckets):
        rated = [b for b in buckets if b[band]["rated"]]
        charts.append({"title": f"{band_label} rating by {noun} (share of page views)",
                       "series": {"kind": "stacked", "unit": "%", "y_domain": [0, 100],
                                  "series": [
                                      {"name": "Good", "points": [
                                          {"t": ctx.iso(b["t"]), "v": round(b[band]["good_pct"], 2)} for b in rated]},
                                      {"name": "Needs improvement", "points": [
                                          {"t": ctx.iso(b["t"]), "v": round(b[band]["needs_pct"], 2)} for b in rated]},
                                      {"name": "Poor", "points": [
                                          {"t": ctx.iso(b["t"]), "v": round(b[band]["poor_pct"], 2)} for b in rated]},
                                  ]}})

    fmt_t = ctx.fmt_date if choice == "day" else ctx.fmt_short
    table_rows = [[fmt_t(b["t"]), _count(b["views"]),
                   _fmt_vital("lcp", b["lcp"]["p75"]), _fmt_vital("inp", b["inp"]["p75"]),
                   _fmt_vital("cls", b["cls"]["p75"]),
                   _pct(b[band]["good_pct"]), _pct(b[band]["poor_pct"])]
                  for b in reversed(buckets)]
    notes = [VITALS_METHOD + " " + THRESHOLD_NOTE,
             f"Times are in {ctx.tz_label()}; '—' marks a {noun} without samples for that vital."] + notes
    if rollup:
        notes += await _rollup_notes(ctx)
        if any(b[band]["samples"] and not b[band]["rated"] for b in buckets):
            notes.append("Some aggregates were written before rating counters existed: those "
                         f"{noun}s show a p75 but no good / poor share.")
    return _section(
        sid, title, description=desc, kpis=kpis, charts=charts,
        tables=[{"title": f"Core Web Vitals by {noun} (newest first)",
                 "headers": ["Day" if choice == "day" else "Hour", "Page views", "LCP p75",
                             "INP p75", "CLS p75", f"Good {band_label}", f"Poor {band_label}"],
                 "styles": ["text", "num", "num", "num", "num", "pct", "pct"],
                 "rows": table_rows}],
        notes=notes,
    )


# ─── Audience by country ────────────────────────────────────────────────────

GEO_SORTS = {"sessions": "Sessions", "error_rate": "Error-Session Rate", "lcp": "Page Load (LCP p75)"}


@register_section(
    "rum_geography", title="Audience by Country", category=CATEGORY,
    description="Sessions, page views, error-session rate and Core Web Vitals per visitor country.",
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(15, 250),
             {"key": "sort", "label": "Sort by", "type": "select", "default": "sessions",
              "choices": [["sessions", "Sessions"], ["error_rate", "Error-session rate"],
                          ["lcp", "LCP p75 (slowest first)"]]}],
)
async def rum_geography(ctx: SectionCtx) -> dict:
    sid = "rum_geography"
    sort = str(ctx.opt("sort"))
    top_n = int(ctx.opt("top_n"))
    title = "Audience by Country" if sort == "sessions" else f"Countries by {GEO_SORTS[sort]}"
    desc = (f"Sessions, page views, error-session rate and Core Web Vitals per visitor country, "
            f"ranked by {GEO_SORTS[sort].lower()}.")
    rollup = await _use_rollup(ctx)
    params = ctx.rum_params()
    countries: dict[str, dict] = {}
    if rollup:
        tot = await ctx.ch_rows(
            "SELECT uniqCombined64Merge(sessions), uniqCombined64Merge(error_sessions), "
            f"uniqCombined64Merge(views) FROM {ROLLUP} WHERE {_scope(ctx)}", params)
        for r in await ctx.ch_rows(
                "SELECT country, uniqCombined64Merge(sessions) AS s, uniqCombined64Merge(views), "
                f"uniqCombined64Merge(error_sessions), {_rollup_vital_cols()} "
                f"FROM {ROLLUP} WHERE {_scope(ctx)} GROUP BY country ORDER BY s DESC LIMIT {MAX_GROUPS}",
                params):
            countries[str(r[0] or "")] = {"sessions": _int(r[1]), "views": _int(r[2]),
                                          "err": _int(r[3]), **_vitals_from(r, 4)}
    else:
        tot = await ctx.ch_rows(
            f"SELECT {SESSIONS}, {ERROR_SESSIONS}, {VIEWS} FROM {EVENTS} WHERE {_scope(ctx)}", params)
        for r in await ctx.ch_rows(
                f"SELECT country, {SESSIONS} AS s, {VIEWS}, {ERROR_SESSIONS} "
                f"FROM {EVENTS} WHERE {_scope(ctx)} GROUP BY country ORDER BY s DESC LIMIT {MAX_GROUPS}",
                params):
            countries[str(r[0] or "")] = {"sessions": _int(r[1]), "views": _int(r[2]),
                                          "err": _int(r[3]), **_vitals_from(None, 0)}
        per_view = _per_view(_scope(ctx), "anyIf(raw.country, raw.country != '') AS dim_value")
        for r in await ctx.ch_rows(
                f"SELECT dim_value, {_raw_vital_cols()} FROM ({per_view}) GROUP BY dim_value", params):
            c = countries.get(str(r[0] or ""))
            if c is not None:
                c.update(_vitals_from(r, 1))
    total_sessions = _int(tot[0][0]) if tot else 0
    total_err = _int(tot[0][1]) if tot else 0
    countries = {k: v for k, v in countries.items() if v["sessions"] > 0}
    notes: list[str] = []
    if rollup:
        notes += await _rollup_notes(ctx)
    if not countries:
        return _section(sid, title, description=desc,
                        notes=["No browser sessions were recorded in this period."] + notes)

    for code, c in countries.items():
        c["code"] = code
        c["label"] = country_label(code)
        c["err_rate"] = _share(c["err"], c["sessions"])
    items = list(countries.values())
    if sort == "error_rate":
        items.sort(key=lambda c: (c["err_rate"] is None, -(c["err_rate"] or 0), -c["sessions"], c["label"]))
    elif sort == "lcp":
        items.sort(key=lambda c: (c["lcp"]["p75"] is None, -(c["lcp"]["p75"] or 0), -c["sessions"], c["label"]))
    else:
        items.sort(key=lambda c: (-c["sessions"], c["label"]))
    shown = items[:top_n]
    known = [c for c in items if c["code"]]
    unknown = countries.get("", {}).get("sessions", 0)
    leader = max(known, key=lambda c: (c["sessions"], c["label"]), default=None)
    denom = total_sessions or sum(c["sessions"] for c in items)

    kpis = [
        _kpi("Countries", _count(len(known)), "primary", "with at least one session"),
        _kpi("Sessions", _count(total_sessions), "info",
             f"{_pct(_share(total_err, total_sessions))} had a JavaScript error"),
        _kpi("Top country", leader["label"] if leader else "—", "primary",
             f"{_pct(_share(leader['sessions'], denom))} of sessions" if leader else None),
        _kpi("Location unknown", _pct(_share(unknown, denom)), "warning" if unknown else "neutral",
             f"{_count(unknown)} sessions without a country"),
    ]
    chart_items = sorted(items, key=lambda c: (-c["sessions"], c["label"]))[:10]
    charts = [{"title": "Sessions by country",
               "series": {"kind": "rank", "unit": "sessions", "color": "primary",
                          "points": [{"label": c["label"], "value": c["sessions"]} for c in chart_items]}}]
    rows = [[c["label"], _count(c["sessions"]), _pct(_share(c["sessions"], denom)), _count(c["views"]),
             _pct(c["err_rate"]), _fmt_vital("lcp", c["lcp"]["p75"]), _fmt_vital("inp", c["inp"]["p75"]),
             _rating("lcp", c["lcp"]["p75"])[0]] for c in shown]
    notes = ["Country comes from the CDN country header or the appliance's GeoIP database; "
             "private and unresolvable addresses are listed as Unknown. Error-session rate is the "
             "share of a country's sampled sessions with at least one JavaScript error. "
             + THRESHOLD_NOTE] + notes
    return _section(
        sid, title, description=desc, kpis=kpis, charts=charts,
        tables=[{"title": _title_n(f"Countries by {GEO_SORTS[sort].lower()}", len(shown), len(items),
                                   "countries"),
                 "headers": ["Country", "Sessions", "Share", "Page views", "Error-session rate",
                             "LCP p75", "INP p75", "LCP rating"],
                 "styles": ["text", "num", "pct", "num", "pct", "num", "num", "status"],
                 "rows": rows}],
        notes=notes,
    )


# ─── Release comparison ─────────────────────────────────────────────────────

RELEASE_SORTS = {"recent": "most recent first", "sessions": "most sessions",
                 "error_rate": "highest error-session rate"}


@register_section(
    "rum_releases", title="Release Experience Comparison", category=CATEGORY,
    description="Sessions, error-session rate and Core Web Vitals for each application release "
                "(service_version) seen in the period, newest first.",
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(10, 100),
             {"key": "sort", "label": "Order", "type": "select", "default": "recent",
              "choices": [["recent", "Most recent first"], ["sessions", "Most sessions"],
                          ["error_rate", "Highest error-session rate"]]}],
)
async def rum_releases(ctx: SectionCtx) -> dict:
    sid, title = "rum_releases", "Release Experience Comparison"
    sort = str(ctx.opt("sort"))
    top_n = int(ctx.opt("top_n"))
    desc = ("Sessions, error-session rate and Core Web Vitals for each application release "
            f"(service_version) seen in the period, {RELEASE_SORTS[sort]}.")
    rollup = await _use_rollup(ctx)
    params = ctx.rum_params()
    rel: dict[str, dict] = {}
    if rollup:
        for r in await ctx.ch_rows(
                "SELECT service_version, uniqCombined64Merge(sessions), uniqCombined64Merge(views), "
                "uniqCombined64Merge(error_sessions), sum(sampled_errors), min(timestamp), "
                f"max(timestamp) AS last_ts, {_rollup_vital_cols()} "
                f"FROM {ROLLUP} WHERE {_scope(ctx)} GROUP BY service_version "
                f"ORDER BY last_ts DESC LIMIT {MAX_GROUPS}", params):
            rel[str(r[0] or "")] = {"sessions": _int(r[1]), "views": _int(r[2]), "err": _int(r[3]),
                                    "errors": _int(r[4]), "first": r[5], "last": r[6],
                                    **_vitals_from(r, 7)}
    else:
        for r in await ctx.ch_rows(
                f"SELECT service_version, {SESSIONS}, {VIEWS}, {ERROR_SESSIONS}, "
                "countIf(event_type = 'error' AND sampled = 1), min(timestamp), max(timestamp) AS last_ts "
                f"FROM {EVENTS} WHERE {_scope(ctx)} GROUP BY service_version "
                f"ORDER BY last_ts DESC LIMIT {MAX_GROUPS}", params):
            rel[str(r[0] or "")] = {"sessions": _int(r[1]), "views": _int(r[2]), "err": _int(r[3]),
                                    "errors": _int(r[4]), "first": r[5], "last": r[6],
                                    **_vitals_from(None, 0)}
        per_view = _per_view(_scope(ctx), "anyIf(raw.service_version, raw.service_version != '') AS dim_value")
        for r in await ctx.ch_rows(
                f"SELECT dim_value, {_raw_vital_cols()} FROM ({per_view}) GROUP BY dim_value", params):
            x = rel.get(str(r[0] or ""))
            if x is not None:
                x.update(_vitals_from(r, 1))
    untagged = rel.pop("", None)
    items = [{"version": k, **v} for k, v in rel.items() if v["sessions"] > 0 or v["errors"] > 0]
    notes: list[str] = []
    if rollup:
        notes += await _rollup_notes(ctx)
    if untagged and untagged["sessions"]:
        notes.append(f"{_count(untagged['sessions'])} session(s) carried no release version; set "
                     "the version option of the browser RUM SDK to attribute them.")
    if not items:
        return _section(sid, title, description=desc,
                        notes=["No release-tagged browser sessions in this period."] + notes)

    for x in items:
        x["err_rate"] = _share(x["err"], x["sessions"])
        x["first_n"] = _naive(x["first"]) or datetime.min
    by_first = sorted(items, key=lambda x: (x["first_n"], x["version"]), reverse=True)
    latest = by_first[0]
    prior = by_first[1] if len(by_first) > 1 else None
    if sort == "sessions":
        items.sort(key=lambda x: (-x["sessions"], x["version"]))
    elif sort == "error_rate":
        items.sort(key=lambda x: (x["err_rate"] is None, -(x["err_rate"] or 0), -x["sessions"]))
    else:
        items = by_first
    shown = items[:top_n]
    against = f"release {prior['version']}" if prior else "previous release"
    lcp_word, lcp_accent = _rating("lcp", latest["lcp"]["p75"])
    err = latest["err_rate"]
    kpis = [
        _kpi("Releases seen", _count(len(items)), "primary", "with browser sessions in the period"),
        _kpi("Latest release", _clip(latest["version"], 40), "info",
             f"First seen {ctx.fmt_short(latest['first'])}"),
        _kpi("Error-session rate (latest)", _pct(err),
             "neutral" if err is None else "danger" if err > 5 else "warning" if err > 1 else "success",
             _pts_change(err, prior["err_rate"] if prior else None, against)),
        _kpi("LCP p75 (latest)", _fmt_vital("lcp", latest["lcp"]["p75"]), lcp_accent,
             f"{lcp_word.capitalize()} · " + _vital_change(
                 "lcp", latest["lcp"]["p75"], prior["lcp"]["p75"] if prior else None, against)),
    ]
    chart_items = [x for x in shown if x["err_rate"] is not None]
    charts = ([{"title": "Error-session rate by release",
                "series": {"kind": "rank", "unit": "%", "color": "danger",
                           "points": [{"label": _clip(x["version"], 40), "value": round(x["err_rate"], 2)}
                                      for x in chart_items]}}] if chart_items else [])
    rows = [[_clip(x["version"], 60), ctx.fmt_short(x["first"]), ctx.fmt_short(x["last"]),
             _count(x["sessions"]), _count(x["views"]), _pct(x["err_rate"]),
             _fmt_vital("lcp", x["lcp"]["p75"]), _fmt_vital("inp", x["inp"]["p75"]),
             _fmt_vital("cls", x["cls"]["p75"])] for x in shown]
    notes = ["First and last seen are within this period. Error-session rate is the share of a "
             "release's sampled sessions with at least one JavaScript error. " + VITALS_METHOD] + notes
    return _section(
        sid, title, description=desc, kpis=kpis, charts=charts,
        tables=[{"title": _title_n(f"Releases, {RELEASE_SORTS[sort]}", len(shown), len(items), "releases"),
                 "headers": ["Release", "First seen", "Last seen", "Sessions", "Page views",
                             "Error-session rate", "LCP p75", "INP p75", "CLS p75"],
                 "styles": ["mono", "text", "text", "num", "num", "pct", "num", "num", "num"],
                 "rows": rows}],
        notes=notes,
    )


# ─── Slow and failing resources ─────────────────────────────────────────────

RESOURCE_TYPES: dict[str, tuple[str, str]] = {
    # option -> (label, SQL condition over the allow-listed resource_type values)
    "all": ("All resources", ""),
    "script": ("Scripts", " AND resource_type = 'script'"),
    "css": ("Stylesheets and CSS assets", " AND resource_type IN ('link', 'css')"),
    "img": ("Images", " AND resource_type IN ('img', 'image')"),
    "fetch": ("API calls (fetch/XHR)", " AND resource_type IN ('fetch', 'xhr', 'xmlhttprequest', 'beacon')"),
    "other": ("Other resources", " AND resource_type NOT IN ('script', 'link', 'css', 'img', 'image', "
                                 "'fetch', 'xhr', 'xmlhttprequest', 'beacon')"),
}
TYPE_LABELS = {"script": "Script", "link": "Stylesheet / link", "css": "CSS asset", "img": "Image",
               "image": "Image", "fetch": "Fetch", "xhr": "XHR", "xmlhttprequest": "XHR",
               "beacon": "Beacon", "iframe": "Frame", "video": "Video", "audio": "Audio",
               "other": "Other", "resource": "Other", "": "Unknown"}
RESOURCE_URL = "cutQueryStringAndFragment(resource_url)"


def _type_label(t: Any) -> str:
    t = str(t or "").strip().lower()
    return TYPE_LABELS.get(t, t.capitalize() or "Unknown")


def _status_label(code: Any) -> str:
    c = _int(code)
    return str(c) if c else "Network error"


@register_section(
    "rum_slow_resources", title="Slow and Failing Resources", category=CATEGORY,
    description="Scripts, stylesheets, images and API calls real browsers loaded slowly or failed "
                "to load, by resource type, with URLs stripped of query strings.",
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(10, 100),
             {"key": "resource_type", "label": "Resource type", "type": "select", "default": "all",
              "choices": [[k, v[0]] for k, v in RESOURCE_TYPES.items()]},
             {"key": "min_requests", "label": "Minimum requests to rank as slow", "type": "int",
              "default": 3, "min": 1, "max": 10000,
              "help": "Resources requested fewer times are left out of the slowest list."}],
)
async def rum_slow_resources(ctx: SectionCtx) -> dict:
    sid, title = "rum_slow_resources", "Slow and Failing Resources"
    top_n = int(ctx.opt("top_n"))
    rtype = str(ctx.opt("resource_type"))
    min_req = int(ctx.opt("min_requests"))
    type_label, type_sql = RESOURCE_TYPES[rtype]
    desc = (f"{type_label} that real browsers loaded slowly or failed to load, by resource type, "
            "with URLs stripped of query strings.")
    where = _scope(ctx, f" AND sampled = 1 AND event_type = 'resource' AND resource_url != ''{type_sql}")
    params = ctx.rum_params(min_req=min_req, lim=top_n)
    notes: list[str] = []
    n = await _raw_only_note(ctx, "Browser resource detail")
    if n:
        notes.append(n)

    tot = await ctx.ch_rows(
        f"SELECT count(), countIf({FAILED}), uniqExact((resource_type, {RESOURCE_URL})), "
        f"uniqExactIf((resource_type, {RESOURCE_URL}), {FAILED}), "
        "quantileTDigestIf(0.75)(duration_ms, duration_ms > 0), countIf(duration_ms > 0) "
        f"FROM {EVENTS} WHERE {where}", params)
    t = tot[0] if tot else (0, 0, 0, 0, None, 0)
    requests, failures, distinct, failing_n = _int(t[0]), _int(t[1]), _int(t[2]), _int(t[3])
    if not requests:
        return _section(sid, title, description=desc,
                        notes=["No browser resource timings were recorded in this period."] + notes)
    p75_all = _num(t[4]) if _int(t[5]) else None

    by_type = await ctx.ch_rows(
        f"SELECT resource_type, count() AS n, countIf({FAILED}), "
        "quantileTDigestIf(0.75)(duration_ms, duration_ms > 0), countIf(duration_ms > 0), "
        "avgIf(transfer_size, transfer_size > 0), countIf(transfer_size > 0) "
        f"FROM {EVENTS} WHERE {where} GROUP BY resource_type ORDER BY n DESC LIMIT 50", params)
    slow_total = await ctx.ch_rows(
        f"SELECT count() FROM (SELECT 1 FROM {EVENTS} WHERE {where} "
        f"GROUP BY resource_type, {RESOURCE_URL} HAVING countIf(duration_ms > 0) >= %(min_req)s)", params)
    slow = await ctx.ch_rows(
        f"SELECT resource_type, {RESOURCE_URL} AS u, count(), "
        "quantileTDigestIf(0.75)(duration_ms, duration_ms > 0) AS p75, countIf(duration_ms > 0), "
        f"avgIf(transfer_size, transfer_size > 0), countIf(transfer_size > 0), countIf({FAILED}) "
        f"FROM {EVENTS} WHERE {where} GROUP BY resource_type, u "
        "HAVING countIf(duration_ms > 0) >= %(min_req)s ORDER BY p75 DESC, u ASC LIMIT %(lim)s", params)
    failing = await ctx.ch_rows(
        f"SELECT resource_type, {RESOURCE_URL} AS u, count(), countIf({FAILED}) AS nf, "
        f"argMaxIf(status_code, timestamp, {FAILED}), maxIf(timestamp, {FAILED}) "
        f"FROM {EVENTS} WHERE {where} GROUP BY resource_type, u HAVING nf > 0 "
        "ORDER BY nf DESC, u ASC LIMIT %(lim)s", params)

    type_rows, type_pts = [], []
    for r in by_type:
        p75 = _num(r[3]) if _int(r[4]) else None
        size = _num(r[5]) if _int(r[6]) else None
        type_rows.append([_type_label(r[0]), _count(r[1]), _count(r[2]), _pct(_share(r[2], r[1])),
                          _fmt_ms(p75), _fmt_bytes(size) if size is not None else "—"])
        if p75 is not None:
            type_pts.append({"label": _type_label(r[0]), "value": round(p75, 1)})
    slow_rows = []
    for r in slow:
        p75 = _num(r[3]) if _int(r[4]) else None
        size = _num(r[5]) if _int(r[6]) else None
        slow_rows.append([clean_url(r[1]), _type_label(r[0]), _count(r[2]), _fmt_ms(p75),
                          _fmt_bytes(size) if size is not None else "—", _pct(_share(r[7], r[2]))])
    fail_rows = [[clean_url(r[1]), _type_label(r[0]), _count(r[2]), _count(r[3]), _pct(_share(r[3], r[2])),
                  _status_label(r[4]), ctx.fmt_short(r[5])] for r in failing]
    n_slow = _int(slow_total[0][0]) if slow_total else len(slow_rows)

    fail_rate = _share(failures, requests)
    kpis = [
        _kpi("Resource requests", _count(requests), "primary", f"{_count(distinct)} distinct resources"),
        _kpi("Failed requests", _count(failures), "danger" if failures else "success",
             f"{_pct(fail_rate)} of requests"),
        _kpi("Failing resources", _count(failing_n), "warning" if failing_n else "success",
             "HTTP 4xx/5xx or network errors"),
        _kpi("Resource p75 duration", _fmt_ms(p75_all), "info", "request start to response end"),
    ]
    charts = ([{"title": "p75 load time by resource type",
                "series": {"kind": "rank", "unit": "ms", "color": "primary", "points": type_pts}}]
              if type_pts else [])
    tables = [{"title": "By resource type",
               "headers": ["Type", "Requests", "Failed", "Failure rate", "p75 duration", "Avg transfer size"],
               "styles": ["text", "num", "num", "pct", "num", "num"], "rows": type_rows}]
    if slow_rows:
        tables.append({"title": _title_n("Slowest resources (p75 duration)", len(slow_rows), n_slow, "resources"),
                       "headers": ["Resource", "Type", "Requests", "p75 duration", "Avg transfer size",
                                   "Failure rate"],
                       "styles": ["mono", "text", "num", "num", "num", "pct"], "rows": slow_rows})
    else:
        notes.insert(0, f"No resource was requested at least {min_req} time(s) with a measured "
                        "duration, so none is ranked as slow.")
    if fail_rows:
        tables.append({"title": _title_n("Failing resources", len(fail_rows), failing_n, "resources"),
                       "headers": ["Resource", "Type", "Requests", "Failed", "Failure rate",
                                   "Last status", "Last failure"],
                       "styles": ["mono", "text", "num", "num", "pct", "text", "text"], "rows": fail_rows})
    else:
        notes.insert(0, "No resource failed to load in this period.")
    notes.append("Durations are the browser's Resource Timing duration (request start to response "
                 "end) from sampled sessions. Cross-origin resources served without "
                 "Timing-Allow-Origin report no transfer size (—). A failure is an HTTP 4xx/5xx "
                 "response or a resource the browser could not load (network error). Query strings "
                 "are removed from URLs.")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── Page-load breakdown ────────────────────────────────────────────────────

# Per-sample, non-overlapping phases (milliseconds). connect_ms includes the
# TLS handshake and wait_ms includes the server time the Server-Timing header
# declares (which includes database time), so those are carved out instead of
# stacked twice.
_SRV = "least(if(has_server_timing = 1, server_ms, 0), wait_ms)"
_DB = f"least(if(has_server_timing = 1, db_ms, 0), {_SRV})"
PHASES: list[tuple[str, str, str]] = [
    ("redirect", "Redirect", "redirect_ms"),
    ("dns", "DNS lookup", "dns_ms"),
    ("connect", "TCP connect", "greatest(connect_ms - tls_ms, 0)"),
    ("tls", "TLS handshake", "tls_ms"),
    ("wait", "Waiting (network and queueing)", f"greatest(wait_ms - {_SRV}, 0)"),  # = WAIT_PHASE
    ("server", "Server processing", f"greatest({_SRV} - {_DB}, 0)"),
    ("db", "Database", _DB),
    ("download", "Content download", "download_ms"),
    ("processing", "Browser processing", "processing_ms"),
]
SERVER_PHASES = ("server", "db")
WAIT_PHASE = f"greatest(wait_ms - {_SRV}, 0)"
TARGETS = {
    "page_loads": {
        "title": "Page Load Breakdown", "noun": "page loads", "unit": "Page loads",
        "where": " AND sampled = 1 AND event_type = 'view' AND is_final = 1 AND has_timing = 1",
        "duration": "load_ms", "group": "view_name", "label": "Page",
    },
    "api": {
        "title": "API Call Breakdown", "noun": "API calls", "unit": "Calls",
        "where": (" AND sampled = 1 AND event_type = 'resource' AND has_timing = 1 "
                  "AND resource_type IN ('fetch', 'xhr', 'xmlhttprequest')"),
        "duration": "duration_ms", "group": f"method, {RESOURCE_URL}", "label": "Endpoint",
    },
}


@register_section(
    "rum_load_breakdown", title="Page Load Breakdown", category=CATEGORY,
    description="Where page-load time goes — DNS, connect, TLS, waiting, server, database, "
                "download and browser processing — over time and for the slowest pages.",
    scope="fleet", requires=REQUIRES,
    options=[{"key": "target", "label": "Requests", "type": "select", "default": "page_loads",
              "choices": [["page_loads", "Page loads (navigations)"], ["api", "API calls (fetch/XHR)"]]},
             top_n_option(10, 50)],
)
async def rum_load_breakdown(ctx: SectionCtx) -> dict:
    sid = "rum_load_breakdown"
    target = str(ctx.opt("target"))
    cfg = TARGETS[target]
    title = cfg["title"]
    top_n = int(ctx.opt("top_n"))
    phases = [p for p in PHASES if not (target == "api" and p[0] == "processing")]
    desc = (f"Where {cfg['noun']} spend their time — "
            + ", ".join(p[1].lower() for p in phases)
            + f" — over time and for the slowest {'pages' if target == 'page_loads' else 'endpoints'}.")
    where = _scope(ctx, cfg["where"])
    params = ctx.rum_params(lim=top_n)
    notes: list[str] = []
    n = await _raw_only_note(ctx, "Browser timing detail")
    if n:
        notes.append(n)

    dur = cfg["duration"]
    cols = []
    for key, _label, expr in phases:
        p75 = (f"quantileTDigestIf(0.75)({expr}, has_server_timing = 1)" if key in SERVER_PHASES
               else f"quantileTDigest(0.75)({expr})")
        cols.append(f"{p75}, avg({expr})")
    tot = await ctx.ch_rows(
        f"SELECT count(), countIf(has_server_timing = 1), "
        f"quantileTDigestIf(0.75)({dur}, {dur} > 0), countIf({dur} > 0), {', '.join(cols)} "
        f"FROM {EVENTS} WHERE {where}", params)
    t = tot[0] if tot else None
    samples = _int(t[0]) if t else 0
    if not samples:
        return _section(sid, title, description=desc,
                        notes=[f"No {cfg['noun']} with a timing breakdown were recorded in this period. "
                               "Phases come from hard navigations (single-page route changes have no "
                               "network phases) and from fetch/XHR calls the SDK can time."] + notes)
    srv_samples = _int(t[1])
    dur_p75 = _num(t[2]) if _int(t[3]) else None
    stats = []
    for i, (key, label, _expr) in enumerate(phases):
        p75, avg = _num(t[4 + i * 2]), _num(t[5 + i * 2])
        measured = srv_samples if key in SERVER_PHASES else samples
        stats.append({"key": key, "label": label, "p75": p75 if measured else None,
                      "avg": avg, "samples": measured})
    total_avg = sum(s["avg"] or 0.0 for s in stats)
    for s in stats:
        s["share"] = _share(s["avg"], total_avg) if total_avg > 0 else None
    largest = max(stats, key=lambda s: s["avg"] or 0.0)

    bucket, bp = ctx.ch_bucket("timestamp")
    trend = await ctx.ch_rows(
        f"SELECT {bucket} AS b, {', '.join(f'avg({expr})' for _k, _l, expr in phases)} "
        f"FROM {EVENTS} WHERE {where} GROUP BY b ORDER BY b", {**params, **bp})
    series = []
    for i, s in enumerate(stats):
        pts = [{"t": ctx.iso(_as_dt(ctx, r[0])), "v": round(_num(r[1 + i]) or 0.0, 1)} for r in trend]
        if any(p["v"] for p in pts):
            series.append({"name": s["label"], "points": pts})
    charts = ([{"title": f"Average time per phase ({cfg['noun']})",
                "series": {"kind": "stacked", "unit": "ms", "series": series}}] if series else [])

    group = cfg["group"]
    slow = await ctx.ch_rows(
        f"SELECT {group}, count() AS n, quantileTDigestIf(0.75)({dur}, {dur} > 0) AS p75, "
        f"countIf({dur} > 0), quantileTDigest(0.75)({WAIT_PHASE}), "
        f"quantileTDigestIf(0.75)({_SRV}, has_server_timing = 1), countIf(has_server_timing = 1), "
        f"quantileTDigest(0.75)(download_ms) "
        f"FROM {EVENTS} WHERE {where} GROUP BY {group} HAVING countIf({dur} > 0) >= 2 "
        "ORDER BY p75 DESC LIMIT %(lim)s", params)
    slow_rows = []
    for r in slow:
        off = 1 if target == "page_loads" else 2
        name = (str(r[0] or "/") if target == "page_loads"
                else f"{str(r[0] or 'GET').upper()} {clean_url(r[1])}")
        slow_rows.append([_clip(name, 100), _count(r[off]),
                          _fmt_ms(_num(r[off + 1]) if _int(r[off + 2]) else None),
                          _fmt_ms(_num(r[off + 3])),
                          _fmt_ms(_num(r[off + 4]) if _int(r[off + 5]) else None),
                          _fmt_ms(_num(r[off + 6]))])

    kpis = [
        _kpi(f"Measured {cfg['noun']}", _count(samples), "primary", "with a phase breakdown"),
        _kpi("Load time p75" if target == "page_loads" else "Duration p75", _fmt_ms(dur_p75), "info",
             "navigation start to load event" if target == "page_loads" else "request start to response end"),
        _kpi("Largest phase", largest["label"], "warning",
             f"avg {_fmt_ms(largest['avg'])} · {_pct(largest['share'])} of measured time"),
        _kpi("Server-Timing coverage", _pct(_share(srv_samples, samples)),
             "success" if srv_samples else "neutral", "samples with a server / database split"),
    ]
    phase_rows = [[s["label"], _fmt_ms(s["p75"]), _fmt_ms(s["avg"]), _pct(s["share"]), _count(s["samples"])]
                  for s in stats]
    tables = [{"title": "Time by phase",
               "headers": ["Phase", "p75", "Average", "Share of measured time", "Samples"],
               "styles": ["text", "num", "num", "pct-bar", "num"], "rows": phase_rows}]
    if slow_rows:
        tables.append({
            "title": (f"Slowest pages by load time (p75) — top {len(slow_rows)}" if target == "page_loads"
                      else f"Slowest API calls (p75) — top {len(slow_rows)}"),
            "headers": [cfg["label"], cfg["unit"], "Load p75" if target == "page_loads" else "Duration p75",
                        "Waiting p75", "Server incl. DB p75", "Download p75"],
            "styles": ["mono", "num", "num", "num", "num", "num"], "rows": slow_rows})
    notes = [("Phases come from the browser's Navigation Timing for hard page loads (single-page "
              "route changes have no network phases)." if target == "page_loads" else
              "Phases come from the browser's Resource Timing for fetch/XHR calls.")
             + " Server processing and Database come from Server-Timing response headers and are "
             "carved out of the waiting time, and TLS out of the connect time, so nothing is "
             "counted twice. The chart stacks averages (averages add up; percentiles do not); "
             "Server and Database p75 cover only samples with a Server-Timing split."] + notes
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── Funnel conversion ──────────────────────────────────────────────────────

def _steps(value: Any) -> list[dict]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return [s for s in (value or []) if isinstance(s, dict) and str(s.get("match") or "").strip()]


def _step_condition(step: dict, index: int, params: dict) -> str:
    """windowFunnel condition for one saved step (same matching as the RUM
    funnel page; the match text is always a bound parameter)."""
    match = str(step.get("match") or "")
    key = f"step{index}"
    if step.get("type") == "action":
        params[key] = match
        return f"(event_type = 'action' AND action_name = %({key})s)"
    if "*" in match:
        params[key] = "^" + re.escape(match).replace(r"\*\*", ".*").replace(r"\*", "[^/]+") + "/?$"
        return f"(event_type = 'view' AND match(view_name, %({key})s))"
    params[key] = match
    return f"(event_type = 'view' AND view_name = %({key})s)"


@register_section(
    "rum_funnel_conversion", title="Funnel Conversion", category=CATEGORY,
    description="For each saved RUM funnel: sessions entering, completing each step in order and "
                "converting, with the biggest drop-off.",
    scope="fleet", requires=REQUIRES,
    options=[{"key": "max_funnels", "label": "Funnels to include", "type": "int", "default": 10,
              "min": 1, "max": 50}],
)
async def rum_funnel_conversion(ctx: SectionCtx) -> dict:
    sid, title = "rum_funnel_conversion", "Funnel Conversion"
    limit = int(ctx.opt("max_funnels"))
    desc = ("For each saved RUM funnel: sessions entering, completing each step in order within "
            "the funnel's time limit and converting, with the biggest drop-off.")
    app = ctx.application_id
    rows = (await ctx.db.execute(text(
        "SELECT id::text, application_id, name, steps, window_seconds FROM rum_funnels"
        + (" WHERE application_id = :app" if app else "") + " ORDER BY name, application_id"),
        {"app": app} if app else {})).fetchall()
    funnels = [{"id": r[0], "app": str(r[1] or ""), "name": str(r[2] or "Funnel"),
                "steps": _steps(r[3]), "window_s": _int(r[4]) or 3600} for r in rows]
    funnels = [f for f in funnels if f["steps"]]
    notes: list[str] = []
    n = await _raw_only_note(ctx, "Browser session detail")
    if n:
        notes.append(n)
    if not funnels:
        return _section(sid, title, description=desc,
                        notes=["No funnels are defined" + (" for this application" if app else "")
                               + ". Create one under APM → Real User Monitoring → Funnels."] + notes)

    results = []
    for f in funnels[:limit]:
        params = ctx.rum_params(fapp=f["app"], window_ms=max(60, f["window_s"]) * 1000)
        conds = [_step_condition(s, i, params) for i, s in enumerate(f["steps"])]
        levels = ", ".join(f"countIf(level >= {i + 1})" for i in range(len(conds)))
        got = await ctx.ch_rows(
            f"SELECT {levels}, count() FROM (SELECT windowFunnel(%(window_ms)s)("
            f"toUInt64(toUnixTimestamp64Milli(timestamp)), {', '.join(conds)}) AS level "
            f"FROM {EVENTS} WHERE timestamp >= %(f)s AND timestamp < %(t)s "
            "AND application_id = %(fapp)s AND sampled = 1 AND event_type IN ('view', 'action') "
            "GROUP BY env, session_id)", params)
        reached = [_int(v) for v in (got[0][:len(conds)] if got else [])] or [0] * len(conds)
        entered = reached[0]
        steps = []
        for i, s in enumerate(f["steps"]):
            prev = reached[i - 1] if i else reached[i]
            steps.append({"label": _clip(s.get("label") or s.get("match"), 60),
                          "match": _clip(f"{'Action' if s.get('type') == 'action' else 'Page'}: {s.get('match')}", 80),
                          "sessions": reached[i], "from_start": _share(reached[i], entered),
                          "from_prev": _share(reached[i], prev) if i else (100.0 if entered else None),
                          "drop": max(prev - reached[i], 0) if i else 0})
        drops = [(st["drop"], _share(st["drop"], steps[i - 1]["sessions"]), st["label"])
                 for i, st in enumerate(steps) if i and st["drop"] > 0]
        worst = max(drops, key=lambda d: (d[1] or 0, d[0]), default=None)
        results.append({**f, "steps": steps, "entered": entered, "converted": reached[-1],
                        "conversion": _share(reached[-1], entered), "worst": worst})

    measured = [r for r in results if r["conversion"] is not None]
    best = max(measured, key=lambda r: (r["conversion"], r["entered"]), default=None)
    low = min(measured, key=lambda r: (r["conversion"], -r["entered"]), default=None)
    worst_drop = max((r for r in results if r["worst"]), key=lambda r: (r["worst"][1] or 0, r["worst"][0]),
                     default=None)
    kpis = [
        _kpi("Funnels", _count(len(funnels)), "primary",
             f"{len(results)} included" if len(results) < len(funnels) else None),
        _kpi("Best conversion", _pct(best["conversion"]) if best else "—", "success" if best else "neutral",
             _clip(best["name"], 40) if best else "No sessions entered a funnel"),
        _kpi("Lowest conversion", _pct(low["conversion"]) if low else "—", "warning" if low else "neutral",
             _clip(low["name"], 40) if low else None),
        _kpi("Largest drop-off", _pct(worst_drop["worst"][1]) if worst_drop else "—",
             "danger" if worst_drop else "neutral",
             f"{_clip(worst_drop['name'], 30)}: before “{worst_drop['worst'][2]}”" if worst_drop else None),
    ]
    if len(results) == 1:
        r0 = results[0]
        charts = [{"title": f"{_clip(r0['name'], 60)}: sessions reaching each step",
                   "series": {"kind": "rank", "unit": "sessions", "color": "primary",
                              "points": [{"label": f"{i + 1}. {s['label']}", "value": s["sessions"]}
                                         for i, s in enumerate(r0["steps"])]}}]
    else:
        charts = ([{"title": "Conversion by funnel",
                    "series": {"kind": "rank", "unit": "%", "color": "primary",
                               "points": [{"label": _clip(r["name"], 50), "value": round(r["conversion"], 2)}
                                          for r in measured]}}] if measured else [])
    summary = [[_clip(r["name"], 60), r["app"] or "—", _count(len(r["steps"])), _count(r["entered"]),
                _count(r["converted"]), _pct(r["conversion"]),
                f"{r['worst'][2]} ({_pct(r['worst'][1])})" if r["worst"] else "—"] for r in results]
    tables = [{"title": _title_n("Funnel summary", len(results), len(funnels), "funnels"),
               "headers": ["Funnel", "Application", "Steps", "Sessions entered", "Converted",
                           "Conversion", "Largest drop-off (before step)"],
               "styles": ["text", "mono", "num", "num", "num", "pct-bar", "text"], "rows": summary}]
    for r in results:
        tables.append({
            "title": f"{_clip(r['name'], 80)} (within {_duration_ms(r['window_s'] * 1000.0)})",
            "headers": ["Step", "Matches", "Sessions", "From start", "From previous step", "Drop-off"],
            "styles": ["text", "mono", "num", "pct-bar", "pct", "num"],
            "rows": [[f"{i + 1}. {s['label']}", s["match"], _count(s["sessions"]), _pct(s["from_start"]),
                      _pct(s["from_prev"]), _count(s["drop"])] for i, s in enumerate(r["steps"])]})
    if not measured:
        notes.insert(0, "No session reached the first step of any funnel in this period.")
    notes.append("A session converts when it completes every step in order within the funnel's "
                 "time limit (ClickHouse windowFunnel over sampled sessions). Conversion from "
                 "start is relative to the sessions that reached step 1.")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── User actions and long tasks ────────────────────────────────────────────

ACTION_SORTS = {
    "count": ("Most frequent", "n DESC, name ASC"),
    "duration": ("Slowest (p75 duration)", "p75 DESC, n DESC"),
    "errors": ("Most errors", "errs DESC, n DESC"),
}
ACTION_TYPES = {"click": "Click", "rage_click": "Rage click", "custom": "Custom", "": "—"}
LONG_TASK_MS = 50.0


@register_section(
    "rum_user_actions", title="User Actions and Long Tasks", category=CATEGORY,
    description="What visitors clicked and did most (including rage clicks), and the pages whose "
                "long main-thread tasks block interaction.",
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(10, 100),
             {"key": "sort", "label": "Rank actions by", "type": "select", "default": "count",
              "choices": [[k, v[0]] for k, v in ACTION_SORTS.items()]}],
)
async def rum_user_actions(ctx: SectionCtx) -> dict:
    sid, title = "rum_user_actions", "User Actions and Long Tasks"
    top_n = int(ctx.opt("top_n"))
    sort = str(ctx.opt("sort"))
    sort_label, order_sql = ACTION_SORTS[sort]
    desc = ("What visitors clicked and did most (including rage clicks), and the pages whose long "
            "main-thread tasks block interaction.")
    base = _scope(ctx, " AND sampled = 1")
    params = ctx.rum_params(lim=top_n)
    notes: list[str] = []
    n = await _raw_only_note(ctx, "Browser action detail")
    if n:
        notes.append(n)
    tbt = f"greatest(duration_ms - {LONG_TASK_MS}, 0)"
    tot = await ctx.ch_rows(
        "SELECT countIf(event_type = 'action'), "
        "countIf(event_type = 'action' AND action_type = 'rage_click'), "
        "countIf(event_type = 'long_task'), "
        "uniqExactIf((application_id, env, session_id), event_type = 'long_task'), "
        f"sumIf({tbt}, event_type = 'long_task'), "
        "uniqExactIf((application_id, env, session_id), event_type = 'action'), "
        "uniqExactIf((view_name, action_name, action_type), event_type = 'action'), "
        "uniqExactIf(view_name, event_type = 'long_task') "
        f"FROM {EVENTS} WHERE {base} AND event_type IN ('action', 'long_task')", params)
    t = tot[0] if tot else (0,) * 8
    actions, rage, long_n, long_sessions, blocking, action_sessions, distinct, long_pages = (
        _int(t[0]), _int(t[1]), _int(t[2]), _int(t[3]), _num(t[4]), _int(t[5]), _int(t[6]), _int(t[7]))
    if not actions and not long_n:
        return _section(sid, title, description=desc,
                        notes=["No user actions or long tasks were recorded in this period."] + notes)

    having = " HAVING countIf(duration_ms > 0) > 0" if sort == "duration" else ""
    top = await ctx.ch_rows(
        "SELECT action_name AS name, action_type, view_name, count() AS n, "
        "uniqExact((application_id, env, session_id)), countIf(attributes['error'] = 'true') AS errs, "
        "quantileTDigestIf(0.75)(duration_ms, duration_ms > 0) AS p75, countIf(duration_ms > 0) "
        f"FROM {EVENTS} WHERE {base} AND event_type = 'action' "
        f"GROUP BY action_name, action_type, view_name{having} ORDER BY {order_sql} LIMIT %(lim)s", params)
    pages = await ctx.ch_rows(
        "SELECT view_name, count() AS n, uniqExact((application_id, env, session_id)), "
        f"quantileTDigest(0.75)(duration_ms), max(duration_ms), sum({tbt}) "
        f"FROM {EVENTS} WHERE {base} AND event_type = 'long_task' "
        "GROUP BY view_name ORDER BY n DESC, view_name ASC LIMIT %(lim)s", params)
    bucket, bp = ctx.ch_bucket("timestamp")
    trend = await ctx.ch_rows(
        f"SELECT {bucket} AS b, countIf(event_type = 'long_task'), "
        "countIf(event_type = 'action' AND action_type = 'rage_click') "
        f"FROM {EVENTS} WHERE {base} AND event_type IN ('action', 'long_task') "
        "GROUP BY b ORDER BY b", {**params, **bp})

    kpis = [
        _kpi("User actions", _count(actions), "primary", f"in {_count(action_sessions)} sessions"),
        _kpi("Rage clicks", _count(rage), "warning" if rage else "success",
             "three quick clicks on one element"),
        _kpi("Long tasks", _count(long_n), "warning" if long_n else "success",
             f"in {_count(long_sessions)} sessions"),
        _kpi("Main-thread blocking", _duration_ms(blocking if long_n else None), "info",
             "time beyond 50 ms in long tasks"),
    ]
    charts = []
    if top and sort == "count":
        charts.append({"title": "Most frequent actions",
                       "series": {"kind": "rank", "unit": "actions", "color": "primary",
                                  "points": [{"label": _clip(f"{r[0] or 'Action'} · {r[2] or '/'}", 60),
                                              "value": _int(r[3])} for r in top[:10]]}})
    if trend and (long_n or rage):
        charts.append({"title": "Long tasks and rage clicks over time",
                       "series": {"kind": "bars", "unit": "count", "series": [
                           {"name": "Long tasks", "points": [{"t": ctx.iso(_as_dt(ctx, r[0])), "v": _int(r[1])}
                                                             for r in trend]},
                           {"name": "Rage clicks", "points": [{"t": ctx.iso(_as_dt(ctx, r[0])), "v": _int(r[2])}
                                                              for r in trend]}]}})
    tables = []
    if top:
        tables.append({
            "title": _title_n(f"User actions — {sort_label.lower()}", len(top),
                              distinct if sort != "duration" else len(top), "actions"),
            "headers": ["Action", "Type", "Page", "Count", "Sessions", "p75 duration", "Errors"],
            "styles": ["text", "text", "mono", "num", "num", "num", "num"],
            "rows": [[_clip(r[0] or "Action", 80), ACTION_TYPES.get(str(r[1] or ""), str(r[1]).replace("_", " ").capitalize()),
                      _clip(r[2] or "/", 80), _count(r[3]), _count(r[4]),
                      _fmt_ms(_num(r[6]) if _int(r[7]) else None), _count(r[5])] for r in top]})
    elif actions:
        notes.insert(0, "No user action has a measured duration, so none is ranked as slowest.")
    else:
        notes.insert(0, "No user actions were recorded in this period.")
    if pages:
        tables.append({
            "title": _title_n("Pages with the most long tasks", len(pages), long_pages, "pages"),
            "headers": ["Page", "Long tasks", "Sessions affected", "p75 duration", "Longest", "Blocking time"],
            "styles": ["mono", "num", "num", "num", "num", "num"],
            "rows": [[_clip(r[0] or "/", 80), _count(r[1]), _count(r[2]), _fmt_ms(_num(r[3])),
                      _fmt_ms(_num(r[4])), _duration_ms(_num(r[5]))] for r in pages]})
    else:
        notes.insert(0, "No long tasks were recorded in this period.")
    notes.append("A long task is main-thread work longer than 50 ms; blocking time is the part of "
                 "each task beyond 50 ms (as in Total Blocking Time). Counts come from sampled "
                 "sessions. Element selectors and user identities are not included.")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── JavaScript issue status ────────────────────────────────────────────────

ISSUE_ORDER = ("regressed", "new", "open", "resolved", "ignored")
ISSUE_FILTERS = {
    "all": ("All issues", ISSUE_ORDER),
    "active": ("New, open and regressed", ("regressed", "new", "open")),
    "new": ("New", ("new",)),
    "regressed": ("Regressed", ("regressed",)),
    "resolved": ("Resolved", ("resolved",)),
    "ignored": ("Ignored", ("ignored",)),
}


def _effective_status(stored: Optional[dict], seen: Optional[dict], first_ever: Optional[datetime],
                      frm: datetime) -> str:
    """new: first occurrence on record falls in the period (and nobody triaged
    it); regressed: resolved, then seen again; else the stored state."""
    state = str((stored or {}).get("status") or "open")
    if state == "resolved":
        resolved_at = _naive((stored or {}).get("resolved_at"))
        last = _naive((seen or {}).get("last"))
        if resolved_at is not None and last is not None and last > resolved_at:
            return "regressed"
        return "resolved"
    if state == "ignored":
        return "ignored"
    first = _naive(first_ever)
    if seen and first is not None and first >= _naive(frm):
        return "new"
    return "open"


@register_section(
    "rum_issue_status", title="JavaScript Issue Status", category=CATEGORY,
    description="Browser JavaScript error groups seen in the period with their triage state — new, "
                "open, regressed, resolved or ignored — and the release each first appeared in.",
    scope="fleet", requires=REQUIRES,
    options=[top_n_option(25, 200),
             {"key": "status", "label": "Show", "type": "select", "default": "all",
              "choices": [[k, v[0]] for k, v in ISSUE_FILTERS.items()]}],
)
async def rum_issue_status(ctx: SectionCtx) -> dict:
    sid, title = "rum_issue_status", "JavaScript Issue Status"
    top_n = int(ctx.opt("top_n"))
    show = str(ctx.opt("status"))
    show_label, wanted = ISSUE_FILTERS[show]
    desc = ("Browser JavaScript error groups seen in the period with their triage state — new, open, "
            "regressed, resolved or ignored — and the release each first appeared in.")
    notes: list[str] = []
    ret = await _retention(ctx)
    n = ctx.window_note(ret["raw"], "Browser error detail")
    if n:
        notes.append(n)
    params = ctx.rum_params()
    seen: dict[tuple, dict] = {}
    for r in await ctx.ch_rows(
            f"SELECT application_id, env, {FINGERPRINT} AS fp, count() AS n, uniqExact(session_id), "
            "min(timestamp), max(timestamp), argMax(error_message, timestamp), argMax(error_type, timestamp) "
            f"FROM {EVENTS} WHERE {_scope(ctx)} AND event_type = 'error' "
            f"GROUP BY application_id, env, fp ORDER BY n DESC LIMIT {MAX_GROUPS}", params):
        seen[(str(r[0]), str(r[1]), str(r[2]))] = {
            "count": _int(r[3]), "sessions": _int(r[4]), "first": r[5], "last": r[6],
            "message": r[7], "type": r[8]}

    app = ctx.application_id
    stored: dict[tuple, dict] = {}
    for r in (await ctx.db.execute(text(
            "SELECT application_id, env, fingerprint, status, first_seen_release, resolved_at "
            "FROM rum_issues" + (" WHERE application_id = :app" if app else "")),
            {"app": app} if app else {})).fetchall():
        stored[(str(r[0]), str(r[1] or ""), str(r[2]))] = {
            "status": r[3], "first_seen_release": r[4], "resolved_at": r[5]}
    frm, to = _naive(ctx.frm), _naive(ctx.to)
    resolved_here = {k for k, s in stored.items() if s["status"] == "resolved"
                     and _naive(s["resolved_at"]) is not None and frm <= _naive(s["resolved_at"]) < to}
    keys = list(seen) + sorted(k for k in resolved_here if k not in seen)
    if not keys:
        return _section(sid, title, description=desc,
                        notes=["No browser JavaScript errors were reported in this period."] + notes)

    # First occurrence on record (raw retention), first release and the latest
    # error text for every listed group, including resolved groups not seen now.
    history: dict[tuple, dict] = {}
    fps = sorted({k[2] for k in keys})[:MAX_GROUPS]
    for r in await ctx.ch_rows(
            f"SELECT application_id, env, {FINGERPRINT} AS fp, min(timestamp), "
            "argMin(service_version, timestamp), argMax(error_message, timestamp), "
            "argMax(error_type, timestamp) "
            f"FROM {EVENTS} WHERE timestamp >= %(since)s AND timestamp < %(t)s{ctx.rum_app_clause()} "
            "AND event_type = 'error' AND fp IN %(fps)s GROUP BY application_id, env, fp",
            ctx.rum_params(since=_now() - timedelta(days=ret["raw"]), fps=tuple(fps))):
        history[(str(r[0]), str(r[1]), str(r[2]))] = {
            "first": r[3], "release": r[4], "message": r[5], "type": r[6]}

    items = []
    for k in keys:
        s, h, st = seen.get(k), history.get(k, {}), stored.get(k)
        status = _effective_status(st, s, h.get("first") or (s or {}).get("first"), ctx.frm)
        release = str((st or {}).get("first_seen_release") or h.get("release") or "")
        items.append({
            "key": k, "status": status,
            "message": (s or {}).get("message") or h.get("message") or "",
            "type": (s or {}).get("type") or h.get("type") or "",
            "count": (s or {}).get("count", 0), "sessions": (s or {}).get("sessions", 0),
            "first": h.get("first") or (s or {}).get("first"), "last": (s or {}).get("last"),
            "release": release})
    counts = {state: sum(1 for i in items if i["status"] == state) for state in ISSUE_ORDER}
    total_errors = sum(i["count"] for i in items)
    listed = [i for i in items if i["status"] in wanted]
    listed.sort(key=lambda i: (ISSUE_ORDER.index(i["status"]), -i["count"], str(i["message"])))
    shown = listed[:top_n]

    kpis = [
        _kpi("Issues seen", _count(len(seen)), "primary", f"{_count(total_errors)} errors in the period"),
        _kpi("New", _count(counts["new"]), "danger" if counts["new"] else "success",
             "first seen in this period"),
        _kpi("Open", _count(counts["open"]), "warning" if counts["open"] else "success",
             "seen before and not resolved"),
        _kpi("Regressed", _count(counts["regressed"]), "danger" if counts["regressed"] else "success",
             "recurred after being resolved"),
        _kpi("Resolved", _count(counts["resolved"]), "success", "and not seen since"),
        _kpi("Ignored", _count(counts["ignored"]), "neutral", "still occurring, triaged as ignored"),
    ]
    charts = [{"title": "Issues by status",
               "series": {"kind": "donut", "unit": "issues",
                          "points": [{"label": s.capitalize(), "value": counts[s]}
                                     for s in ISSUE_ORDER if counts[s]]}}]
    rows = [[_clip(i["message"] or "(no message)", 90), _clip(i["type"] or "—", 40), i["status"],
             _count(i["count"]), _count(i["sessions"]), ctx.fmt_short(i["first"]),
             ctx.fmt_short(i["last"]), _clip(i["release"] or "—", 40)] for i in shown]
    tables = [{"title": _title_n(f"{show_label} by status and occurrences", len(shown), len(listed), "issues"),
               "headers": ["Issue", "Type", "Status", "Errors", "Sessions", "First seen", "Last seen",
                           "First seen release"],
               "styles": ["text", "text", "status", "num", "num", "text", "text", "mono"],
               "rows": rows}] if rows else []
    if not rows:
        notes.insert(0, f"No issues match “{show_label}” in this period.")
    notes.append("New: first occurrence on record falls in this period. Regressed: resolved, then "
                 "seen again. Resolved and ignored are the triage states set on the RUM errors page; "
                 "resolved issues that did not recur are listed when they were resolved in this "
                 f"period. First seen looks back {ret['raw']} days (raw browser-event retention). "
                 "Error text is scrubbed of e-mail addresses and credentials at ingest.")
    return _section(sid, title, description=desc, kpis=kpis, charts=charts, tables=tables, notes=notes)


# ─── Templates ──────────────────────────────────────────────────────────────

template(
    "rum_core_web_vitals_trend", "Core Web Vitals Trend",
    "Daily 75th-percentile LCP, INP and CLS from real visitors with good / poor shares and the "
    "change against the previous period, plus Core Web Vitals by release and the countries "
    "with the slowest page loads.",
    "experience",
    ["rum_vitals_trend",
     S("rum_vitals_trend", "Interactivity (INP) Rating Trend", rating_vital="inp"),
     S("rum_releases", "Core Web Vitals by Release", top_n=5),
     S("rum_geography", "Slowest Countries (LCP p75)", sort="lcp", top_n=10)],
    audience=("Application owner", "Executive", "IT manager"),
    tags=("rum", "core web vitals", "lcp", "inp", "cls", "trend"),
    default_period="last_30d", frequency="weekly", icon="Gauge", featured=True,
)

template(
    "rum_audience_by_country", "Audience by Country",
    "Where visitors come from: sessions, page views, error-session rate and Core Web Vitals per "
    "country, and the countries with the highest error-session rate.",
    "experience",
    [S("rum_geography", "Top 25 Countries by Sessions", top_n=25),
     S("rum_geography", "Countries with the Highest Error-Session Rate", sort="error_rate", top_n=10)],
    audience=("Application owner", "Executive"), tags=("rum", "geography", "audience", "countries"),
    default_period="last_30d", frequency="monthly", icon="Globe",
)

template(
    "rum_release_experience", "Release Experience Comparison",
    "Each release side by side — sessions, error-session rate and Core Web Vitals — with the "
    "JavaScript issues that are new or regressed and the release they first appeared in.",
    "experience",
    [S("rum_releases", None, top_n=10),
     S("rum_issue_status", "New and Regressed JavaScript Issues", status="active", top_n=25)],
    audience=("Application owner", "Service owner"), tags=("rum", "releases", "deployments", "regressions"),
    default_period="last_30d", frequency="weekly", icon="Layers",
)

template(
    "rum_slow_failing_resources", "Slow and Failing Resources",
    "The 25 slowest and the failing scripts, stylesheets, images and API calls real browsers "
    "loaded, by resource type, with the time breakdown of API calls.",
    "experience",
    [S("rum_slow_resources", None, top_n=25),
     S("rum_slow_resources", "Slowest and Failing API Calls", resource_type="fetch", top_n=10),
     S("rum_load_breakdown", None, target="api")],
    audience=("Application owner", "Systems engineer"), tags=("rum", "resources", "errors", "performance"),
    default_period="last_7d", frequency="weekly", icon="Boxes",
)

template(
    "rum_page_load_breakdown", "Page Load Breakdown",
    "Where page-load time goes — DNS, connect, TLS, waiting, server, database, download and "
    "browser processing — for page loads and API calls, with the slowest pages and endpoints.",
    "experience",
    [S("rum_load_breakdown", None, target="page_loads", top_n=15),
     S("rum_load_breakdown", None, target="api", top_n=15)],
    audience=("Application owner", "Systems engineer", "Network engineer"),
    tags=("rum", "page load", "ttfb", "server timing"),
    default_period="last_7d", frequency="weekly", icon="BarChart3",
)

template(
    "rum_funnels", "Funnel Conversion",
    "Conversion and drop-off for every saved RUM funnel: sessions entering, completing each step "
    "in order and converting.",
    "experience",
    [S("rum_funnel_conversion", None, max_funnels=20)],
    audience=("Application owner", "Executive"), tags=("rum", "funnels", "conversion", "journeys"),
    default_period="last_7d", frequency="weekly", icon="Route",
)

template(
    "rum_user_actions_long_tasks", "User Actions and Long Tasks",
    "The most frequent and slowest user actions, rage clicks, the pages whose long main-thread "
    "tasks block interaction, and the interactivity (INP) trend.",
    "experience",
    [S("rum_user_actions", None, top_n=25),
     S("rum_user_actions", "Slowest User Actions", sort="duration", top_n=10),
     S("rum_vitals_trend", "Interactivity (INP) Trend", rating_vital="inp")],
    audience=("Application owner",), tags=("rum", "actions", "rage clicks", "long tasks", "inp"),
    default_period="last_7d", frequency="weekly", icon="MousePointerClick",
)

template(
    "rum_javascript_issue_status", "JavaScript Issue Status",
    "Browser JavaScript error groups with their triage state — new, open, regressed, resolved "
    "or ignored — the release each first appeared in, and the error rate real visitors saw.",
    "experience",
    [S("rum_issue_status", None, top_n=50), "rum_reliability"],
    audience=("Application owner", "Service owner"), tags=("rum", "javascript", "errors", "issues", "triage"),
    default_period="last_7d", frequency="weekly", icon="Bell",
)

template(
    "rum_monthly_dx_review", "Monthly Digital Experience Review",
    "A month of real-user experience for leadership and application owners: audience and "
    "engagement, the Core Web Vitals trend, top pages, device and browser mix, countries, "
    "releases, browser reliability, JavaScript issues and funnel conversion.",
    "experience",
    ["rum_audience", "rum_vitals_trend", "rum_top_pages", "rum_segments",
     S("rum_geography", None, top_n=10), S("rum_releases", None, top_n=5), "rum_reliability",
     S("rum_issue_status", "New and Regressed JavaScript Issues", status="active", top_n=10),
     S("rum_funnel_conversion", None, max_funnels=5)],
    audience=("Executive", "IT manager", "Application owner"),
    tags=("rum", "monthly review", "digital experience", "web vitals"),
    default_period="previous_month", frequency="monthly", icon="Briefcase",
)
