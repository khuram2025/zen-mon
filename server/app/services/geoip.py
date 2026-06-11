"""Dependency-free GeoIP / ASN enrichment (Phase 2b).

Reads optional DB-IP Lite / GeoLite2 ``.mmdb`` files from ``NETFLOW_GEOIP_DIR``.
The MMDB reader is **vendored** (pure Python, no pip dependency) so this feature
can never break the OTA ``pip_install`` step, and when no database is present
every lookup is a graceful no-op.

DB-IP Lite data is licensed CC-BY-4.0 (https://db-ip.com). Provision it with
``scripts/fetch-geoip.py``.
"""
from __future__ import annotations

import ipaddress
import os
import struct
import threading

GEOIP_DIR = os.getenv("NETFLOW_GEOIP_DIR", "/opt/zenplus/data/geoip")
_COUNTRY_DB = "country.mmdb"
_ASN_DB = "asn.mmdb"

_METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
_DATA_SEP = 16


class MMDBReader:
    """Minimal MaxMind DB reader covering the types used by DB-IP / GeoLite2
    country & ASN databases. Format: https://maxmind.github.io/MaxMind-DB/"""

    def __init__(self, path: str):
        with open(path, "rb") as f:
            self._buf = f.read()
        marker = self._buf.rfind(_METADATA_MARKER)
        if marker == -1:
            raise ValueError("not a MaxMind DB (metadata marker not found)")
        meta_start = marker + len(_METADATA_MARKER)
        meta, _ = self._decode(meta_start, base=meta_start)
        self.node_count = int(meta["node_count"])
        self.record_size = int(meta["record_size"])
        self.ip_version = int(meta["ip_version"])
        self.node_bytes = self.record_size * 2 // 8
        self.search_tree_size = self.node_count * self.node_bytes
        self.data_base = self.search_tree_size + _DATA_SEP
        self._ipv4_root = None

    def _read_node(self, node: int, index: int) -> int:
        off = node * self.node_bytes
        b = self._buf
        rs = self.record_size
        if rs == 24:
            base = off + index * 3
            return (b[base] << 16) | (b[base + 1] << 8) | b[base + 2]
        if rs == 28:
            if index == 0:
                return ((b[off + 3] & 0xF0) << 20) | (b[off] << 16) | (b[off + 1] << 8) | b[off + 2]
            return ((b[off + 3] & 0x0F) << 24) | (b[off + 4] << 16) | (b[off + 5] << 8) | b[off + 6]
        if rs == 32:
            base = off + index * 4
            return struct.unpack(">I", b[base:base + 4])[0]
        raise ValueError(f"unsupported record size {rs}")

    def _ipv4_start(self) -> int:
        if self.ip_version == 4:
            return 0
        if self._ipv4_root is None:
            node = 0
            for _ in range(96):
                if node >= self.node_count:
                    break
                node = self._read_node(node, 0)
            self._ipv4_root = node
        return self._ipv4_root

    def get(self, ip_str: str):
        try:
            addr = ipaddress.ip_address(ip_str)
        except ValueError:
            return None
        packed = addr.packed
        if addr.version == 4:
            node = self._ipv4_start()
            bits = 32
        else:
            node = 0
            bits = 128
        for i in range(bits):
            if node >= self.node_count:
                break
            byte = packed[i >> 3]
            bit = (byte >> (7 - (i & 7))) & 1
            node = self._read_node(node, bit)
        if node <= self.node_count:
            return None  # node_count == empty; < == ran out of bits with no data
        offset = (node - self.node_count) + self.search_tree_size
        value, _ = self._decode(offset, base=self.data_base)
        return value

    def _decode(self, offset: int, base: int):
        b = self._buf
        ctrl = b[offset]
        offset += 1
        dtype = ctrl >> 5
        if dtype == 0:
            dtype = 7 + b[offset]
            offset += 1
        if dtype == 1:  # pointer
            return self._decode_pointer(ctrl, offset, base)
        size = ctrl & 0x1F
        if size >= 29:
            if size == 29:
                size = 29 + b[offset]; offset += 1
            elif size == 30:
                size = 285 + ((b[offset] << 8) | b[offset + 1]); offset += 2
            else:
                size = 65821 + ((b[offset] << 16) | (b[offset + 1] << 8) | b[offset + 2]); offset += 3
        if dtype == 2:  # string
            return b[offset:offset + size].decode("utf-8", "replace"), offset + size
        if dtype == 7:  # map
            out = {}
            o = offset
            for _ in range(size):
                key, o = self._decode(o, base)
                val, o = self._decode(o, base)
                out[key] = val
            return out, o
        if dtype in (5, 6, 9, 10):  # uint16/32/64/128
            return int.from_bytes(b[offset:offset + size], "big"), offset + size
        if dtype == 11:  # array
            arr = []
            o = offset
            for _ in range(size):
                v, o = self._decode(o, base)
                arr.append(v)
            return arr, o
        if dtype == 8:  # int32
            return int.from_bytes(b[offset:offset + size], "big", signed=True), offset + size
        if dtype == 14:  # boolean
            return bool(size), offset
        if dtype == 4:  # bytes
            return b[offset:offset + size], offset + size
        if dtype == 15:  # float
            return struct.unpack(">f", b[offset:offset + 4])[0], offset + 4
        if dtype == 3:  # double
            return struct.unpack(">d", b[offset:offset + 8])[0], offset + 8
        return None, offset + size  # 12 cache container / 13 end marker

    def _decode_pointer(self, ctrl: int, offset: int, base: int):
        b = self._buf
        ss = (ctrl >> 3) & 0x3
        if ss == 0:
            ptr = ((ctrl & 0x7) << 8) | b[offset]; offset += 1
        elif ss == 1:
            ptr = (((ctrl & 0x7) << 16) | (b[offset] << 8) | b[offset + 1]) + 2048; offset += 2
        elif ss == 2:
            ptr = (((ctrl & 0x7) << 24) | (b[offset] << 16) | (b[offset + 1] << 8) | b[offset + 2]) + 526336; offset += 3
        else:
            ptr = int.from_bytes(b[offset:offset + 4], "big"); offset += 4
        value, _ = self._decode(base + ptr, base)
        return value, offset


