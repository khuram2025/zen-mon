"""NetFlow threat-intelligence IP reputation (CR-10), offline capable.

Feeds (``netflow_ti_feeds``) are prefix lists: a URL the appliance downloads
(``source='url'``, disabled until an operator enables it, refreshed every
``refresh_hours`` by ``netflow_ti_loop``) or a list an operator uploads
(``source='upload'``, for air-gapped appliances). Their prefixes live in
``netflow_ti_entries``. No reputation data ships with the product: public
lists change daily and carry their own licences, so the catalog below only
*suggests* URL feeds (with their licence notes) that an operator can add.

Matching (the ``threat_intel`` detector): the distinct public addresses of
each closed bucket are looked up in an in-memory interval index built from
every enabled feed (IPv4 and IPv6, nested prefixes from different feeds
resolved by a sweep into disjoint intervals, so a lookup is one binary
search). The index is rebuilt when the feed set changes.

Parsing is forgiving about formats (plain lists, ``prefix ; comment``
Spamhaus-style, ``#`` comments, CSV first column) and strict about content:
prefixes wider than /8 (IPv4) or /16 (IPv6) and non-public ranges are
ignored -- aggregate lists such as FireHOL level 1 include bogons and private
space, which would otherwise flag every internal host.
"""

from __future__ import annotations

import asyncio
import bisect
import ipaddress
import logging
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("zenplus.netflow.ti")

ADVISORY_LOCK = 1515074406
LOOP_INTERVAL_S = 600.0
STARTUP_DELAY_S = 90.0
FETCH_TIMEOUT_S = 30.0
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_UPLOAD_CHARS = 16 * 1024 * 1024
MAX_ENTRIES = 500_000
INSERT_CHUNK = 20_000
INDEX_CHECK_S = 60.0
MAX_DISTINCT_ADDRESSES = 1_000_000
MAX_MATCHED_ADDRESSES = 2_000
MIN_PREFIX = {4: 8, 6: 16}
CATEGORIES = ("malicious", "botnet_c2", "malware", "scanner", "spam", "tor_exit", "anonymizer", "custom")
HIGH_RISK_CATEGORIES = frozenset({"botnet_c2", "malware", "malicious"})

# Suggested URL feeds. Nothing is downloaded until an operator adds and
# enables one; the licence note is shown next to it.
CATALOG: tuple[dict, ...] = (
    {
        "key": "feodo",
        "name": "abuse.ch Feodo Tracker (botnet C2)",
        "url": "https://feodotracker.abuse.ch/downloads/ipblocklist_recommended.txt",
        "category": "botnet_c2",
        "refresh_hours": 24,
        "licence": "CC0 1.0 (public domain dedication) — see abuse.ch terms of use",
    },
    {
        "key": "spamhaus_drop",
        "name": "Spamhaus DROP (hijacked netblocks)",
        "url": "https://www.spamhaus.org/drop/drop.txt",
        "category": "malicious",
        "refresh_hours": 24,
        "licence": "Free to use under the Spamhaus DROP terms; check them for commercial redistribution",
    },
    {
        "key": "tor_exit",
        "name": "Tor Project exit nodes",
        "url": "https://check.torproject.org/torbulkexitlist",
        "category": "tor_exit",
        "refresh_hours": 6,
        "licence": "Published by the Tor Project for public use",
    },
)


def _nf():
    from app.api.v1.netflow import access, common, filters, sql

    return common, sql, filters, access


# ── Parsing ──────────────────────────────────────────────────────────────────

