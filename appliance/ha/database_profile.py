"""Render a fresh PostgreSQL/etcd cell; never adopt an installed database.

This is the operational database layer, not a complete application HA installer.
Generated files contain credentials and must stay outside source/release trees.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import re


NAME = re.compile(r"[a-z][a-z0-9-]{0,47}\Z")
PROFILES = {"strict": 3, "economy": 2}


def validate(inventory):
    if set(inventory) != {"cluster", "profile", "nodes"}:
        raise ValueError("Inventory requires exactly cluster, profile and nodes")
    if not NAME.fullmatch(inventory["cluster"]):
        raise ValueError("Invalid cluster identity")
    if inventory["profile"] not in PROFILES:
        raise ValueError("Profile must be strict or economy")
    nodes = inventory["nodes"]
    if len(nodes) != 3:
        raise ValueError("A cell requires exactly three independent DCS voters")
    names, addresses = set(), set()
    for n in nodes:
        if set(n) != {"name", "address", "data"} or type(n["data"]) is not bool:
            raise ValueError("Each node requires name, address and boolean data")
        if not NAME.fullmatch(n["name"]) or n["name"] in names:
            raise ValueError("Node names must be valid and unique")
        ip = ipaddress.ip_address(n["address"])
        if ip.version != 4 or ip.is_unspecified or ip.is_multicast or ip.is_loopback or str(ip) in addresses:
            raise ValueError("Node addresses must be unique unicast IPv4 addresses")
        names.add(n["name"])
        addresses.add(str(ip))
    if sum(n["data"] for n in nodes) != PROFILES[inventory["profile"]]:
        raise ValueError("Incorrect number of data nodes for profile")


def render(inventory, node_name, passwords, *, lab=False):
    validate(inventory)
    node = next((n for n in inventory["nodes"] if n["name"] == node_name), None)
    if node is None:
        raise ValueError("Node is not a member of this cell")
    if set(passwords) != {"superuser", "replication", "rewind"} or any(
        not isinstance(v, str) or len(v) < 32 or len(v) > 256 or "\x00" in v for v in passwords.values()
    ):
        raise ValueError("Three distinct database credentials of 32 to 256 characters are required")
    if len(set(passwords.values())) != 3:
        raise ValueError("Database credentials must be distinct")
    ip, name, cluster = node["address"], node["name"], inventory["cluster"]
    tls = "/etc/zenplus-ha/tls"
    peers = inventory["nodes"]
    data = [n for n in peers if n["data"]]
    etcd = {
        "name": name, "data-dir": "/var/lib/zenplus-ha/etcd",
        "listen-peer-urls": f"https://{ip}:2380",
        "initial-advertise-peer-urls": f"https://{ip}:2380",
        "listen-client-urls": f"https://{ip}:2379",
        "advertise-client-urls": f"https://{ip}:2379",
        "initial-cluster": ",".join(f"{n['name']}=https://{n['address']}:2380" for n in peers),
        "initial-cluster-state": "new", "initial-cluster-token": cluster,
        "auto-compaction-mode": "periodic", "auto-compaction-retention": "1h",
        "quota-backend-bytes": 256 * 1024 * 1024,
        "client-transport-security": {"cert-file": f"{tls}/etcd.crt", "key-file": f"{tls}/etcd.key",
            "client-cert-auth": True, "trusted-ca-file": f"{tls}/ca.crt"},
        "peer-transport-security": {"cert-file": f"{tls}/etcd.crt", "key-file": f"{tls}/etcd.key",
            "client-cert-auth": True, "trusted-ca-file": f"{tls}/ca.crt"},
        "logger": "zap", "log-level": "warn",
    }
    files = {"etcd.json": etcd}
    if node["data"]:
        files["patroni.json"] = {
            "scope": cluster, "namespace": "/zenplus/", "name": name,
            "restapi": {"listen": f"{ip}:8008", "connect_address": f"{ip}:8008",
                "certfile": f"{tls}/patroni.crt", "keyfile": f"{tls}/patroni.key",
                "cafile": f"{tls}/ca.crt", "verify_client": "required"},
            "ctl": {"certfile": f"{tls}/patroni.crt", "keyfile": f"{tls}/patroni.key", "cacert": f"{tls}/ca.crt"},
            "etcd3": {"hosts": [f"{n['address']}:2379" for n in peers], "protocol": "https",
                "cacert": f"{tls}/ca.crt", "cert": f"{tls}/patroni.crt", "key": f"{tls}/patroni.key"},
            "bootstrap": {
                "dcs": {"ttl": 30, "loop_wait": 5, "retry_timeout": 5,
                    "check_timeline": True, "maximum_lag_on_failover": 0,
                    "synchronous_mode": True, "synchronous_mode_strict": inventory["profile"] == "strict",
                    "synchronous_node_count": 1, "failsafe_mode": False,
                    "postgresql": {"use_pg_rewind": True, "use_slots": True,
                        "parameters": {"wal_level": "replica", "hot_standby": "on", "wal_log_hints": "on",
                            "synchronous_commit": "on", "fsync": "on", "full_page_writes": "on",
                            "max_wal_senders": 10, "max_replication_slots": 10,
                            "wal_keep_size": "128MB", "max_slot_wal_keep_size": "1GB",
                            "max_wal_size": "1GB", "password_encryption": "scram-sha-256"}}},
                "initdb": [{"encoding": "UTF8"}, "data-checksums"],
            },
            "postgresql": {"listen": f"{ip}:5432", "connect_address": f"{ip}:5432",
                "data_dir": "/var/lib/zenplus-ha/postgresql", "bin_dir": "/usr/lib/postgresql/16/bin",
                "pgpass": "/var/lib/zenplus-ha/pgpass",
                "authentication": {
                    "superuser": {"username": "postgres", "password": passwords["superuser"],
                        "sslmode": "verify-full", "sslrootcert": f"{tls}/ca.crt"},
                    "replication": {"username": "replicator", "password": passwords["replication"],
                        "sslmode": "verify-full", "sslrootcert": f"{tls}/ca.crt"},
                    "rewind": {"username": "rewind_user", "password": passwords["rewind"],
                        "sslmode": "verify-full", "sslrootcert": f"{tls}/ca.crt"}},
                "parameters": {"ssl": "on", "ssl_cert_file": f"{tls}/postgres.crt",
                    "ssl_key_file": f"{tls}/postgres.key", "ssl_min_protocol_version": "TLSv1.2",
                    "shared_buffers": "128MB", "unix_socket_directories": "/var/run/postgresql"},
                "pg_hba": ["local all postgres peer"] + [
                    f"hostssl {db} {user} {n['address']}/32 scram-sha-256"
                    for n in data for db, user in (("replication", "replicator"), ("postgres", "rewind_user"), ("all", "postgres"))
                ] + ["host all all 0.0.0.0/0 reject", "host all all ::0/0 reject"],
            },
            "watchdog": {"mode": "off" if lab else "required", "device": "/dev/watchdog", "safety_margin": -1},
            "tags": {"nofailover": False, "noloadbalance": False, "nosync": False},
        }
    files["profile.json"] = {"schema": 1, "cluster": cluster, "node": name, "profile": inventory["profile"],
        "lab_only": lab, "controller_ha_qualified": False, "autonomous_site_qualified": False,
        "notification_failover_qualified": False, "data_node": node["data"],
        "protection": "two-copy-required" if inventory["profile"] == "strict" else "single-copy-possible"}
    return files


def write_new(output, files):
    output = Path(output)
    # Refuse reuse: bootstrap settings must never silently mutate an existing cell.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    for name, value in files.items():
        fd = os.open(output / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
            f.write("\n")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--inventory", required=True)
    p.add_argument("--node", required=True)
    p.add_argument("--credentials", required=True, help="Private JSON file; never pass secret values on the command line")
    p.add_argument("--output", required=True, help="New private directory, outside repositories")
    p.add_argument("--lab-only-no-watchdog", action="store_true", help="Unqualified disposable lab only")
    args = p.parse_args()
    if os.name != "posix":
        p.error("Render private deployment files on Linux, where owner-only permissions are enforced")
    if Path(args.credentials).stat().st_mode & 0o077:
        p.error("Credential file must be owner-only")
    files = render(json.loads(Path(args.inventory).read_text()), args.node,
                   json.loads(Path(args.credentials).read_text()), lab=args.lab_only_no_watchdog)
    write_new(args.output, files)


if __name__ == "__main__":
    main()
