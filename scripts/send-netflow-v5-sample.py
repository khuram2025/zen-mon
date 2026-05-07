#!/usr/bin/env python3
"""Send a deterministic NetFlow v5 sample packet for collector smoke tests."""

from __future__ import annotations

import argparse
import socket
import struct
import time


def ip4(addr: str) -> bytes:
    return socket.inet_aton(addr)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=2055)
    parser.add_argument("--src", default="10.10.10.25")
    parser.add_argument("--dst", default="172.16.20.50")
    parser.add_argument("--dst-port", type=int, default=443)
    parser.add_argument("--bytes", type=int, default=125000)
    parser.add_argument("--packets", type=int, default=500)
    args = parser.parse_args()

    sys_uptime_ms = 600_000
    now = int(time.time())
    header = struct.pack(
        "!HHIIIIBBH",
        5,                 # version
        1,                 # count
        sys_uptime_ms,
        now,
        0,                 # nsec
        now % 100000,      # flow sequence
        0,                 # engine type
        0,                 # engine id
        1,                 # sampling interval
    )
    record = b"".join(
        [
            ip4(args.src),
            ip4(args.dst),
            ip4("10.10.10.1"),
            struct.pack("!HHII", 1, 2, args.packets, args.bytes),
            struct.pack("!II", sys_uptime_ms - 12_000, sys_uptime_ms - 2_000),
            struct.pack("!HHBBBBHHBBH", 51515, args.dst_port, 0, 0x1B, 6, 0, 64512, 64513, 24, 24, 0),
        ]
    )
    packet = header + record
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.sendto(packet, (args.host, args.port))
    print(f"sent 1 NetFlow v5 record to {args.host}:{args.port} {args.src} -> {args.dst}:{args.dst_port}")


if __name__ == "__main__":
    main()