@dataclass
class ParseStats:
    lines: int = 0
    accepted: int = 0
    invalid: int = 0
    non_public: int = 0
    too_broad: int = 0
    duplicates: int = 0
    truncated: bool = False
    samples_invalid: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"lines": self.lines, "accepted": self.accepted, "invalid": self.invalid,
                "non_public": self.non_public, "too_broad": self.too_broad, "duplicates": self.duplicates,
                "truncated": self.truncated, "samples_invalid": self.samples_invalid[:5]}

    def summary(self) -> str:
        parts = [f"{self.accepted:,} prefix{'' if self.accepted == 1 else 'es'}"]
        if self.invalid:
            parts.append(f"{self.invalid:,} unreadable lines skipped")
        if self.non_public:
            parts.append(f"{self.non_public:,} private/special-use prefixes ignored")
        if self.too_broad:
            parts.append(f"{self.too_broad:,} over-broad prefixes ignored")
        if self.truncated:
            parts.append(f"truncated at {MAX_ENTRIES:,}")
        return "; ".join(parts)


def _public_network(net: ipaddress.IPv4Network | ipaddress.IPv6Network) -> bool:
    from app.services.netflow_detection import SPECIAL_USE

    for spec in SPECIAL_USE:
        s = ipaddress.ip_network(spec)
        if s.version == net.version and (net.subnet_of(s)):
            return False
    return True


def parse_feed(content: str) -> tuple[list[str], ParseStats]:
    """Prefixes (canonical CIDR text) from a feed body, and what was skipped."""
    stats = ParseStats()
    seen: set[str] = set()
    out: list[str] = []
    for raw in content.splitlines():
        line = raw.strip()
        if not line:
            continue
        for marker in ("#", ";", "//"):
            idx = line.find(marker)
            if idx == 0:
                line = ""
                break
            if idx > 0:
                line = line[:idx].strip()
        if not line:
            continue
        stats.lines += 1
        token = line.replace(",", " ").replace("\t", " ").split()[0].strip().strip('"')
        # "1.2.3.4-1.2.3.9" ranges are not supported; neither are hostnames.
        try:
            net = ipaddress.ip_network(token, strict=False)
        except ValueError:
            stats.invalid += 1
            if len(stats.samples_invalid) < 5:
                stats.samples_invalid.append(token[:60])
            continue
        if isinstance(net, ipaddress.IPv6Network) and net.network_address.ipv4_mapped is not None and net.prefixlen >= 96:
            net = ipaddress.ip_network(f"{net.network_address.ipv4_mapped}/{net.prefixlen - 96}")
        if net.prefixlen < MIN_PREFIX[net.version]:
            stats.too_broad += 1
            continue
        if not _public_network(net):
            stats.non_public += 1
            continue
        key = str(net)
        if key in seen:
            stats.duplicates += 1
            continue
        if len(out) >= MAX_ENTRIES:
            stats.truncated = True
            break
        seen.add(key)
        out.append(key)
    stats.accepted = len(out)
    return out, stats


# ── Storage ──────────────────────────────────────────────────────────────────

async def replace_entries(db: AsyncSession, feed_id: Any, prefixes: list[str]) -> int:
    """Replace a feed's prefixes (caller commits)."""
    await db.execute(text("DELETE FROM netflow_ti_entries WHERE feed_id = :f"), {"f": feed_id})
    for i in range(0, len(prefixes), INSERT_CHUNK):
        chunk = prefixes[i:i + INSERT_CHUNK]
        await db.execute(
            text("INSERT INTO netflow_ti_entries (feed_id, prefix) "
                 "SELECT :f, p FROM unnest(CAST(:ps AS cidr[])) AS p ON CONFLICT DO NOTHING"),
            {"f": feed_id, "ps": chunk},
        )
    await db.execute(text("UPDATE netflow_ti_feeds SET entry_count = :n, updated_at = NOW() WHERE id = :f"),
                     {"n": len(prefixes), "f": feed_id})
    invalidate_index()
    return len(prefixes)


async def _set_status(db: AsyncSession, feed_id: Any, status: str, *, refreshed: bool) -> None:
    await db.execute(
        text("UPDATE netflow_ti_feeds SET last_status = :s, "
             + ("last_refreshed_at = NOW(), " if refreshed else "")
             + "updated_at = NOW() WHERE id = :f"),
        {"s": status[:2000], "f": feed_id},
    )


