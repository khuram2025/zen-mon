#!/usr/bin/env python3
"""NetFlow v5 / v9 / IPFIX and sFlow v5 traffic simulator for collector and UI testing.

Generates a deterministic, realistic traffic mix (bidirectional HTTPS, DNS,
SSH, SMB, ESP tunnels, periodic ICMP monitoring, a diurnal curve) and exports
it over UDP exactly the way a router, firewall or switch would, so the real
collector, rollups, API and dashboard can be exercised end to end on a lab box.

Scenarios cover the cases the NetFlow development review asks staging to
prove (flag presence, IPv6, 32-bit ifIndex, sampling, sequence loss, scans):

  mixed        normal enterprise mix (default)
  no-flags     v9/IPFIX template WITHOUT the TCP_FLAGS field
  ipv6         IPv6 conversations (fields 27/28) alongside IPv4
  big-ifindex  4-byte interface fields with indexes above 65,535
  scan         a host sending SYN-only probes to hundreds of destinations
  icmp         a sustained high-rate ICMP burst from one host; on v9/IPFIX the
               ICMP flows use their own template carrying icmpTypeCodeIPv4
               (IE 32) instead of ports, which the collector normalises into
               dst_port (type*256+code)
  v5-sampled   NetFlow v5 with a sampled header: the 2 mode bits set over the
               14-bit interval (1-in-100 deterministic = 0x4064 unless
               --sampling / --sampling-mode say otherwise)
  all          mixed + scan + icmp + ipv6 (+ big-ifindex on v9/IPFIX)
  nsel         a Cisco ASA-style NetFlow v9 firewall: one record per session
               event (flow-create, flow-update, flow-teardown with initiator
               and responder byte/packet counters IE 231/232/298/299, flow-denied
               for ACL-blocked probes and refused connections), post-NAT
               addresses and ports (IE 225-228, PAT to 198.51.100.7 for
               internet-bound sessions), event time IE 323 and connection id
  appid        NBAR2-style application ids: IE 95 in every flow record, plus
               the application table (IE 95 + 96 name + 94 description) and
               attributes (IE 95 + 372 category) option templates, sent as two
               separate options records like Cisco does (v9 or IPFIX)

--protocol sflow exports the same traffic as an sFlow v5 agent (a switch):
flow samples of individual packets chosen 1-in-N (--sampling, default 512)
with Ethernet/802.1Q/IPv4/IPv6/TCP/UDP/ICMP headers, extended_router and
extended_gateway records (next hop, masks, AS path), and a generic interface
counter sample per interface per tick (flow_if_counters). With big-ifindex
the expanded sample formats are used. sFlow has no timestamps, so it cannot
backfill history or simulate a skewed clock.

Clock checks: --clock-offset N shifts the exporter's header and flow times by
N seconds. The collector reports the offset per exporter (clock_offset_ms in
flow_collector_stats and /health) and, with its default clamp policy, places
flows more than 2 minutes ahead or 2 hours behind its own clock at the receipt
time (fields_present bit 28). --history-hours backfills the past the same way
an exporter with a wrong clock would, so the collector keeps those timestamps
only when it runs with NETFLOW_CLOCK_POLICY=trust.

Examples:
  # 24 hours of backfilled one-minute history, then live traffic forever
  scripts/netflow-simulator.py --collector 127.0.0.1:2055 --exporter 127.0.0.61 \
      --version 9 --history-hours 24 --live

  # an IPFIX exporter without TCP flags, sampled 1-in-100, dropping every 50th packet
  scripts/netflow-simulator.py --version 10 --scenario no-flags --sampling 100 \
      --drop-every 50 --exporter 127.0.0.62 --live --live-seconds 120

  # an IPv6 exporter (the collector must key its templates by the full address)
  scripts/netflow-simulator.py --collector [::1]:2055 --exporter ::1 --version 10 \
      --scenario ipv6 --live --live-seconds 60

  # a sampled v5 router whose clock runs 3 minutes fast
  scripts/netflow-simulator.py --scenario v5-sampled --clock-offset 180 --live

  # an ASA firewall exporting NSEL session events with NAT
  scripts/netflow-simulator.py --scenario nsel --exporter 127.0.0.65 --history-hours 1 --live

  # a Cisco router with NBAR2 application ids (IPFIX)
  scripts/netflow-simulator.py --version 10 --scenario appid --exporter 127.0.0.66 --live

  # a switch exporting sFlow at 1-in-512 to the collector's sFlow port
  scripts/netflow-simulator.py --protocol sflow --collector 127.0.0.1:6343 \
      --exporter 127.0.0.67 --scenario all --live

Timestamps are carried in the export header / absolute flow-time fields, so
history is placed in the past without touching the collector's clock.
"""

from __future__ import annotations

import argparse
import ipaddress
import math
import random
import socket
import struct
import sys
import time
from dataclasses import dataclass, field

MAX_DATAGRAM = 1400
SYS_BOOT_OFFSET_MS = 3_600_000  # pretend the exporter booted an hour before the first export

# TCP flag bits
FIN, SYN, RST, PSH, ACK, URG = 0x01, 0x02, 0x04, 0x08, 0x10, 0x20


@dataclass
class Flow:
    start_ms: int
    end_ms: int
    src: str
    dst: str
    sport: int
    dport: int
    proto: int
    packets: int
    octets: int
    flags: int = 0
    tos: int = 0
    in_if: int = 1
    out_if: int = 2
    src_mask: int = 24
    dst_mask: int = 24
    src_as: int = 0
    dst_as: int = 0
    next_hop: str = "0.0.0.0"
    # The two halves of one conversation: the initiator's flow points at the
    # responder's (session exporters such as NSEL report them as one record).
    reply: "Flow | None" = field(default=None, repr=False)
    is_reply: bool = False

    @property
    def v6(self) -> bool:
        return ":" in self.src or ":" in self.dst


def _link(req: Flow, resp: Flow) -> list[Flow]:
    req.reply, resp.is_reply = resp, True
    return [req, resp]


# Origin AS of the well-known external servers of the model (the AS numbers
# show up in the AS fields of every export format and in sFlow gateway data).
SERVER_AS = {
    "142.250.74.46": 15169, "13.107.42.14": 8068, "151.101.1.140": 54113,
    "104.16.132.229": 13335, "203.0.113.10": 64500, "2001:db8:ffff::443": 64501,
}


# ── Traffic model ─────────────────────────────────────────────────────────────