# ── Service: lazy, mtime-reloaded, graceful ──────────────────────────────────
_readers: dict[str, tuple] = {}  # filename -> (mtime, reader|None)
_lock = threading.Lock()


def _reader_for(filename: str):
    path = os.path.join(GEOIP_DIR, filename)
    try:
        mtime = os.stat(path).st_mtime
    except OSError:
        return None
    with _lock:
        cached = _readers.get(filename)
        if cached and cached[0] == mtime:
            return cached[1]
    try:
        reader = MMDBReader(path)
    except Exception:
        reader = None
    with _lock:
        _readers[filename] = (mtime, reader)
    return reader


def available() -> bool:
    return _reader_for(_COUNTRY_DB) is not None or _reader_for(_ASN_DB) is not None


def country_of(ip: str) -> tuple[str | None, str | None]:
    r = _reader_for(_COUNTRY_DB)
    if not r:
        return (None, None)
    try:
        d = r.get(ip) or {}
        c = d.get("country") or d.get("registered_country") or {}
        return (c.get("iso_code"), (c.get("names") or {}).get("en"))
    except Exception:
        return (None, None)


def asn_of(ip: str) -> tuple[int | None, str | None]:
    r = _reader_for(_ASN_DB)
    if not r:
        return (None, None)
    try:
        d = r.get(ip) or {}
        return (d.get("autonomous_system_number"), d.get("autonomous_system_organization"))
    except Exception:
        return (None, None)


def enrich(ip: str) -> dict:
    country, country_name = country_of(ip)
    asn, as_name = asn_of(ip)
    return {"country": country, "country_name": country_name, "asn": asn, "as_name": as_name}