# ── Feed download: public destinations only ─────────────────────────────────
# A feed URL is chosen by a netflow.manage user, and the appliance fetches it
# from inside the network. Without a destination check that is an SSRF and
# port-scan oracle: loopback services, the database port and metadata
# endpoints answered differently in the feed status. So every hop (the URL
# and each redirect) resolves its host and must land on public internet
# addresses only; the connection then goes to the address that was checked
# (TLS keeps the host name for SNI and certificate checks), so a second DNS
# answer cannot swap in an internal one. Feed servers inside the network can
# be allowed explicitly with NETFLOW_TI_FETCH_ALLOW (comma-separated CIDRs).
# Download failures are reported generically: which internal port refused or
# hung up is not the operator's business through this path.

FETCH_ALLOW_ENV = "NETFLOW_TI_FETCH_ALLOW"
MAX_REDIRECTS = 5
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
UNREACHABLE = "the feed server could not be reached or did not answer"


class FetchRefused(ValueError):
    """The feed URL (or a redirect) points at a destination that is not allowed."""


def _allowed_networks() -> list:
    import os

    out = []
    for item in (os.environ.get(FETCH_ALLOW_ENV) or "").split(","):
        item = item.strip()
        if not item:
            continue
        try:
            out.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            logger.warning("%s: ignoring invalid network %r", FETCH_ALLOW_ENV, item)
    return out


def check_destination(address: str) -> None:
    """FetchRefused unless ``address`` is a public internet address (or in
    NETFLOW_TI_FETCH_ALLOW): loopback, private, shared (CGN), link-local
    (including cloud metadata 169.254.169.254), multicast, reserved and
    unspecified addresses are refused, IPv4-mapped IPv6 judged as IPv4."""
    try:
        addr = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        raise FetchRefused("the feed address is not valid") from None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if any(addr in net for net in _allowed_networks()):
        return
    if not addr.is_global or addr.is_multicast or addr.is_reserved or addr.is_unspecified:
        raise FetchRefused("feed URLs must point to a public internet address")


def check_url_literal(url: str) -> None:
    """Early check for feed create/update: FetchRefused when the URL's host
    is an IP literal that is not allowed (host names are checked at fetch)."""
    import httpx

    try:
        host = httpx.URL(url).host
    except Exception:  # noqa: BLE001 - malformed URL
        raise FetchRefused("the feed URL is not valid") from None
    if not host:
        raise FetchRefused("the feed URL has no host")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host.lower() in ("localhost", "localhost.localdomain") or host.lower().endswith(".localhost"):
            raise FetchRefused("feed URLs must point to a public internet address") from None
        return
    check_destination(host)


async def _resolve_public(host: str, port: int) -> str:
    """One checked address for ``host``; every address it resolves to must
    be allowed (a mixed public/internal answer is refused)."""
    import socket

    try:
        ipaddress.ip_address(host)
        check_destination(host)
        return host
    except ValueError as exc:
        if isinstance(exc, FetchRefused):
            raise
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError:
        raise ValueError("the feed host name does not resolve") from None
    addresses = list(dict.fromkeys(str(info[4][0]) for info in infos))
    if not addresses:
        raise ValueError("the feed host name does not resolve")
    for address in addresses:
        check_destination(address)
    return addresses[0]


