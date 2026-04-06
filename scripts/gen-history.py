#!/usr/bin/env python3
"""
ZenPlus Historical Data Generator
==================================
Generates realistic historical ping metrics and status logs for a device
and inserts them into ClickHouse (ping_metrics, ping_metrics_5m, ping_metrics_1h,
device_status_log).

Usage:
    sudo -u zenplus /opt/zenplus/venv/bin/python /opt/zenplus/scripts/gen-history.py \
        --ip 192.168.1.1 \
        --start "2025-04-01" \
        --end "2026-04-05" \
        --uptime 99.5

    # Multiple devices at once:
    sudo -u zenplus /opt/zenplus/venv/bin/python /opt/zenplus/scripts/gen-history.py \
        --ip 192.168.1.1,192.168.1.2,10.0.0.1 \
        --start "2025-04-01" \
        --end "2026-04-05" \
        --uptime 99.5

Options:
    --ip        Device IP(s), comma-separated
    --start     Start datetime (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)
    --end       End datetime   (YYYY-MM-DD or YYYY-MM-DD HH:MM:SS)
    --uptime    Target uptime percentage (0-100), e.g. 99.5
    --rtt-base  Base RTT in ms (default: auto from 0.5-50 based on device)
    --rtt-jitter RTT jitter range in ms (default: 20% of base)
    --interval  Ping interval in seconds (default: from device config or 60)
    --dry-run   Show what would be inserted without writing
    --clear     Clear existing data for this device in the time range first
"""

import argparse
import math
import os
import random
import statistics
import sys
from datetime import datetime, timedelta
from ipaddress import ip_address

import psycopg2
import clickhouse_connect

# ── Load .env ────────────────────────────────────────────────────────────────
def load_env(path="/opt/zenplus/.env"):
    env = {}
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

ENV = load_env()

# ── DB helpers ───────────────────────────────────────────────────────────────
def get_pg():
    return psycopg2.connect(
        host=ENV.get("POSTGRES_HOST", "localhost"),
        port=int(ENV.get("POSTGRES_PORT", 5432)),
        dbname=ENV.get("POSTGRES_DB", "zenplus"),
        user=ENV.get("POSTGRES_USER", "zenplus"),
        password=ENV.get("POSTGRES_PASSWORD", ""),
    )

def get_ch():
    return clickhouse_connect.get_client(
        host=ENV.get("CLICKHOUSE_HOST", "localhost"),
        port=int(ENV.get("CLICKHOUSE_HTTP_PORT", 8123)),
        database=ENV.get("CLICKHOUSE_DB", "zenplus"),
        username=ENV.get("CLICKHOUSE_USER", "default"),
        password=ENV.get("CLICKHOUSE_PASSWORD", ""),
    )

