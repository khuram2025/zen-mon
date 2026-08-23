#!/usr/bin/env python3
"""Seed / refresh the udt_oui table (MAC prefix -> vendor) for UDT.

Downloads the IEEE OUI registry (MA-L CSV, ~37k assignments) and upserts it
into Postgres so User Device Tracker endpoints resolve their NIC vendor.
Existing endpoint rows with a NULL vendor are backfilled afterwards.

Best-effort by design: ALWAYS exits 0 so it is safe from the OTA updater.
Standard library only — connects with psql via the local socket as the
invoking user (run as postgres, or set PGHOST/PGUSER/PGPASSWORD for the
zenplus role).

Usage:  python3 scripts/fetch-oui.py [--db zenplus]
Source: https://standards-oui.ieee.org/oui/oui.csv (fallback: Wireshark manuf)
"""
import csv
import io
import os
import subprocess
import sys
import tempfile
import urllib.request

DB = "zenplus"
IEEE_URL = "https://standards-oui.ieee.org/oui/oui.csv"
WIRESHARK_URL = "https://www.wireshark.org/download/automated/data/manuf"


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "zenplus-udt/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def parse_ieee_csv(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    reader = csv.reader(io.StringIO(raw.decode("utf-8", "replace")))
    header = next(reader, None)
    if not header or "Assignment" not in header:
        return {}
    ai = header.index("Assignment")
    oi = header.index("Organization Name")
    for row in reader:
        if len(row) <= max(ai, oi):
            continue
        prefix = row[ai].strip().lower()
        org = row[oi].strip()
        if len(prefix) == 6 and org:
            out[prefix] = org
    return out


def parse_wireshark_manuf(raw: bytes) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        mac = parts[0].strip().lower()
        if "/" in mac:  # skip longer-than-24-bit blocks
            continue
        prefix = mac.replace(":", "").replace("-", "")
        if len(prefix) != 6:
            continue
        vendor = (parts[2] if len(parts) > 2 else parts[1]).strip()
        if vendor:
            out[prefix] = vendor
    return out


def main() -> int:
    db = DB
    if "--db" in sys.argv:
        db = sys.argv[sys.argv.index("--db") + 1]

    ouis: dict[str, str] = {}
    for url, parser in ((IEEE_URL, parse_ieee_csv), (WIRESHARK_URL, parse_wireshark_manuf)):
        try:
            print(f"downloading {url} ...")
            ouis = parser(fetch(url))
            if len(ouis) > 1000:
                break
            print(f"  parsed only {len(ouis)} rows — trying next source")
        except Exception as exc:  # noqa: BLE001
            print(f"  failed: {exc}")
    if len(ouis) < 100:
        print("no usable OUI data downloaded; leaving udt_oui unchanged")
        return 0

    print(f"upserting {len(ouis)} OUI assignments ...")
    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as tmp:
        w = csv.writer(tmp)
        for prefix, vendor in sorted(ouis.items()):
            w.writerow([prefix, vendor[:255]])
        tmp_path = tmp.name
    os.chmod(tmp_path, 0o644)

    sql = f"""
CREATE TEMP TABLE oui_stage (prefix VARCHAR(6), vendor VARCHAR(255));
\\copy oui_stage FROM '{tmp_path}' WITH (FORMAT csv)
INSERT INTO udt_oui (prefix, vendor)
SELECT DISTINCT ON (prefix) prefix, vendor FROM oui_stage
ON CONFLICT (prefix) DO UPDATE SET vendor = EXCLUDED.vendor;
UPDATE udt_endpoints e SET vendor = o.vendor, updated_at = NOW()
FROM udt_oui o
WHERE e.vendor IS NULL
  AND o.prefix = replace(substring(e.mac::text, 1, 8), ':', '');
"""
    try:
        res = subprocess.run(
            ["psql", "-d", db, "-v", "ON_ERROR_STOP=1"],
            input=sql, capture_output=True, text=True, timeout=300,
        )
        print(res.stdout.strip())
        if res.returncode != 0:
            print(f"psql failed: {res.stderr.strip()}")
    except Exception as exc:  # noqa: BLE001
        print(f"psql failed: {exc}")
    finally:
        os.unlink(tmp_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