async def fetch_url(url: str) -> str:
    """Download a feed (http/https only, public destinations only, redirects
    re-checked, size-capped)."""
    import httpx

    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("Only http:// and https:// feed URLs are supported")
    timeout = httpx.Timeout(FETCH_TIMEOUT_S, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                 headers={"User-Agent": "ZenPlus-NetFlow-TI/1"}) as client:
        current = httpx.URL(url)
        for _hop in range(MAX_REDIRECTS + 1):
            if current.scheme not in ("http", "https") or not current.host:
                raise FetchRefused("Only http:// and https:// feed URLs are supported")
            port = current.port or (443 if current.scheme == "https" else 80)
            address = await _resolve_public(current.host, port)
            extensions = {}
            if current.scheme == "https" and address != current.host:
                extensions["sni_hostname"] = current.host
            request = client.build_request(
                "GET", current.copy_with(host=address),
                headers={"Host": current.netloc.decode("ascii")}, extensions=extensions)
            try:
                resp = await client.send(request, stream=True)
            except httpx.HTTPError:
                raise ValueError(UNREACHABLE) from None
            try:
                if resp.status_code in _REDIRECT_CODES and resp.headers.get("location"):
                    current = current.join(resp.headers["location"])
                    continue
                if resp.status_code != 200:
                    raise ValueError(f"HTTP {resp.status_code}")
                chunks: list[bytes] = []
                size = 0
                try:
                    async for chunk in resp.aiter_bytes():
                        size += len(chunk)
                        if size > MAX_DOWNLOAD_BYTES:
                            raise ValueError(f"Feed is larger than {MAX_DOWNLOAD_BYTES // (1024 * 1024)} MB")
                        chunks.append(chunk)
                except httpx.HTTPError:
                    raise ValueError(UNREACHABLE) from None
                return b"".join(chunks).decode("utf-8", errors="replace")
            finally:
                await resp.aclose()
        raise ValueError(f"more than {MAX_REDIRECTS} redirects")


async def refresh_feed(db: AsyncSession, feed_id: Any, *, content: str | None = None) -> dict:
    """Fetch (URL feeds) or take ``content`` (uploads), parse, replace entries.
    Keeps the previous entries when the download fails. Caller commits."""
    row = (await db.execute(text("SELECT id, source, url FROM netflow_ti_feeds WHERE id = :f"),
                            {"f": feed_id})).first()
    if row is None:
        raise LookupError("feed not found")
    fetched = content is None
    try:
        if content is None:
            if row[1] != "url" or not row[2]:
                raise ValueError("This feed has no URL; upload its list instead")
            content = await asyncio.wait_for(fetch_url(row[2]), timeout=FETCH_TIMEOUT_S + 15)
    except Exception as exc:  # noqa: BLE001 - recorded on the feed
        # Only reasons this module words itself are shown; a transport
        # error's own text (refused vs reset vs hung up) would tell internal
        # services apart (SSRF oracle).
        if isinstance(exc, asyncio.TimeoutError):
            reason = UNREACHABLE
        elif isinstance(exc, ValueError):
            reason = str(exc) or UNREACHABLE
        else:
            logger.info("netflow TI feed %s download failed", feed_id, exc_info=True)
            reason = UNREACHABLE
        status = f"Download failed ({reason[:200]}); previous entries kept"
        await _set_status(db, feed_id, status, refreshed=False)
        return {"ok": False, "status": status}
    prefixes, stats = parse_feed(content)
    if fetched:
        # Never echo what a downloaded URL returned (not even unreadable
        # lines): the counts say enough, and the text of a page the appliance
        # fetched is not shown back through the feed. Uploads keep their
        # samples, the operator sent that text.
        stats.samples_invalid = []
    if not prefixes and stats.lines:
        status = f"No usable prefixes ({stats.summary()}); previous entries kept"
        await _set_status(db, feed_id, status, refreshed=False)
        return {"ok": False, "status": status, "stats": stats.as_dict()}
    await replace_entries(db, feed_id, prefixes)
    status = f"OK: {stats.summary()}"
    await _set_status(db, feed_id, status, refreshed=True)
    return {"ok": True, "status": status, "stats": stats.as_dict(), "entries": len(prefixes)}


FEED_COLUMNS = ("id, name, source, url, category, enabled, refresh_hours, last_refreshed_at, last_status, "
                "entry_count, created_by, updated_by, created_at, updated_at")