@dataclass
class Model:
    rng: random.Random
    scenario: str
    big_ifindex: bool
    lan_if: int = 1
    wan_if: int = 2
    clients: list[str] = field(default_factory=list)
    clients6: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.clients = [f"10.20.{net}.{host}" for net in (1, 2, 3, 4) for host in range(10, 40)]
        self.clients6 = [f"2001:db8:20::{i:x}" for i in range(0x10, 0x30)]
        if self.big_ifindex:
            self.lan_if, self.wan_if = 70000, 70001

    def diurnal(self, ts: float) -> float:
        """Traffic multiplier: business-hours peak, quiet nights (UTC based)."""
        hour = time.gmtime(ts).tm_hour + time.gmtime(ts).tm_min / 60.0
        return 0.25 + 0.75 * max(0.0, math.sin((hour - 5) / 15.0 * math.pi)) if 5 <= hour <= 20 else 0.25

    def _pair(self, t_ms: int, step_ms: int, client: str, server: str, dport: int, proto: int,
              req_bytes: int, resp_bytes: int, *, flags_req: int, flags_resp: int, tos: int = 0,
              outbound: bool = True) -> list[Flow]:
        start = t_ms + self.rng.randint(0, max(1, step_ms - 2000))
        dur = self.rng.randint(200, min(step_ms, 55_000))
        sport = self.rng.randint(49152, 65535)
        req_pkts = max(1, req_bytes // 600)
        resp_pkts = max(1, resp_bytes // 1300)
        lan, wan = (self.lan_if, self.wan_if) if outbound else (self.lan_if, self.lan_if + 2)
        server_as = SERVER_AS.get(server, 0)
        return _link(
            Flow(start, start + dur, client, server, sport, dport, proto, req_pkts, req_bytes,
                 flags_req, tos, in_if=lan, out_if=wan, dst_as=server_as),
            Flow(start + 5, start + dur, server, client, dport, sport, proto, resp_pkts, resp_bytes,
                 flags_resp, tos, in_if=wan, out_if=lan, src_as=server_as),
        )

    def tick(self, t_ms: int, step_ms: int) -> list[Flow]:
        rng = self.rng
        mult = self.diurnal(t_ms / 1000)
        scale = step_ms / 60_000.0
        flows: list[Flow] = []
        sc = self.scenario

        def n(base: float) -> int:
            return max(0, int(rng.gauss(base * mult * scale, base * 0.1 * scale)))

        web_servers = ["142.250.74.46", "13.107.42.14", "151.101.1.140", "104.16.132.229"]
        for _ in range(n(40)):  # HTTPS browsing (request + response halves)
            flows += self._pair(t_ms, step_ms, rng.choice(self.clients), rng.choice(web_servers), 443, 6,
                                rng.randint(1_500, 40_000), rng.randint(20_000, 2_500_000),
                                flags_req=SYN | ACK | PSH | FIN, flags_resp=SYN | ACK | PSH | FIN)
        for _ in range(n(25)):  # DNS
            flows += self._pair(t_ms, step_ms, rng.choice(self.clients), "10.30.0.53", 53, 17,
                                rng.randint(60, 120), rng.randint(90, 500), flags_req=0, flags_resp=0,
                                outbound=False)
        for _ in range(n(4)):  # SSH to data-centre hosts
            flows += self._pair(t_ms, step_ms, rng.choice(self.clients[:20]), f"10.30.0.{rng.randint(10, 30)}", 22, 6,
                                rng.randint(5_000, 80_000), rng.randint(10_000, 400_000),
                                flags_req=SYN | ACK | PSH | FIN, flags_resp=SYN | ACK | PSH | FIN, tos=0x40,
                                outbound=False)
        for _ in range(n(6)):  # SMB file shares
            flows += self._pair(t_ms, step_ms, rng.choice(self.clients), "10.30.0.40", 445, 6,
                                rng.randint(20_000, 400_000), rng.randint(200_000, 12_000_000),
                                flags_req=SYN | ACK | PSH | FIN, flags_resp=SYN | ACK | PSH | FIN,
                                outbound=False)
        if rng.random() < 0.15 * scale:  # occasional refused connections
            c = rng.choice(self.clients)
            flows += self._pair(t_ms, step_ms, c, "10.30.0.41", 8443, 6, 60, 40,
                                flags_req=SYN, flags_resp=RST | ACK, outbound=False)
        # Site-to-site IPsec tunnel: ESP both ways, a large steady share
        esp_bytes = int(rng.gauss(180_000_000, 20_000_000) * mult * scale)
        start = t_ms + 10
        flows += _link(
            Flow(start, start + step_ms - 20, "198.51.100.1", "203.0.113.10", 0, 0, 50,
                 packets=max(1, esp_bytes // 1200), octets=max(1, esp_bytes), tos=0x88,
                 in_if=self.lan_if, out_if=self.wan_if, dst_as=SERVER_AS["203.0.113.10"]),
            Flow(start, start + step_ms - 20, "203.0.113.10", "198.51.100.1", 0, 0, 50,
                 packets=max(1, esp_bytes // 1100), octets=max(1, int(esp_bytes * 0.8)), tos=0x88,
                 in_if=self.wan_if, out_if=self.lan_if, src_as=SERVER_AS["203.0.113.10"]))
        # Voice (EF) RTP stream
        for _ in range(n(2)):
            flows += self._pair(t_ms, step_ms, rng.choice(self.clients), "10.30.0.70", 16384, 17,
                                rng.randint(300_000, 900_000), rng.randint(300_000, 900_000),
                                flags_req=0, flags_resp=0, tos=0xB8, outbound=False)
        # Monitoring host pings a handful of targets every minute (benign periodic ICMP)
        for i in range(max(1, int(12 * scale))):
            target = f"10.30.0.{100 + i % 12}"
            s = t_ms + i * 1000
            # ICMP type/code travel in dst_port the way routers export them (8/0 request, 0/0 reply)
            flows += _link(Flow(s, s + 10, "10.20.1.5", target, 0, 0x0800, 1, packets=1, octets=84,
                                in_if=self.lan_if, out_if=self.lan_if + 2),
                           Flow(s + 1, s + 11, target, "10.20.1.5", 0, 0x0000, 1, packets=1, octets=84,
                                in_if=self.lan_if + 2, out_if=self.lan_if))
        if sc in ("scan", "all") and rng.random() < 0.5:
            scanner = "10.20.3.66"
            for d in range(rng.randint(80, 160)):
                dst = f"10.30.{1 + d // 250}.{d % 250 + 1}"
                s = t_ms + d * 20
                flows.append(Flow(s, s + 1, scanner, dst, rng.randint(40000, 60000), rng.choice([22, 80, 443, 445, 3389]),
                                  6, packets=1, octets=44, flags=SYN, in_if=self.lan_if, out_if=self.lan_if + 2))
        if sc in ("icmp", "all"):
            # A 5-minute sustained ICMP burst at the top of every third hour
            minute = time.gmtime(t_ms / 1000).tm_min
            hour = time.gmtime(t_ms / 1000).tm_hour
            if hour % 3 == 0 and minute < 5:
                for i in range(int(400 * scale)):
                    s = t_ms + i * 100
                    flows.append(Flow(s, s + 90, "10.20.4.77", f"10.30.9.{i % 200 + 1}", 0, 0x0800, 1,
                                      packets=900, octets=900 * 1000, in_if=self.lan_if, out_if=self.lan_if + 2))
        if sc in ("ipv6", "all"):
            for _ in range(n(10)):
                flows += self._pair(t_ms, step_ms, rng.choice(self.clients6), "2001:db8:ffff::443", 443, 6,
                                    rng.randint(2_000, 30_000), rng.randint(30_000, 1_500_000),
                                    flags_req=SYN | ACK | PSH | FIN, flags_resp=SYN | ACK | PSH | FIN)
        return flows


# ── Encoders ──────────────────────────────────────────────────────────────────

V9_FIELDS_V4 = [  # (type, length)
    (1, 8), (2, 8), (4, 1), (5, 1), (6, 1), (7, 2), (8, 4), (9, 1), (10, 2),
    (11, 2), (12, 4), (13, 1), (14, 2), (15, 4), (16, 4), (17, 4), (21, 4), (22, 4),
]
V9_FIELDS_V6 = [
    (1, 8), (2, 8), (4, 1), (5, 1), (6, 1), (7, 2), (27, 16), (29, 1), (10, 2),
    (11, 2), (28, 16), (30, 1), (14, 2), (62, 16), (21, 4), (22, 4),
]
IPFIX_TIME_FIELDS = [(152, 8), (153, 8)]
# ICMP template: no transport ports, type/code in icmpTypeCodeIPv4 (IE 32).
V9_FIELDS_ICMP = [
    (1, 8), (2, 8), (4, 1), (5, 1), (8, 4), (9, 1), (10, 2), (12, 4), (13, 1), (14, 2),
    (32, 2), (21, 4), (22, 4),
]


# NBAR2-style application ids for --scenario appid: (engine, selector) ->
# (name, description, category). Illustrative values in the NBAR2 id space
# (engine 13 = NBAR2, 3 = IANA-L4 port, 1 = IANA-L3 protocol), not a Cisco
# protocol pack.
APP_TABLE = {
    (13, 453): ("ssl", "Secure Socket Layer", "browsing"),
    (13, 1411): ("youtube", "YouTube video streaming", "consumer-streaming"),
    (13, 1476): ("ms-office-365", "Microsoft Office 365", "business-and-productivity-tools"),
    (13, 1206): ("fastly", "Fastly content delivery", "browsing"),
    (13, 1300): ("cloudflare", "Cloudflare content delivery", "browsing"),
    (13, 61): ("rtp", "Real-time Transport Protocol", "voice-and-video"),
    (3, 53): ("dns", "Domain Name System", "net-admin"),
    (3, 22): ("ssh", "Secure Shell", "net-admin"),
    (3, 445): ("cifs", "Common Internet File System", "file-sharing"),
    (1, 50): ("ipsec", "IP Encapsulating Security Payload", "layer3-over-ip"),
    (1, 1): ("icmp", "Internet Control Message Protocol", "net-admin"),
}
SERVER_APPS = {"142.250.74.46": (13, 1411), "13.107.42.14": (13, 1476),
               "151.101.1.140": (13, 1206), "104.16.132.229": (13, 1300)}
PORT_APPS = {443: (13, 453), 53: (3, 53), 22: (3, 22), 445: (3, 445), 16384: (13, 61)}


def app_of(fl: Flow) -> tuple[int, int]:
    """The (engine, selector) an NBAR2 exporter would report; (0, 0) = none."""
    if fl.proto == 50:
        return (1, 50)
    if fl.proto == 1:
        return (1, 1)
    for addr in (fl.src, fl.dst):
        if addr in SERVER_APPS and 443 in (fl.sport, fl.dport):
            return SERVER_APPS[addr]
    for port in (fl.dport, fl.sport):
        if port in PORT_APPS:
            return PORT_APPS[port]
    return (0, 0)


def _app_id_bytes(engine: int, selector: int) -> bytes:
    # applicationId: one classification-engine byte + a 3-byte selector (RFC 6759)
    return bytes([engine]) + selector.to_bytes(3, "big")


def _field_list(v6: bool, flags: bool, big_if: bool, ipfix: bool, icmp: bool = False,
                appid: bool = False) -> list[tuple[int, int]]:
    base = list(V9_FIELDS_ICMP if icmp else V9_FIELDS_V6 if v6 else V9_FIELDS_V4)
    if not flags:
        base = [f for f in base if f[0] != 6]
    if big_if:
        base = [(t, 4) if t in (10, 14) else (t, ln) for t, ln in base]
    if ipfix:
        base = [f for f in base if f[0] not in (21, 22)] + IPFIX_TIME_FIELDS
    if appid:
        base.append((95, 4))
    return base


def _encode_value(ftype: int, length: int, fl: Flow, uptime_base_ms: int) -> bytes:
    if ftype == 1:
        return fl.octets.to_bytes(length, "big")
    if ftype == 2:
        return fl.packets.to_bytes(length, "big")
    if ftype == 4:
        return bytes([fl.proto])
    if ftype == 5:
        return bytes([fl.tos])
    if ftype == 6:
        return bytes([fl.flags & 0xFF])
    if ftype == 7:
        return fl.sport.to_bytes(2, "big")
    if ftype in (11, 32):  # ICMP flows carry type*256+code where routers put the port
        return fl.dport.to_bytes(2, "big")
    if ftype in (8, 12, 15):
        addr = {8: fl.src, 12: fl.dst, 15: fl.next_hop}[ftype]
        return ipaddress.IPv4Address(addr).packed
    if ftype in (27, 28, 62):
        addr = {27: fl.src, 28: fl.dst, 62: "::"}[ftype]
        return ipaddress.IPv6Address(addr).packed
    if ftype in (9, 29):
        return bytes([fl.src_mask])
    if ftype in (13, 30):
        return bytes([fl.dst_mask])
    if ftype == 10:
        return fl.in_if.to_bytes(length, "big")
    if ftype == 14:
        return fl.out_if.to_bytes(length, "big")
    if ftype == 16:
        return fl.src_as.to_bytes(4, "big")
    if ftype == 17:
        return fl.dst_as.to_bytes(4, "big")
    if ftype == 21:
        return ((fl.end_ms - uptime_base_ms) & 0xFFFFFFFF).to_bytes(4, "big")
    if ftype == 22:
        return ((fl.start_ms - uptime_base_ms) & 0xFFFFFFFF).to_bytes(4, "big")
    if ftype == 95:
        return _app_id_bytes(*app_of(fl))
    if ftype == 152:
        return fl.start_ms.to_bytes(8, "big")
    if ftype == 153:
        return fl.end_ms.to_bytes(8, "big")
    return bytes(length)


def _pad4(b: bytes) -> bytes:
    return b + bytes((-len(b)) % 4)


def _text(s: str, length: int) -> bytes:
    """A NUL-padded fixed-length string field."""
    return s.encode()[:length].ljust(length, b"\0")


class Exporter:
    """Stateful encoder for one exporter (sequence numbers, template refresh)."""

    def __init__(self, version: int, source_id: int, flags: bool, big_if: bool, sampling: int,
                 template_every: int = 20, icmp_ie: bool = False, sampling_mode: int = 1,
                 appid: bool = False):
        self.version = version
        self.source_id = source_id
        self.flags = flags
        self.big_if = big_if and version in (9, 10)
        self.sampling = sampling
        self.sampling_mode = sampling_mode
        self.template_every = template_every
        self.icmp_ie = icmp_ie and version in (9, 10)
        self.appid = appid and version in (9, 10)
        self.packets_sent = 0
        self.seq = 0  # v9: packet sequence; IPFIX: data-record sequence; v5: flow sequence
        self.boot_ms: int | None = None

    def _templates(self) -> list[tuple[int, list[tuple[int, int]]]]:
        ipfix = self.version == 10
        out = [
            (256, _field_list(False, self.flags, self.big_if, ipfix, appid=self.appid)),
            (257, _field_list(True, self.flags, self.big_if, ipfix, appid=self.appid)),
        ]
        if self.icmp_ie:
            out.append((259, _field_list(False, True, self.big_if, ipfix, icmp=True, appid=self.appid)))
        return out

    def _template_set(self) -> bytes:
        body = b""
        for tid, fields in self._templates():
            body += struct.pack("!HH", tid, len(fields))
            for t, ln in fields:
                body += struct.pack("!HH", t, ln)
        set_id = 2 if self.version == 10 else 0
        body = _pad4(body)
        out = struct.pack("!HH", set_id, len(body) + 4) + body
        if self.sampling > 1:
            # Options template 258: scope = system/observation domain, option = SAMPLING_INTERVAL (34)
            if self.version == 10:
                ot = struct.pack("!HHH", 258, 2, 1) + struct.pack("!HH", 149, 4) + struct.pack("!HH", 34, 4)
                ot = _pad4(ot)
                out += struct.pack("!HH", 3, len(ot) + 4) + ot
            else:
                ot = struct.pack("!HHH", 258, 4, 4) + struct.pack("!HH", 1, 4) + struct.pack("!HH", 34, 4)
                ot = _pad4(ot)
                out += struct.pack("!HH", 1, len(ot) + 4) + ot
            data = _pad4(struct.pack("!II", self.source_id, self.sampling))
            out += struct.pack("!HH", 258, len(data) + 4) + data
        return out

    def _groups(self, flows: list[Flow]) -> dict[int, list]:
        """Items to export per data template id."""
        icmp = [f for f in flows if not f.v6 and f.proto == 1] if self.icmp_ie else []
        groups: dict[int, list] = {
            256: [f for f in flows if not f.v6 and not (self.icmp_ie and f.proto == 1)],
            257: [f for f in flows if f.v6]}
        if self.icmp_ie:
            groups[259] = icmp
        return groups

    def _value(self, ftype: int, length: int, item) -> bytes:
        return _encode_value(ftype, length, item, self.boot_ms)

    def _header(self, count: int, export_ms: int, payload: bytes, data_records: int) -> bytes:
        """Message header; advances the sequence (v9: datagrams, IPFIX: data records)."""
        if self.version == 9:
            uptime = (export_ms - self.boot_ms) & 0xFFFFFFFF
            hdr = struct.pack("!HHIIII", 9, count, uptime, export_ms // 1000, self.seq, self.source_id)
            self.seq = (self.seq + 1) & 0xFFFFFFFF
        else:
            hdr = struct.pack("!HHIII", 10, 16 + len(payload), export_ms // 1000, self.seq, self.source_id)
            self.seq = (self.seq + data_records) & 0xFFFFFFFF
        self.packets_sent += 1
        return hdr

    def encode(self, flows: list[Flow], export_ms: int) -> list[bytes]:
        if self.version == 5:
            return self._encode_v5([f for f in flows if not f.v6], export_ms)
        if self.boot_ms is None:
            self.boot_ms = export_ms - SYS_BOOT_OFFSET_MS
        templates = dict(self._templates())
        out: list[bytes] = []
        for tid, group in self._groups(flows).items():
            fields = templates[tid]
            rec_len = sum(ln for _, ln in fields)
            i = 0
            while i < len(group):
                with_tpl = self.packets_sent % self.template_every == 0
                if with_tpl and self.appid:
                    out += self._app_datagrams(export_ms)
                tpl = self._template_set() if with_tpl else b""
                hdr_len = 20 if self.version == 9 else 16
                room = MAX_DATAGRAM - hdr_len - len(tpl) - 4
                n = max(1, room // rec_len)
                chunk = group[i:i + n]
                i += n
                body = b"".join(
                    b"".join(self._value(t, ln, item) for t, ln in fields) for item in chunk
                )
                body = _pad4(body)
                data_set = struct.pack("!HH", tid, len(body) + 4) + body
                payload = tpl + data_set
                # The sampler options record sent with the templates is a data
                # record too: v9 counts it in the header, IPFIX in its sequence.
                options_records = 1 if with_tpl and self.sampling > 1 else 0
                count = len(chunk) + options_records + (len(self._templates()) + options_records if with_tpl else 0)
                out.append(self._header(count, export_ms, payload, len(chunk) + options_records) + payload)
        return out

    def _app_datagrams(self, export_ms: int) -> list[bytes]:
        """The NBAR2 application table (template 300: id + name + description)
        and attributes (template 301: id + category) option templates and
        their data, in datagrams of their own. Cisco sends the name and the
        category in separate records; the collector merges them."""
        ipfix = self.version == 10
        if ipfix:  # IPFIX: applicationId is the scope of both options templates
            t300 = struct.pack("!HHH", 300, 3, 1) + struct.pack("!HHHHHH", 95, 4, 96, 24, 94, 48)
            t301 = struct.pack("!HHH", 301, 2, 1) + struct.pack("!HHHH", 95, 4, 372, 32)
            tset = struct.pack("!HH", 3, len(t300) + len(t301) + 4) + t300 + t301
            scope = b""
        else:  # v9: scope System, applicationId among the options
            t300 = struct.pack("!HHH", 300, 4, 12) + struct.pack("!HHHHHHHH", 1, 4, 95, 4, 96, 24, 94, 48)
            t301 = struct.pack("!HHH", 301, 4, 8) + struct.pack("!HHHHHH", 1, 4, 95, 4, 372, 32)
            tset = struct.pack("!HH", 1, len(t300) + len(t301) + 4) + t300 + t301
            scope = struct.pack("!I", self.source_id)
        apps = sorted(APP_TABLE.items())
        out = []
        per_datagram = 6
        for i in range(0, len(apps), per_datagram):
            chunk = apps[i:i + per_datagram]
            names = _pad4(b"".join(scope + _app_id_bytes(*k) + _text(n, 24) + _text(d, 48) for k, (n, d, _) in chunk))
            cats = _pad4(b"".join(scope + _app_id_bytes(*k) + _text(c, 32) for k, (_, _, c) in chunk))
            payload = (tset + struct.pack("!HH", 300, len(names) + 4) + names
                       + struct.pack("!HH", 301, len(cats) + 4) + cats)
            out.append(self._header(2 + 2 * len(chunk), export_ms, payload, 2 * len(chunk)) + payload)
        return out

    def _encode_v5(self, flows: list[Flow], export_ms: int) -> list[bytes]:
        if self.boot_ms is None:
            self.boot_ms = export_ms - SYS_BOOT_OFFSET_MS
        out = []
        # Header word: 2 sampling-mode bits over a 14-bit interval.
        sampling_field = (self.sampling_mode << 14 | min(self.sampling, 0x3FFF)) if self.sampling > 1 else 0
        for i in range(0, len(flows), 30):
            chunk = flows[i:i + 30]
            uptime = (export_ms - self.boot_ms) & 0xFFFFFFFF
            hdr = struct.pack("!HHIIIIBBH", 5, len(chunk), uptime, export_ms // 1000,
                              (export_ms % 1000) * 1_000_000, self.seq, 0, 0, sampling_field)
            recs = b""
            for fl in chunk:
                recs += struct.pack(
                    "!4s4s4sHHIIIIHHBBBBHHBBH",
                    ipaddress.IPv4Address(fl.src).packed, ipaddress.IPv4Address(fl.dst).packed,
                    ipaddress.IPv4Address(fl.next_hop).packed, fl.in_if & 0xFFFF, fl.out_if & 0xFFFF,
                    fl.packets & 0xFFFFFFFF, fl.octets & 0xFFFFFFFF,
                    (fl.start_ms - self.boot_ms) & 0xFFFFFFFF, (fl.end_ms - self.boot_ms) & 0xFFFFFFFF,
                    fl.sport, fl.dport, 0, fl.flags & 0xFF, fl.proto, fl.tos,
                    fl.src_as & 0xFFFF, fl.dst_as & 0xFFFF, fl.src_mask, fl.dst_mask, 0,
                )
            self.seq = (self.seq + len(chunk)) & 0xFFFFFFFF
            out.append(hdr + recs)
        return out


# ── Cisco ASA NSEL (--scenario nsel) ─────────────────────────────────────────

# Session events: connection id, 5-tuple, ICMP type/code, ASA interfaces,
# post-NAT addresses/ports, firewall event + extended event, event time,
# flow-create time, initiator/responder byte and packet deltas.
NSEL_SESSION_V4 = [(148, 4), (8, 4), (12, 4), (4, 1), (7, 2), (11, 2), (176, 1), (177, 1), (10, 2), (14, 2),
                   (225, 4), (226, 4), (227, 2), (228, 2), (233, 1), (33002, 2), (323, 8), (152, 8),
                   (231, 4), (232, 4), (298, 4), (299, 4)]
NSEL_SESSION_V6 = [(148, 4), (27, 16), (28, 16), (4, 1), (7, 2), (11, 2), (178, 1), (179, 1), (10, 2), (14, 2),
                   (281, 16), (282, 16), (227, 2), (228, 2), (233, 1), (33002, 2), (323, 8), (152, 8),
                   (231, 4), (232, 4), (298, 4), (299, 4)]
# Denied connections: no counters, the ingress ACL id (12 bytes).
NSEL_DENY_V4 = [(148, 4), (8, 4), (12, 4), (4, 1), (7, 2), (11, 2), (176, 1), (177, 1), (10, 2), (14, 2),
                (233, 1), (33002, 2), (323, 8), (33000, 12)]

FW_CREATED, FW_DELETED, FW_DENIED, FW_UPDATE = 1, 2, 3, 5
PAT_ADDRESS = "198.51.100.7"  # the firewall's outside address internet-bound sessions are translated to


@dataclass
class NSELEvent:
    conn: int
    fl: Flow
    event: int
    ext_event: int
    event_ms: int
    fwd_bytes: int = 0
    rev_bytes: int = 0
    fwd_pkts: int = 0
    rev_pkts: int = 0
    xsrc: str = "0.0.0.0"
    xdst: str = "0.0.0.0"
    xsport: int = 0
    xdport: int = 0


class NSELExporter(Exporter):
    """A Cisco ASA exporting NetFlow v9 NSEL: one record per session event,
    never a traffic flow of its own. Sessions of the traffic model become
    flow-create + flow-teardown events (with a flow-update half way through
    long sessions), with initiator and responder counters in one record;
    SYN probes without an answer and refused connections become flow-denied
    events."""

    def __init__(self, source_id: int, template_every: int = 20):
        super().__init__(9, source_id, flags=True, big_if=False, sampling=1, template_every=template_every)
        self.conn = 0

    def _templates(self) -> list[tuple[int, list[tuple[int, int]]]]:
        return [(263, NSEL_SESSION_V4), (264, NSEL_DENY_V4), (265, NSEL_SESSION_V6)]

    def _nat(self, ev: NSELEvent) -> None:
        fl = ev.fl
        ev.xsrc, ev.xdst, ev.xsport, ev.xdport = fl.src, fl.dst, fl.sport, fl.dport  # identity (no NAT)
        if not fl.v6 and not ipaddress.ip_address(fl.dst).is_private:
            ev.xsrc, ev.xsport = PAT_ADDRESS, 1024 + (ev.conn * 7919) % 64000

    def _groups(self, flows: list[Flow]) -> dict[int, list]:
        groups: dict[int, list] = {263: [], 264: [], 265: []}
        for fl in flows:
            if fl.is_reply:
                continue  # reported with its initiator
            self.conn += 1
            rep = fl.reply
            ev = lambda event, ext, at, **kw: NSELEvent(self.conn, fl, event, ext, at, **kw)  # noqa: E731
            denied = (fl.proto == 6 and fl.flags == SYN and rep is None) or (rep is not None and rep.flags & RST)
            if denied:
                if not fl.v6:
                    groups[264].append(ev(FW_DENIED, 1001, fl.start_ms))  # 1001: denied by the ingress ACL
                continue
            tid = 265 if fl.v6 else 263
            events = [ev(FW_CREATED, 0, fl.start_ms)]
            fwd_b, fwd_p = fl.octets, fl.packets
            rev_b, rev_p = (rep.octets, rep.packets) if rep else (0, 0)
            if fl.end_ms - fl.start_ms > 40_000:  # long session: a flow-update carries the first half
                h = ev(FW_UPDATE, 0, (fl.start_ms + fl.end_ms) // 2, fwd_bytes=fwd_b // 2, rev_bytes=rev_b // 2,
                       fwd_pkts=fwd_p // 2, rev_pkts=rev_p // 2)
                events.append(h)
                fwd_b, rev_b, fwd_p, rev_p = fwd_b - h.fwd_bytes, rev_b - h.rev_bytes, fwd_p - h.fwd_pkts, rev_p - h.rev_pkts
            # 2004: terminated by the endpoints (TCP FIN); 2001: idle timeout
            events.append(ev(FW_DELETED, 2004 if fl.proto == 6 else 2001, fl.end_ms,
                             fwd_bytes=fwd_b, rev_bytes=rev_b, fwd_pkts=fwd_p, rev_pkts=rev_p))
            for e in events:
                self._nat(e)
            groups[tid] += events
        return groups

    def _value(self, ftype: int, length: int, ev: NSELEvent) -> bytes:
        fl = ev.fl
        icmp = fl.proto in (1, 58)
        if ftype == 148:
            return ev.conn.to_bytes(4, "big")
        if ftype in (7, 11, 227, 228) and icmp:
            return bytes(2)  # ICMP sessions carry type/code in 176/177, not ports
        if ftype in (176, 177, 178, 179):
            if not icmp:
                return bytes(1)
            return bytes([fl.dport >> 8 if ftype in (176, 178) else fl.dport & 0xFF])
        if ftype in (225, 226, 281, 282):
            return ipaddress.ip_address(ev.xsrc if ftype in (225, 281) else ev.xdst).packed
        if ftype == 227:
            return ev.xsport.to_bytes(2, "big")
        if ftype == 228:
            return ev.xdport.to_bytes(2, "big")
        if ftype == 233:
            return bytes([ev.event])
        if ftype == 33002:
            return ev.ext_event.to_bytes(2, "big")
        if ftype == 323:
            return ev.event_ms.to_bytes(8, "big")
        if ftype == 231:
            return (ev.fwd_bytes & 0xFFFFFFFF).to_bytes(4, "big")
        if ftype == 232:
            return (ev.rev_bytes & 0xFFFFFFFF).to_bytes(4, "big")
        if ftype == 298:
            return (ev.fwd_pkts & 0xFFFFFFFF).to_bytes(4, "big")
        if ftype == 299:
            return (ev.rev_pkts & 0xFFFFFFFF).to_bytes(4, "big")
        if ftype == 33000:
            return struct.pack("!III", 0x1A2B3C4D, 0, 0)
        return _encode_value(ftype, length, fl, self.boot_ms)


# ── sFlow v5 agent (--protocol sflow) ────────────────────────────────────────

SFLOW_ROUTER_AS = 64512
ETH_OVERHEAD = 18  # MAC header + FCS: an sFlow frame length is the layer-2 size


def _xdr_opaque(b: bytes) -> bytes:
    return struct.pack("!I", len(b)) + b + bytes((-len(b)) % 4)


def _sflow_addr(a: str) -> bytes:
    ip = ipaddress.ip_address(a)
    return struct.pack("!I", 1 if ip.version == 4 else 2) + ip.packed


class SFlowAgent:
    """A switch exporting sFlow v5: packets chosen 1-in-N become flow samples
    carrying the first bytes of the frame; every interface also reports its
    cumulative counters once per tick. The interface counters count ALL the
    traffic (they are the ifTable), the samples only the chosen packets."""

    def __init__(self, agent: str, sub_agent: int, sampling: int, seed: int, expanded: bool):
        self.agent = agent
        self.sub_agent = sub_agent
        self.sampling = max(1, sampling)
        self.rng = random.Random(seed + 1)  # sampling draws must not disturb the traffic model
        self.expanded = expanded
        self.seq = 0  # datagram sequence (the collector's loss measure)
        self.sample_seq = 0
        self.counter_seq = 0
        self.pool = 0
        self.boot_ms: int | None = None
        self.counters: dict[int, list[int]] = {}  # ifIndex -> [in_oct, in_pkts, out_oct, out_pkts, in_err, out_disc]

    def _account(self, fl: Flow) -> None:
        l2 = fl.octets + ETH_OVERHEAD * fl.packets
        for if_index, off in ((fl.in_if, 0), (fl.out_if, 2)):
            c = self.counters.setdefault(if_index, [0] * 6)
            c[off] += l2
            c[off + 1] += fl.packets
        if self.rng.random() < 0.05:  # the odd CRC error / output discard
            self.counters[fl.in_if][4] += 1
            self.counters[fl.out_if][5] += 1

    def _flags(self, fl: Flow) -> int:
        if fl.packets <= 2:
            return fl.flags
        return self.rng.choice([ACK, ACK, PSH | ACK])

    def _header(self, fl: Flow, frame_len: int) -> bytes:
        eth = bytes.fromhex("001b213a4b5c000c29102030")
        if fl.in_if == 1 or fl.out_if == 1:  # the access-side trunk carries VLAN 20
            eth += struct.pack("!HH", 0x8100, 20)
        l4_len = {6: 20, 17: 8, 1: 8, 58: 8}.get(fl.proto, 0)
        if fl.proto == 6:
            l4 = struct.pack("!HHIIBBHHH", fl.sport, fl.dport, self.rng.getrandbits(32), self.rng.getrandbits(32),
                             0x50, self._flags(fl), 64240, 0, 0)
        elif fl.proto == 17:
            l4 = struct.pack("!HHHH", fl.sport, fl.dport, max(8, frame_len - ETH_OVERHEAD - 20), 0)
        elif fl.proto in (1, 58):
            l4 = struct.pack("!BBHI", fl.dport >> 8, fl.dport & 0xFF, 0, 0)
        else:
            l4 = b""
        ip_len = max(frame_len - ETH_OVERHEAD, (40 if fl.v6 else 20) + l4_len)
        if fl.v6:
            ip = struct.pack("!IHBB", 0x60000000 | fl.tos << 20, ip_len - 40, fl.proto, 64)
            ip += ipaddress.IPv6Address(fl.src).packed + ipaddress.IPv6Address(fl.dst).packed
            eth += struct.pack("!H", 0x86DD)
        else:
            ip = struct.pack("!BBHHHBBH4s4s", 0x45, fl.tos, ip_len, self.rng.getrandbits(16), 0x4000, 64, fl.proto, 0,
                             ipaddress.IPv4Address(fl.src).packed, ipaddress.IPv4Address(fl.dst).packed)
            eth += struct.pack("!H", 0x0800)
        return (eth + ip + l4)[:max(0, min(128, frame_len - 4))]  # the FCS is stripped

    def _flow_sample(self, fl: Flow, frame_len: int) -> bytes:
        header = self._header(fl, frame_len)
        records = [(1, struct.pack("!III", 1, frame_len, 4) + _xdr_opaque(header))]
        next_hop = "2001:db8:ff::1" if fl.v6 else "10.255.0.1"
        records.append((1002, _sflow_addr(next_hop) + struct.pack("!II", fl.src_mask, fl.dst_mask)))
        if fl.src_as or fl.dst_as:
            path = struct.pack("!IIII", 1, 2, 1, fl.dst_as) if fl.dst_as else struct.pack("!I", 0)
            gateway = (_sflow_addr(next_hop) + struct.pack("!III", SFLOW_ROUTER_AS, fl.src_as, 0) + path
                       + struct.pack("!II", 0, 100))  # no communities, local preference 100
            records.append((1003, gateway))
        body = b"".join(struct.pack("!I", fmt) + _xdr_opaque(data) for fmt, data in records)
        self.sample_seq += 1
        if self.expanded:  # 32-bit source id type/index and interface format/value words
            head = struct.pack("!IIIIIIIIIII", self.sample_seq, 0, fl.in_if, self.sampling, self.pool & 0xFFFFFFFF, 0,
                               0, fl.in_if, 0, fl.out_if, len(records))
            return struct.pack("!I", 3) + _xdr_opaque(head + body)
        head = struct.pack("!IIIIIIII", self.sample_seq, fl.in_if & 0xFFFFFF, self.sampling, self.pool & 0xFFFFFFFF,
                           0, fl.in_if, fl.out_if, len(records))
        return struct.pack("!I", 1) + _xdr_opaque(head + body)

    def _counter_sample(self, if_index: int) -> bytes:
        in_oct, in_pkts, out_oct, out_pkts, in_err, out_disc = self.counters[if_index]
        speed = 10_000_000_000 if if_index == 2 or if_index > 65535 else 1_000_000_000
        m = 0xFFFFFFFF  # 32-bit counters wrap as on a real agent
        data = struct.pack("!IIQII", if_index, 6, speed, 1, 3)
        data += struct.pack("!QIIIIII", in_oct & 0xFFFFFFFFFFFFFFFF, in_pkts & m, 0, 0, 0, in_err & m, 0)
        data += struct.pack("!QIIIIII", out_oct & 0xFFFFFFFFFFFFFFFF, out_pkts & m, 0, 0, out_disc & m, 0, 0)
        self.counter_seq += 1
        if self.expanded:
            head = struct.pack("!IIII", self.counter_seq, 0, if_index, 1)
            fmt = 4
        else:
            head = struct.pack("!III", self.counter_seq, if_index & 0xFFFFFF, 1)
            fmt = 2
        return struct.pack("!I", fmt) + _xdr_opaque(head + struct.pack("!I", 1) + _xdr_opaque(data))

    def encode(self, flows: list[Flow], export_ms: int) -> list[bytes]:
        if self.boot_ms is None:
            self.boot_ms = export_ms - SYS_BOOT_OFFSET_MS
        samples: list[bytes] = []
        for fl in flows:
            self._account(fl)
            self.pool += fl.packets
            expected = fl.packets / self.sampling
            k = int(expected) + (1 if self.rng.random() < expected - int(expected) else 0)
            frame_len = min(9018, max(64, fl.octets // max(1, fl.packets) + ETH_OVERHEAD))
            samples += [self._flow_sample(fl, frame_len) for _ in range(k)]
        samples += [self._counter_sample(i) for i in sorted(self.counters)]
        uptime = (export_ms - self.boot_ms) & 0xFFFFFFFF
        out, batch, size = [], [], 0
        head_len = 28 + (16 if ":" in self.agent else 4)
        for sample in samples:
            if batch and head_len + size + len(sample) > MAX_DATAGRAM:
                out.append(self._datagram(batch, uptime))
                batch, size = [], 0
            batch.append(sample)
            size += len(sample)
        if batch:
            out.append(self._datagram(batch, uptime))
        return out

    def _datagram(self, samples: list[bytes], uptime: int) -> bytes:
        self.seq = (self.seq + 1) & 0xFFFFFFFF
        return (struct.pack("!I", 5) + _sflow_addr(self.agent)
                + struct.pack("!IIII", self.sub_agent, self.seq, uptime, len(samples)) + b"".join(samples))


# ── Main loop ─────────────────────────────────────────────────────────────────

def _open_socket(exporter: str, collector: tuple[str, int]) -> socket.socket:
    fam = socket.AF_INET6 if ":" in exporter else socket.AF_INET
    sock = socket.socket(fam, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    sock.bind((exporter, 0))
    sock.connect(collector)
    return sock


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--collector", default="127.0.0.1:2055", help="host:port of the collector (IPv6: [::1]:2055)")
    ap.add_argument("--exporter", default="127.0.0.61", help="source address to export from (acts as the exporter IP)")
    ap.add_argument("--protocol", choices=("netflow", "sflow"), default="netflow",
                    help="netflow: NetFlow v5/v9/IPFIX per --version; sflow: an sFlow v5 agent (a switch)")
    ap.add_argument("--version", type=int, choices=(5, 9, 10), default=9)
    ap.add_argument("--scenario", default="mixed",
                    choices=("mixed", "no-flags", "ipv6", "big-ifindex", "scan", "icmp", "v5-sampled", "all",
                             "nsel", "appid"))
    ap.add_argument("--source-id", type=int, default=1,
                    help="v9 source ID / IPFIX observation domain / sFlow sub-agent id")
    ap.add_argument("--sampling", type=int, default=0,
                    help="1-in-N packet sampling (default 1; 100 for --scenario v5-sampled; 512 for sFlow)")
    ap.add_argument("--sampling-mode", type=int, choices=(1, 2), default=1,
                    help="v5 header sampling mode bits: 1 deterministic, 2 random")
    ap.add_argument("--history-hours", type=float, default=0.0, help="backfill this many hours before now")
    ap.add_argument("--step", type=int, default=60, help="seconds of traffic per generated tick")
    ap.add_argument("--live", action="store_true", help="after history, keep exporting in real time")
    ap.add_argument("--live-seconds", type=float, default=0.0, help="stop live mode after N seconds (0 = forever)")
    ap.add_argument("--clock-offset", type=float, default=0.0, help="shift exporter timestamps by N seconds (clock skew)")
    ap.add_argument("--drop-every", type=int, default=0, help="skip sending every Nth datagram (sequence loss)")
    ap.add_argument("--pps", type=float, default=4000.0, help="max datagrams per second while backfilling")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    scenario = args.scenario
    sflow = args.protocol == "sflow"
    if sflow:
        if scenario in ("no-flags", "v5-sampled", "nsel", "appid"):
            ap.error(f"--scenario {scenario} is a NetFlow scenario")
        if args.history_hours > 0 or args.clock_offset:
            ap.error("sFlow carries no timestamps: --history-hours and --clock-offset do not apply")
        args.sampling = args.sampling or 512
    if scenario == "v5-sampled":
        if args.version != 5 and "--version" in sys.argv:
            ap.error("--scenario v5-sampled is a NetFlow v5 scenario")
        args.version = 5
        args.sampling = args.sampling or 100
    if scenario == "nsel":
        if (args.version != 9 and "--version" in sys.argv) or args.sampling > 1:
            ap.error("--scenario nsel is unsampled NetFlow v9, as Cisco ASA/FTD exports it")
        args.version = 9
    if scenario == "appid" and args.version == 5:
        ap.error("--scenario appid needs NetFlow v9 or IPFIX (v5 has no application field)")
    args.sampling = max(1, args.sampling)

    host, _, port = args.collector.rpartition(":")
    host = host.strip("[]")
    sock = _open_socket(args.exporter, (host, int(port)))
    # nsel/appid export the full traffic mix (scans, ICMP, IPv6) through
    # their own record formats; 16-bit interface fields, as those exporters use.
    model_scenario = "all" if scenario in ("nsel", "appid") else scenario
    big_if = scenario in ("big-ifindex", "all") and (sflow or args.version != 5)
    model = Model(random.Random(args.seed), model_scenario, big_ifindex=big_if)
    exp: Exporter | SFlowAgent
    if sflow:
        exp = SFlowAgent(args.exporter, args.source_id, args.sampling, args.seed, expanded=model.big_ifindex)
    elif scenario == "nsel":
        exp = NSELExporter(args.source_id)
    else:
        exp = Exporter(args.version, args.source_id, flags=scenario != "no-flags",
                       big_if=model.big_ifindex, sampling=args.sampling,
                       icmp_ie=scenario in ("icmp", "all"), sampling_mode=args.sampling_mode,
                       appid=scenario == "appid")
    step_ms = args.step * 1000
    offset_ms = int(args.clock_offset * 1000)
    sent = dropped = refused = flows_total = 0

    def export(t_ms: int, pace: bool) -> None:
        nonlocal sent, dropped, refused, flows_total
        flows = model.tick(t_ms, step_ms)
        for fl in flows:  # a router only exports flows that ended before the export
            fl.end_ms = min(fl.end_ms, t_ms + step_ms)
            fl.start_ms = min(fl.start_ms, fl.end_ms)
        if offset_ms:  # a skewed exporter clock stamps both the header and the flow times
            for fl in flows:
                fl.start_ms += offset_ms
                fl.end_ms += offset_ms
        if args.sampling > 1 and not sflow:  # a sampled flow exporter reports 1/N of the traffic
            for fl in flows:
                fl.octets = max(1, fl.octets // args.sampling)
                fl.packets = max(1, fl.packets // args.sampling)
        flows_total += len(flows)
        for dgram in exp.encode(flows, t_ms + step_ms + offset_ms):
            if args.drop_every and (sent + dropped + 1) % args.drop_every == 0:
                dropped += 1
                continue
            try:
                sock.send(dgram)
            except ConnectionRefusedError:
                # The collector was not listening when an earlier datagram
                # arrived (a restart): the kernel reports it on this connected
                # socket. A router keeps exporting regardless; so does this.
                refused += 1
                continue
            sent += 1
            if pace:
                time.sleep(1.0 / args.pps)

    now_ms = int(time.time() * 1000)
    if args.history_hours > 0:
        start = (now_ms - int(args.history_hours * 3_600_000)) // step_ms * step_ms
        t = start
        while t + step_ms <= now_ms:
            export(t, pace=True)
            t += step_ms
        print(f"history: {flows_total} flows in {sent} datagrams ({dropped} dropped, {refused} refused) "
              f"from {time.strftime('%Y-%m-%d %H:%M', time.gmtime(start / 1000))} UTC", flush=True)
    if args.live:
        deadline = time.time() + args.live_seconds if args.live_seconds else None
        t = int(time.time() * 1000) // step_ms * step_ms
        while deadline is None or time.time() < deadline:
            nxt = t + step_ms
            wait = nxt / 1000 - time.time()
            if wait > 0:
                time.sleep(wait)
            export(t, pace=False)
            t = nxt
            print(f"live: {flows_total} flows, {sent} datagrams, {dropped} dropped, {refused} refused", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
