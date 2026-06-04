#!/usr/bin/env python3
"""Provision DB-IP Lite GeoIP databases for ZenPlus NetFlow enrichment (Phase 2b).

Downloads the free, CC-BY-4.0 DB-IP Lite *country* and *ASN* .mmdb databases into
NETFLOW_GEOIP_DIR (default /opt/zenplus/data/geoip), where the API's GeoIP service
picks them up automatically (mtime reload).

Best-effort by design: it ALWAYS exits 0 so it is safe to run from the OTA
updater (run_hook). It skips the download when the current month's database is
already present. Run it monthly (cron) to stay fresh.

Attribution: IP geolocation by DB-IP (https://db-ip.com), licensed CC-BY-4.0.
Standard library only — no third-party dependency.
"""
import datetime
import gzip
import os
import sys
import urllib.request

GEOIP_DIR = os.getenv("NETFLOW_GEOIP_DIR", "/opt/zenplus/data/geoip")
BASE = "https://download.db-ip.com/free"
DATASETS = [("dbip-country-lite", "country.mmdb"), ("dbip-asn-lite", "asn.mmdb")]


def recent_months(n: int = 3) -> list[str]:
    d = datetime.date.today()
    out = []
    for i in range(n):
        m, y = d.month - i, d.year
        while m <= 0:
            m += 12
            y -= 1
        out.append(f"{y:04d}-{m:02d}")
    return out


def is_current(dest: str, stamp: str) -> bool:
    sp = dest + ".stamp"
    try:
        return os.path.exists(dest) and open(sp).read().strip() == stamp
    except OSError:
        return False


def fetch(prefix: str, dest: str) -> bool:
    for ym in recent_months():
        if is_current(dest, ym):
            print(f"  {os.path.basename(dest)}: already current ({ym})")
            return True
        url = f"{BASE}/{prefix}-{ym}.mmdb.gz"
        try:
            print(f"  fetching {url}")
            req = urllib.request.Request(url, headers={"User-Agent": "zenplus-geoip/1"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = gzip.decompress(resp.read())
            tmp = dest + ".tmp"
            with open(tmp, "wb") as f:
                f.write(raw)
            os.replace(tmp, dest)
            with open(dest + ".stamp", "w") as f:
                f.write(ym)
            print(f"  wrote {os.path.basename(dest)} ({len(raw) // 1024} KiB, {ym})")
            return True
        except Exception as e:  # noqa: BLE001 — best-effort
            print(f"  {ym} unavailable: {e}")
    return False


def main() -> None:
    try:
        os.makedirs(GEOIP_DIR, exist_ok=True)
    except OSError as e:
        print(f"geoip: cannot create {GEOIP_DIR}: {e}")
        return
    ok = True
    for prefix, name in DATASETS:
        if not fetch(prefix, os.path.join(GEOIP_DIR, name)):
            ok = False
            print(f"geoip: could not provision {name} (continuing)")
    print("geoip provisioning complete" if ok else "geoip provisioning incomplete (best-effort)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"geoip: unexpected error: {e}")
    sys.exit(0)  # ALWAYS succeed — never break an OTA update