def feed_item(row: Any) -> dict:
    d = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)

    def iso(v):
        if v is None:
            return None
        v = v if v.tzinfo else v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    return {
        "id": str(d["id"]), "name": d["name"], "source": d["source"], "url": d.get("url"),
        "category": d["category"], "enabled": bool(d["enabled"]), "refresh_hours": d["refresh_hours"],
        "last_refreshed_at": iso(d.get("last_refreshed_at")), "last_status": d.get("last_status"),
        "entry_count": int(d.get("entry_count") or 0),
        "created_at": iso(d.get("created_at")), "updated_at": iso(d.get("updated_at")),
    }


# ── Interval index ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeedInfo:
    id: str
    name: str
    category: str

    def as_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "category": self.category}


class TiIndex:
    """Disjoint intervals per address family; each carries the feeds listing it."""

    def __init__(self, entries: Iterable[tuple[str, str]], feeds: dict[str, FeedInfo], version: str = ""):
        self.feeds = feeds
        self.version = version
        self.prefixes = 0
        events: dict[int, list[tuple[int, int, str]]] = {4: [], 6: []}
        for feed_id, prefix in entries:
            try:
                net = ipaddress.ip_network(prefix, strict=False)
            except ValueError:
                continue
            lo = int(net.network_address)
            hi = int(net.broadcast_address) + 1
            events[net.version].append((lo, 1, feed_id))
            events[net.version].append((hi, -1, feed_id))
            self.prefixes += 1
        self._starts: dict[int, list[int]] = {}
        self._payload: dict[int, list[tuple[str, ...] | None]] = {}
        for fam, evs in events.items():
            evs.sort(key=lambda e: (e[0], e[1]))
            starts: list[int] = []
            payload: list[tuple[str, ...] | None] = []
            active: Counter = Counter()
            i = 0
            while i < len(evs):
                pos = evs[i][0]
                while i < len(evs) and evs[i][0] == pos:
                    _, delta, fid = evs[i]
                    active[fid] += delta
                    if active[fid] <= 0:
                        del active[fid]
                    i += 1
                cur = tuple(sorted(active)) if active else None
                if payload and payload[-1] == cur:
                    continue
                starts.append(pos)
                payload.append(cur)
            self._starts[fam] = starts
            self._payload[fam] = payload

    @property
    def empty(self) -> bool:
        return self.prefixes == 0

    def lookup(self, ip: str) -> list[FeedInfo]:
        try:
            addr = ipaddress.ip_address(ip.strip())
        except ValueError:
            return []
        if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
            addr = addr.ipv4_mapped
        starts = self._starts.get(addr.version) or []
        i = bisect.bisect_right(starts, int(addr)) - 1
        if i < 0:
            return []
        hit = self._payload[addr.version][i]
        if not hit:
            return []
        return [self.feeds[f] for f in hit if f in self.feeds]


_index_state: dict[str, Any] = {"index": None, "checked": 0.0, "lock": None, "loop": None}


def invalidate_index() -> None:
    _index_state["checked"] = 0.0
    _index_state["version"] = None


def _index_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    if _index_state.get("loop") is not loop:
        _index_state.update(lock=asyncio.Lock(), loop=loop)
    return _index_state["lock"]


async def _version(db: AsyncSession) -> str:
    row = (await db.execute(text(
        "SELECT count(*), max(updated_at), max(last_refreshed_at), coalesce(sum(entry_count), 0) "
        "FROM netflow_ti_feeds WHERE enabled"
    ))).first()
    return "|".join(str(x) for x in row)


async def get_index(db: AsyncSession | None = None) -> TiIndex:
    """The current index (rebuilt when the enabled feed set changed)."""
    if _index_state["index"] is not None and time.monotonic() - _index_state["checked"] < INDEX_CHECK_S:
        return _index_state["index"]
    async with _index_lock():
        if _index_state["index"] is not None and time.monotonic() - _index_state["checked"] < INDEX_CHECK_S:
            return _index_state["index"]
        if db is None:
            from app.core.database import AsyncSessionLocal

            async with AsyncSessionLocal() as own:
                index = await _build_if_changed(own)
        else:
            index = await _build_if_changed(db)
        _index_state.update(index=index, checked=time.monotonic())
        return index


