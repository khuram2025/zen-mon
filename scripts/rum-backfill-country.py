#!/opt/zenplus/venv/bin/python
"""Backfill visitor countries on stored RUM events from the on-box GeoIP database.

Intake resolves the country of every new event (CDN header first, then the
DB-IP database under data/geoip). This script applies the same lookup to
events that were stored before GeoIP resolution existed, or before the
database was provisioned.

Only the raw event table is rewritten. The 5-minute rollup keys on country,
so its historical rows keep the blank country; the overview and trends on the
30/90-day ranges therefore show resolved countries only for traffic after the
database was installed, while every explorer (raw events) is fully backfilled.

Usage (run as any user that can read /opt/zenplus/.env):
    scripts/rum-backfill-country.py [--days 14] [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))


def load_env() -> None:
    path = os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=14, help="how far back to backfill (raw retention is 14 days)")
    parser.add_argument("--dry-run", action="store_true", help="resolve and report, but do not write")
    args = parser.parse_args()

    load_env()
    from app.core.database import get_ch_client
    from app.services import geoip

    if not geoip.available():
        print(f"GeoIP database not found under {geoip.GEOIP_DIR}; run scripts/fetch-geoip.py first.", file=sys.stderr)
        return 2

    client = get_ch_client()
    rows = client.query(
        """
        SELECT client_ip, count()
        FROM zenplus.apm_rum_events
        WHERE timestamp >= now() - INTERVAL {days:UInt32} DAY
          AND country = '' AND client_ip != ''
        GROUP BY client_ip
        """,
        parameters={"days": args.days},
    ).result_rows

    by_country: dict[str, list[str]] = defaultdict(list)
    unresolved = 0
    for ip, count in rows:
        iso, _ = geoip.country_of(ip)
        if iso and len(iso) == 2 and iso.isalpha():
            by_country[iso.upper()].append(ip)
        else:
            unresolved += int(count)

    resolved_events = 0
    for country, ips in sorted(by_country.items()):
        print(f"{country}: {len(ips)} client address(es)")
        if args.dry_run:
            continue
        # ALTER ... UPDATE is a background mutation in ClickHouse; each country
        # is one statement so a large table never needs a giant IN list.
        for start in range(0, len(ips), 500):
            chunk = ips[start:start + 500]
            client.command(
                """
                ALTER TABLE zenplus.apm_rum_events
                UPDATE country = {country:String}
                WHERE timestamp >= now() - INTERVAL {days:UInt32} DAY
                  AND country = '' AND client_ip IN {ips:Array(String)}
                """,
                parameters={"country": country, "days": args.days, "ips": chunk},
            )
        resolved_events += 1
    print(
        f"{len(rows)} blank-country address(es) examined · {sum(len(v) for v in by_country.values())} resolved"
        f" · {unresolved} event(s) from private/reserved addresses left blank"
        + (" · dry run, nothing written" if args.dry_run else " · mutations submitted (ClickHouse applies them in the background)")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