def lookup_device(pg, ip_str):
    """Look up device_id, hostname, ping_interval from PostgreSQL."""
    cur = pg.cursor()
    cur.execute(
        "SELECT id, hostname, ping_interval FROM devices WHERE host(ip_address) = %s",
        (ip_str,),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {"id": str(row[0]), "hostname": row[1], "interval": row[2]}


# ── Realistic outage generation ──────────────────────────────────────────────
def generate_outages(start: datetime, end: datetime, uptime_pct: float):
    """
    Generate realistic outage windows.
    Strategy:
    - Calculate total downtime seconds from uptime %
    - Create a mix of outage types:
        * Brief flaps (10s - 2min)  — 40% of incidents
        * Short outages (2-15min)   — 30% of incidents
        * Medium outages (15m-2h)   — 20% of incidents
        * Major outages (2h-12h)    — 10% of incidents
    - Spread them randomly, avoid overlap, more likely during maintenance windows
    """
    total_seconds = (end - start).total_seconds()
    downtime_target = total_seconds * (1 - uptime_pct / 100)

    if downtime_target < 10:
        return []

    outage_profiles = [
        (0.40, 10, 120),        # flaps
        (0.30, 120, 900),       # short
        (0.20, 900, 7200),      # medium
        (0.10, 7200, 43200),    # major
    ]

    outages = []
    remaining = downtime_target

    # Generate outages until we've used up the downtime budget
    attempts = 0
    while remaining > 10 and attempts < 5000:
        attempts += 1
        # Pick outage type weighted
        r = random.random()
        cum = 0
        for weight, min_dur, max_dur in outage_profiles:
            cum += weight
            if r <= cum:
                duration = random.uniform(min_dur, min(max_dur, remaining))
                break
        else:
            duration = random.uniform(10, min(120, remaining))

        duration = min(duration, remaining)
        if duration < 5:
            break

        # Pick a random time, slightly favor nighttime/weekends for realism
        range_secs = total_seconds - duration
        if range_secs <= 0:
            break

        offset = random.uniform(0, range_secs)
        outage_start = start + timedelta(seconds=offset)

        # 30% chance to shift toward maintenance windows (2-6 AM local)
        if random.random() < 0.3:
            # Shift to early morning
            outage_start = outage_start.replace(
                hour=random.randint(2, 5),
                minute=random.randint(0, 59),
            )
            if outage_start < start:
                outage_start = start + timedelta(seconds=offset)

        outage_end = outage_start + timedelta(seconds=duration)
        if outage_end > end:
            outage_end = end
            duration = (outage_end - outage_start).total_seconds()

        # Check overlap with existing outages
        overlap = False
        for (os_, oe_) in outages:
            if outage_start < oe_ and outage_end > os_:
                overlap = True
                break

        if not overlap and duration >= 5:
            outages.append((outage_start, outage_end))
            remaining -= duration

    outages.sort(key=lambda x: x[0])
    return outages


def is_during_outage(ts: datetime, outages) -> bool:
    for os_, oe_ in outages:
        if os_ <= ts < oe_:
            return True
    return False


# ── RTT generation ───────────────────────────────────────────────────────────
def gen_rtt(base: float, jitter: float, hour: int) -> float:
    """Generate realistic RTT with time-of-day variation."""
    # Slightly higher RTT during business hours (8-18)
    if 8 <= hour <= 18:
        load_factor = 1.0 + random.uniform(0, 0.3)
    elif 0 <= hour <= 5:
        load_factor = 0.85 + random.uniform(0, 0.1)
    else:
        load_factor = 0.95 + random.uniform(0, 0.15)

    rtt = base * load_factor + random.gauss(0, jitter)
    # Occasional spikes (2% chance)
    if random.random() < 0.02:
        rtt *= random.uniform(2, 5)
    return max(0.1, round(rtt, 3))


# ── Main data generation ────────────────────────────────────────────────────
def generate_data(device_id, ip_str, start, end, uptime_pct,
                  rtt_base, rtt_jitter, interval):
    print(f"  Generating outage windows for {uptime_pct}% uptime...")
    outages = generate_outages(start, end, uptime_pct)
    total_downtime = sum((oe - os).total_seconds() for os, oe in outages)
    actual_uptime = (1 - total_downtime / (end - start).total_seconds()) * 100
    print(f"  Generated {len(outages)} outage windows, actual uptime: {actual_uptime:.3f}%")

    # ── Raw ping_metrics (last 30 days only, or all if range < 30d) ──
    raw_start = max(start, end - timedelta(days=29))
    print(f"  Generating raw metrics ({raw_start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')})...")

    raw_rows = []
    ts = raw_start
    while ts < end:
        down = is_during_outage(ts, outages)
        if down:
            rtt = 0.0
            loss = 1.0
            jitter = 0.0
            is_up = 0
            sent, recv = 3, 0
        else:
            rtt = gen_rtt(rtt_base, rtt_jitter, ts.hour)
            loss = 0.0 if random.random() > 0.01 else random.uniform(0.1, 0.3)
            jitter = abs(random.gauss(0, rtt_jitter * 0.5))
            is_up = 1
            sent, recv = 3, 3
            if loss > 0:
                recv = max(1, int(sent * (1 - loss)))

        raw_rows.append((
            device_id, ts, is_up, rtt, loss,
            round(jitter, 3), round(max(0.1, rtt - jitter) if rtt > 0 else 0, 3),
            round(rtt + jitter if rtt > 0 else 0, 3),
            sent, recv, "history-gen", ip_str,
        ))
        ts += timedelta(seconds=interval)

    # ── 5-minute rollups (last 90 days) ──
    roll5_start = max(start, end - timedelta(days=89))
    print(f"  Generating 5m rollups ({roll5_start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')})...")

    roll5_rows = []
    ts = roll5_start
    while ts < end:
        bucket_end = ts + timedelta(minutes=5)
        # Simulate samples in this bucket
        samples = []
        t = ts
        while t < bucket_end and t < end:
            down = is_during_outage(t, outages)
            if not down:
                samples.append(gen_rtt(rtt_base, rtt_jitter, t.hour))
            t += timedelta(seconds=interval)

        n = int(300 / interval)  # expected samples per 5min
        up_count = len(samples)
        uptime = up_count / max(n, 1)

        if samples:
            avg_rtt = statistics.mean(samples)
            min_rtt = min(samples)
            max_rtt = max(samples)
            avg_jit = abs(random.gauss(0, rtt_jitter * 0.3))
            avg_loss = 0.0 if random.random() > 0.01 else random.uniform(0.01, 0.05)
        else:
            avg_rtt = min_rtt = max_rtt = 0.0
            avg_jit = 0.0
            avg_loss = 1.0

        roll5_rows.append((
            device_id, ts,
            round(avg_rtt, 3), round(min_rtt, 3), round(max_rtt, 3),
            round(avg_loss, 4), round(avg_jit, 3),
            round(uptime, 4), max(n, 1), ip_str,
        ))
        ts = bucket_end

    # ── 1-hour rollups (full range) ──
    print(f"  Generating 1h rollups ({start.strftime('%Y-%m-%d')} to {end.strftime('%Y-%m-%d')})...")

    roll1h_rows = []
    ts = start
    while ts < end:
        bucket_end = ts + timedelta(hours=1)
        samples = []
        t = ts
        while t < bucket_end and t < end:
            down = is_during_outage(t, outages)
            if not down:
                samples.append(gen_rtt(rtt_base, rtt_jitter, t.hour))
            t += timedelta(seconds=interval)

        n = int(3600 / interval)
        up_count = len(samples)
        uptime = up_count / max(n, 1)

        if samples:
            sorted_s = sorted(samples)
            avg_rtt = statistics.mean(sorted_s)
            min_rtt = sorted_s[0]
            max_rtt = sorted_s[-1]
            p95_idx = int(len(sorted_s) * 0.95)
            p95_rtt = sorted_s[min(p95_idx, len(sorted_s) - 1)]
            avg_jit = abs(random.gauss(0, rtt_jitter * 0.3))
            avg_loss = 0.0 if random.random() > 0.01 else random.uniform(0.01, 0.03)
        else:
            avg_rtt = min_rtt = max_rtt = p95_rtt = 0.0
            avg_jit = 0.0
            avg_loss = 1.0

        roll1h_rows.append((
            device_id, ts,
            round(avg_rtt, 3), round(min_rtt, 3), round(max_rtt, 3),
            round(p95_rtt, 3),
            round(avg_loss, 4), round(avg_jit, 3),
            round(uptime, 4), max(n, 1), ip_str,
        ))
        ts = bucket_end

    # ── Status log entries ──
    print(f"  Generating status change log...")
    status_rows = []
    for os_, oe_ in outages:
        duration = int((oe_ - os_).total_seconds())
        reasons = [
            "Request timeout", "Host unreachable", "Connection refused",
            "Network unreachable", "TTL expired", "No response",
        ]
        status_rows.append((device_id, os_, "up", "down", random.choice(reasons), 0))
        status_rows.append((device_id, oe_, "down", "up", "Recovery", duration))

    return raw_rows, roll5_rows, roll1h_rows, status_rows


def insert_data(ch, raw_rows, roll5_rows, roll1h_rows, status_rows, clear, device_id, start, end):
    if clear:
        print("  Clearing existing data in time range...")
        for tbl in ["ping_metrics", "ping_metrics_5m", "ping_metrics_1h", "device_status_log"]:
            ch.command(
                f"ALTER TABLE zenplus.{tbl} DELETE WHERE device_id = '{device_id}' "
                f"AND timestamp >= '{start.strftime('%Y-%m-%d %H:%M:%S')}' "
                f"AND timestamp <= '{end.strftime('%Y-%m-%d %H:%M:%S')}'"
            )
        import time; time.sleep(2)  # wait for mutations

    batch = 50000

    if raw_rows:
        print(f"  Inserting {len(raw_rows):,} raw ping_metrics rows...")
        cols = ["device_id", "timestamp", "is_up", "rtt_ms", "packet_loss",
                "jitter_ms", "min_rtt_ms", "max_rtt_ms", "packets_sent",
                "packets_recv", "poller_id", "ip_address"]
        for i in range(0, len(raw_rows), batch):
            ch.insert("ping_metrics", raw_rows[i:i+batch], column_names=cols)

    if roll5_rows:
        print(f"  Inserting {len(roll5_rows):,} ping_metrics_5m rows...")
        cols = ["device_id", "timestamp", "avg_rtt_ms", "min_rtt_ms", "max_rtt_ms",
                "avg_packet_loss", "avg_jitter_ms", "uptime_pct", "sample_count", "ip_address"]
        for i in range(0, len(roll5_rows), batch):
            ch.insert("ping_metrics_5m", roll5_rows[i:i+batch], column_names=cols)

    if roll1h_rows:
        print(f"  Inserting {len(roll1h_rows):,} ping_metrics_1h rows...")
        cols = ["device_id", "timestamp", "avg_rtt_ms", "min_rtt_ms", "max_rtt_ms",
                "p95_rtt_ms", "avg_packet_loss", "avg_jitter_ms", "uptime_pct",
                "sample_count", "ip_address"]
        for i in range(0, len(roll1h_rows), batch):
            ch.insert("ping_metrics_1h", roll1h_rows[i:i+batch], column_names=cols)

    if status_rows:
        print(f"  Inserting {len(status_rows):,} device_status_log rows...")
        cols = ["device_id", "timestamp", "old_status", "new_status", "reason", "duration_sec"]
        ch.insert("device_status_log", status_rows, column_names=cols)


# ── CLI ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="Generate realistic historical ping data for ZenPlus devices",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Single device, 1 year, 99.5% uptime:
  sudo -u zenplus /opt/zenplus/venv/bin/python /opt/zenplus/scripts/gen-history.py \\
      --ip 192.168.101.100 --start 2025-04-01 --end 2026-04-05 --uptime 99.5

  # Multiple devices, custom RTT:
  sudo -u zenplus /opt/zenplus/venv/bin/python /opt/zenplus/scripts/gen-history.py \\
      --ip 192.168.1.1,192.168.1.2 --start 2025-01-01 --end 2026-04-05 \\
      --uptime 98.0 --rtt-base 15 --clear

  # All devices in the system:
  sudo -u zenplus /opt/zenplus/venv/bin/python /opt/zenplus/scripts/gen-history.py \\
      --all --start 2025-04-01 --end 2026-04-05 --uptime 99.2
        """,
    )
    parser.add_argument("--ip", help="Device IP(s), comma-separated")
    parser.add_argument("--all", action="store_true", help="Generate for ALL devices")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--uptime", type=float, required=True, help="Target uptime %% (0-100)")
    parser.add_argument("--rtt-base", type=float, default=0, help="Base RTT ms (0=auto)")
    parser.add_argument("--rtt-jitter", type=float, default=0, help="RTT jitter ms (0=auto)")
    parser.add_argument("--interval", type=int, default=0, help="Ping interval sec (0=from device)")
    parser.add_argument("--clear", action="store_true", help="Clear existing data first")
    parser.add_argument("--dry-run", action="store_true", help="Show plan without inserting")

    args = parser.parse_args()

    if not args.ip and not args.all:
        parser.error("Provide --ip or --all")

    start = datetime.strptime(args.start, "%Y-%m-%d")
    end = datetime.strptime(args.end, "%Y-%m-%d")
    if end <= start:
        parser.error("End must be after start")

    uptime = args.uptime
    if not 0 <= uptime <= 100:
        parser.error("Uptime must be 0-100")

    pg = get_pg()
    ch = get_ch()

    # Resolve devices
    if args.all:
        cur = pg.cursor()
        cur.execute("SELECT host(ip_address) FROM devices")
        ips = [row[0] for row in cur.fetchall()]
        cur.close()
        print(f"Found {len(ips)} devices")
    else:
        ips = [ip.strip() for ip in args.ip.split(",")]

    total_days = (end - start).days
    print(f"\n{'='*60}")
    print(f"ZenPlus Historical Data Generator")
    print(f"{'='*60}")
    print(f"  Period:  {start.strftime('%Y-%m-%d')} → {end.strftime('%Y-%m-%d')} ({total_days} days)")
    print(f"  Uptime:  {uptime}%")
    print(f"  Devices: {len(ips)}")
    print(f"  Clear:   {'Yes' if args.clear else 'No'}")
    print(f"{'='*60}\n")

    for ip_str in ips:
        dev = lookup_device(pg, ip_str)
        if not dev:
            print(f"[!] Device {ip_str} not found in PostgreSQL, skipping")
            continue

        device_id = dev["id"]
        interval = args.interval if args.interval > 0 else dev["interval"]

        # Auto RTT based on IP range
        if args.rtt_base > 0:
            rtt_base = args.rtt_base
        else:
            octets = ip_str.split(".")
            if octets[0] in ("10", "172", "192"):
                rtt_base = random.uniform(0.3, 5.0)  # LAN
            else:
                rtt_base = random.uniform(10, 80)  # WAN

        rtt_jitter = args.rtt_jitter if args.rtt_jitter > 0 else rtt_base * 0.2

        print(f"[*] {dev['hostname']} ({ip_str}) — id={device_id}")
        print(f"    RTT base={rtt_base:.1f}ms, jitter={rtt_jitter:.1f}ms, interval={interval}s")

        raw, r5, r1h, slog = generate_data(
            device_id, ip_str, start, end, uptime, rtt_base, rtt_jitter, interval,
        )

        print(f"    Raw: {len(raw):,} | 5m: {len(r5):,} | 1h: {len(r1h):,} | Status: {len(slog):,}")

        if args.dry_run:
            print(f"    [DRY RUN] Skipping insert")
        else:
            insert_data(ch, raw, r5, r1h, slog, args.clear, device_id, start, end)
            print(f"    [OK] Done")

        print()

    pg.close()
    print("All done!")


if __name__ == "__main__":
    main()