async def _build_if_changed(db: AsyncSession) -> TiIndex:
    version = await _version(db)
    current = _index_state.get("index")
    if current is not None and current.version == version:
        return current
    feeds = {
        str(r[0]): FeedInfo(str(r[0]), r[1], r[2])
        for r in (await db.execute(text("SELECT id, name, category FROM netflow_ti_feeds WHERE enabled"))).all()
    }
    rows = (await db.execute(text(
        "SELECT e.feed_id::text, e.prefix::text FROM netflow_ti_entries e "
        "JOIN netflow_ti_feeds f ON f.id = e.feed_id WHERE f.enabled"
    ))).all()
    started = time.monotonic()
    index = await asyncio.to_thread(TiIndex, [(r[0], r[1]) for r in rows], feeds, version)
    logger.info("netflow TI: index built from %d prefixes of %d feeds in %.2fs",
                index.prefixes, len(feeds), time.monotonic() - started)
    return index


# ── Detector ─────────────────────────────────────────────────────────────────

async def detect(ctx: Any, out: Any) -> None:
    """The ``threat_intel`` detector (called by netflow_detection.run_detectors)."""
    from app.services import netflow_detection as det

    common, sql, *_ = _nf()
    index = await get_index()
    det.annotate_coverage("threat_intel", ctx.coverage, out)
    if index.empty:
        out.status = "unavailable"
        out.reason = "No enabled threat-intelligence feed has entries; add or enable a feed"
        return
    cfg = ctx.cfg("threat_intel")
    where, params = ctx.where()
    params["cap"] = MAX_DISTINCT_ADDRESSES + 1
    public_src = f"NOT {det._in_ranges('src', det.SPECIAL_USE)}"
    public_dst = f"NOT {det._in_ranges('dst', det.SPECIAL_USE)}"
    q = f"""
        SELECT {det._disp('a')} FROM (
            SELECT DISTINCT {sql.SRC} AS a FROM {det._raw()} WHERE {where} AND {public_src}
            UNION DISTINCT
            SELECT DISTINCT {sql.DST} AS a FROM {det._raw()} WHERE {where} AND {public_dst}
        ) LIMIT %(cap)s
    """
    res = await common.run_query(q, params, budget_s=ctx.budget_s, label="detect-ti-distinct")
    addresses = [r[0] for r in res.result_rows]
    if len(addresses) > MAX_DISTINCT_ADDRESSES:
        out.truncated = True
        out.notes.append(f"Only the first {MAX_DISTINCT_ADDRESSES:,} distinct public addresses were checked")
        addresses = addresses[:MAX_DISTINCT_ADDRESSES]
    out.notes.append(f"{len(addresses):,} distinct public addresses checked against {index.prefixes:,} "
                     f"prefixes from {len(index.feeds)} feed{'s' if len(index.feeds) != 1 else ''}")
    matched = await asyncio.to_thread(lambda: {a: index.lookup(a) for a in addresses})
    matched = {a: f for a, f in matched.items() if f}
    if not matched:
        return
    if len(matched) > MAX_MATCHED_ADDRESSES:
        out.truncated = True
        keep = sorted(matched)[:MAX_MATCHED_ADDRESSES]
        matched = {a: matched[a] for a in keep}
    where2, params2 = ctx.where()
    params2["ips"] = sorted({sql.ip_param(a) for a in matched})
    q2 = f"""
        SELECT {det._disp('e')}, {det._disp('ti')}, {det._disp('peer')}, outbound, count(), sum(bytes), sum(packets),
               min(timestamp), max(timestamp), groupUniqArray(8)(port), groupUniqArray(288)(bucket)
        FROM (
            SELECT {sql.EXPORTER} AS e, {sql.SRC} IN %(ips)s AS src_hit,
                   if(src_hit, {sql.SRC}, {sql.DST}) AS ti, if(src_hit, {sql.DST}, {sql.SRC}) AS peer,
                   NOT src_hit AS outbound, bytes, packets, timestamp,
                   if(src_hit, src_port, dst_port) AS port, {det.BUCKET_SQL} AS bucket
            FROM {det._raw()} WHERE {where2} AND ({sql.SRC} IN %(ips)s OR {sql.DST} IN %(ips)s)
        )
        GROUP BY e, ti, peer, outbound
    """
    res2 = await common.run_query(q2, params2, budget_s=ctx.budget_s, label="detect-ti-detail")
    per: dict[tuple[str, str], dict] = {}
    for e, ti, peer, outbound, recs, vol, pk, t_min, t_max, ports, buckets in res2.result_rows:
        agg = per.setdefault((e, ti), {"peers": {}, "outbound": False, "records": 0, "bytes": 0, "packets": 0,
                                       "first": None, "last": None, "ports": set(), "buckets": set(),
                                       "out_bytes": 0})
        p = agg["peers"].setdefault(peer, {"ip": peer, "bytes": 0, "records": 0, "outbound": False, "out_bytes": 0})
        p["bytes"] += int(vol)
        p["records"] += int(recs)
        p["outbound"] = p["outbound"] or bool(outbound)
        if outbound:
            p["out_bytes"] += int(vol)
        agg["outbound"] = agg["outbound"] or bool(outbound)
        if outbound:
            agg["out_bytes"] += int(vol)
        agg["records"] += int(recs)
        agg["bytes"] += int(vol)
        agg["packets"] += int(pk)
        agg["ports"].update(int(x) for x in ports)
        agg["buckets"].update(det._aware(b) for b in buckets)
    cands = []
    for (exp, ti), agg in per.items():
        if not agg["outbound"] and not cfg["include_inbound"]:
            continue
        feeds = matched.get(ti) or index.lookup(ti)
        categories = sorted({f.category for f in feeds})
        high_risk = bool(set(categories) & HIGH_RISK_CATEGORIES)
        peers = sorted(agg["peers"].values(), key=lambda p: -p["bytes"])
        top = peers[0]["ip"] if peers else None
        buckets = sorted(agg["buckets"])
        first, last, latest = det._span(ctx, buckets[0], buckets[-1])
        listed = f"listed by {', '.join(f.name for f in feeds[:3])} ({', '.join(categories)})"
        if agg["outbound"]:
            sev = cfg["severity_outbound"]
            if not high_risk and sev == "critical":
                sev = "warning"
            # Attribute bytes to the hosts that sent them: the address's total
            # is not one host's traffic when several internal hosts talk to it.
            out_peers = sorted((p for p in peers if p["outbound"]), key=lambda p: -p["out_bytes"])
            src_host = out_peers[0]["ip"] if out_peers else top
            received = agg["bytes"] - agg["out_bytes"]
            if len(out_peers) <= 1:
                desc = f"{src_host} sent {det._fmt_bytes(agg['out_bytes'])} to {ti}"
            else:
                desc = (f"{len(out_peers)} internal hosts sent {det._fmt_bytes(agg['out_bytes'])} to {ti} "
                        f"(most from {src_host}: {det._fmt_bytes(out_peers[0]['out_bytes'])})")
            if received > 0:
                desc += f" and received {det._fmt_bytes(received)} from it"
            desc += f"; {listed}"
            src, dst, host = src_host, ti, src_host
        else:
            sev = cfg["severity_inbound"]
            desc = f"{ti} sent {det._fmt_bytes(agg['bytes'])} to "
            desc += top if len(peers) <= 1 else f"{len(peers)} internal hosts (most to {top})"
            desc += f" and no traffic back was seen; {listed}"
            src, dst, host = ti, top, top
        cands.append(det.Candidate(
            kind="threat_intel", exporter=exp, subject=(ti,), severity=sev,
            confidence="high" if agg["outbound"] and high_risk else "medium",
            title="Traffic with a threat-intel listed address",
            description=desc, first_seen=first, last_seen=last, latest=latest, occurrences=len(buckets),
            method=det.DETECTOR_BY_KIND["threat_intel"].method,
            threshold={"feeds": len(index.feeds), "include_inbound": cfg["include_inbound"]},
            observed={"direction": "outbound" if agg["outbound"] else "inbound", "records": agg["records"],
                      "bytes": agg["bytes"], "sent_bytes": agg["out_bytes"], "ports": sorted(agg["ports"])[:16],
                      "internal_hosts": len(peers)},
            evidence_filter={"exporter": exp, "talker": ti},
            segments=det._segments_from_buckets(buckets, ctx),
            src=src, dst=dst, host=host, metric=agg["bytes"], metric_label="bytes",
            flows=agg["records"], bytes=agg["bytes"], packets=agg["packets"],
            extra={"ti_address": ti, "feeds": [f.as_dict() for f in feeds], "categories": categories,
                   "internal_hosts": [{"ip": p["ip"], "bytes": p["bytes"], "sent_bytes": p["out_bytes"],
                                       "outbound": p["outbound"]} for p in peers[:10]]},
        ))
    cands.sort(key=lambda c: (det.SEVERITY_RANK.get(c.severity, 0), c.bytes or 0), reverse=True)
    out.match_count = len(cands)
    out.truncated = out.truncated or len(cands) > ctx.limit
    out.candidates = cands[: ctx.limit]


