#!/usr/bin/env python3
"""Loopback-only synthetic controller for disposable sensor VM fault tests.

This is not the application API or an appliance. Never publish its state,
certificate or test identity. Real SQL transaction tests live in
server/tests/test_sensor_event_replay.py.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
import struct
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import zlib

SENSOR_ID = "1391edbe-ed84-4c60-b690-c221f4131381"
CHECK_ID = "621fc56a-1bcf-4291-9a05-e3edf9e32ac1"


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def durable_json(path, value):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as out:
        json.dump(value, out)
        out.flush()
        os.fsync(out.fileno())
    temporary.replace(path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def connect(root):
    db = sqlite3.connect(root / "receipts.sqlite", timeout=30)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    return db


def initialize(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    (root / "state").mkdir(mode=0o700, exist_ok=True)
    if (root / "state/state.json").exists():
        raise SystemExit("Refusing to replace existing fixture identity/state")
    durable_json(root / "state/state.json", {"sensor_id": SENSOR_ID, "api_key": secrets.token_urlsafe(32)})
    with connect(root) as db:
        db.executescript("""
          CREATE TABLE batches (key TEXT PRIMARY KEY, hash TEXT NOT NULL, count INTEGER NOT NULL);
          CREATE TABLE observations (hash TEXT PRIMARY KEY, payload TEXT NOT NULL);
          CREATE TABLE requests (key TEXT NOT NULL, duplicate INTEGER NOT NULL);
        """)


def serve(root):
    identity = json.loads((root / "state/state.json").read_text())
    config = {"sensor_id": SENSOR_ID, "sensor_name": "isolated-ha-fixture", "etag": "ha-fixture-1",
              "devices": [], "service_checks": [{
                  "id": CHECK_ID, "name": "loopback TCP fixture", "check_type": "tcp",
                  "target_host": "127.0.0.1", "target_port": 19090,
                  "check_interval": 1, "timeout": 1, "retry_count": 1,
                  "retry_delay_s": 1, "enabled": True,
              }]}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Never log authentication headers.

        def reply(self, status, body):
            data = canonical(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def authorized(self):
            if self.headers.get("Authorization") != "Bearer " + identity["api_key"] or self.headers.get("X-Sensor-ID") != SENSOR_ID:
                self.reply(401, {"detail": "fixture authentication failed"})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            if self.path == "/api/v1/sensor/config":
                self.reply(200, config)
            else:
                self.reply(404, {})

        def do_POST(self):
            if not self.authorized():
                return
            size = int(self.headers.get("Content-Length", 0))
            if size > 32 * 1024 * 1024:
                self.reply(413, {})
                return
            body = json.loads(self.rfile.read(size))
            if self.path == "/api/v1/sensor/heartbeat":
                durable_json(root / "heartbeat.json", body)
                self.reply(200, {"ok": True, "config_etag": config["etag"], "commands": []})
                return
            if self.path not in {"/api/v1/sensor/results/service", "/api/v1/sensor/events"}:
                self.reply(404, {})
                return
            items, key = body["items"], body["idempotency_key"]
            with connect(root) as db:
                db.execute("BEGIN IMMEDIATE")
                prior = db.execute("SELECT hash FROM batches WHERE key=?", (key,)).fetchone()
                if prior and prior[0] != digest(items):
                    self.reply(409, {"detail": "key reused for different payload"})
                    return
                if not prior:
                    db.execute("INSERT INTO batches VALUES (?, ?, ?)", (key, digest(items), len(items)))
                    for item in items:
                        db.execute("INSERT OR IGNORE INTO observations VALUES (?, ?)", (digest(item), canonical(item)))
                db.execute("INSERT INTO requests VALUES (?, ?)", (key, int(bool(prior))))
            # Commit receipt before intentionally dropping one response.
            marker = root / "lose-next-ack"
            if marker.exists():
                marker.rename(root / "ack-was-lost")
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            self.reply(200, {"inserted": len(items), "accepted": len(items), "duplicate": bool(prior)})

    server = ThreadingHTTPServer(("127.0.0.1", 18443), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(root / "controller.crt", root / "controller.key")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


def checkpoint(root):
    hashes = set()
    for path in (root / "state/wal").glob("segment-*.wal"):
        with path.open("rb") as source:
            while header := source.read(8):
                if len(header) != 8:
                    break  # Writer may be appending; only complete frames count.
                length, expected_crc = struct.unpack(">II", header)
                if not 0 < length <= 64 * 1024 * 1024:
                    raise ValueError("invalid frame length")
                raw = source.read(length)
                if len(raw) != length:
                    break
                if zlib.crc32(raw) != expected_crc:
                    raise ValueError("WAL checksum mismatch")
                hashes.add(digest(json.loads(raw)["item"]["payload"]))
    durable_json(root / "checkpoint.json", sorted(hashes))
    print(json.dumps({"checkpoint_observations": len(hashes)}))


def report(root):
    expected = set(json.loads((root / "checkpoint.json").read_text()))
    with connect(root) as db:
        received = {row[0] for row in db.execute("SELECT hash FROM observations")}
        retries = db.execute("SELECT count(*) FROM requests WHERE duplicate=1").fetchone()[0]
        batches = db.execute("SELECT count(*) FROM batches").fetchone()[0]
    heartbeat = json.loads((root / "heartbeat.json").read_text()) if (root / "heartbeat.json").exists() else {}
    result = {"checkpoint": len(expected), "unique_received": len(received),
              "missing_checkpoint_observations": len(expected - received), "duplicate_requests": retries,
              "batches": batches, "queue_depth": heartbeat.get("queue_depth"),
              "queue_dropped_count": heartbeat.get("queue_dropped_count")}
    print(json.dumps(result, sort_keys=True))
    if not expected or expected - received:
        raise SystemExit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["init", "serve", "checkpoint", "report"])
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    {"init": initialize, "serve": serve, "checkpoint": checkpoint, "report": report}[args.action](args.root)