# ── Refresh loop ─────────────────────────────────────────────────────────────

async def refresh_due(now: datetime | None = None) -> dict:
    """Refresh enabled URL feeds whose interval elapsed (one worker at a time)."""
    from app.core.database import AsyncSessionLocal, engine as pg_engine

    now = now or datetime.now(timezone.utc)
    summary = {"refreshed": 0, "failed": 0}
    async with pg_engine.connect() as lock_conn:
        got = (await lock_conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ADVISORY_LOCK})).scalar()
        await lock_conn.commit()
        if not got:
            return {"skipped": "locked"}
        try:
            async with AsyncSessionLocal() as db:
                due = (await db.execute(text(DUE_FEEDS_SQL), {"now": now})).all()
                for (feed_id,) in due:
                    result = await refresh_feed(db, feed_id)
                    await db.commit()
                    summary["refreshed" if result.get("ok") else "failed"] += 1
        finally:
            await lock_conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": ADVISORY_LOCK})
            await lock_conn.commit()
    if summary["refreshed"] or summary["failed"]:
        logger.info("netflow TI refresh: %s", summary)
    return summary


# URL feeds due for a refresh. ``:now`` is cast explicitly: an untyped
# parameter in ``:now - make_interval(...)`` is inferred as an interval
# (interval - interval), and the comparison with a timestamptz then fails
# on every loop iteration, so no feed ever refreshed itself.
DUE_FEEDS_SQL = (
    "SELECT id FROM netflow_ti_feeds WHERE enabled AND source = 'url' AND url IS NOT NULL "
    "AND (last_refreshed_at IS NULL "
    "     OR last_refreshed_at < CAST(:now AS timestamptz) - make_interval(hours => refresh_hours)) "
    "AND (last_status IS NULL OR last_status NOT LIKE 'Download failed%' "
    "     OR updated_at < CAST(:now AS timestamptz) - interval '1 hour') "
    "ORDER BY last_refreshed_at NULLS FIRST"
)


async def netflow_ti_loop() -> None:
    """Background loop (contract §9.4): daily (per-feed interval) feed refresh."""
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            await refresh_due()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("netflow TI: refresh failed")
        await asyncio.sleep(LOOP_INTERVAL_S)


def new_feed_id() -> uuid.UUID:
    return uuid.uuid4()


__all__ = [
    "CATALOG", "CATEGORIES", "parse_feed", "ParseStats", "replace_entries", "refresh_feed", "fetch_url",
    "TiIndex", "FeedInfo", "get_index", "invalidate_index", "detect", "refresh_due", "netflow_ti_loop",
    "feed_item", "FEED_COLUMNS", "MAX_UPLOAD_CHARS",
]
