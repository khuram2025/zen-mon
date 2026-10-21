"""SSH host keys the appliance has seen, in ``ncm_host_keys``.

Two writers, two policies:

* The SSH test terminal confirms on first use. An unknown key is shown to the
  administrator as a fingerprint and only a confirmed key is accepted; a
  changed key is refused until an administrator forgets the stored one.
  The password is never sent before the key is accepted.
* NCCM backups trust on first use and only warn: the first key a device
  presents is recorded, and a later different key of the same type is
  audited as ``backup.host_key_changed`` while the backup still runs.

Rows written here carry their own ``verification_method`` so the UDT gateway
collector, which enforces operator-registered keys, keeps its behaviour.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from typing import Optional

from sqlalchemy import text

logger = logging.getLogger(__name__)

FIRST_USE = "first_use"
TERMINAL_CONFIRMED = "terminal_confirmed"
RECORDED_METHODS = (FIRST_USE, TERMINAL_CONFIRMED)


def fingerprint(key_type: str, key_b64: str) -> str:
    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


async def active_keys(db, host: str, port: int) -> list[tuple[str, str]]:
    rows = (await db.execute(text(
        "SELECT key_type, key_base64 FROM ncm_host_keys "
        "WHERE host=:host AND port=:port AND retired_at IS NULL"),
        {"host": host, "port": port})).all()
    return [(r[0], r[1]) for r in rows]


async def record(db, host: str, port: int, key_type: str, key_b64: str, *, method: str,
                 note: str, actor=None, device_id=None) -> None:
    await db.execute(text(
        """INSERT INTO ncm_host_keys(device_id, host, port, key_type, key_base64, fingerprint_sha256,
               verification_method, verification_note, registered_by, registered_by_name)
           VALUES (:device, :host, :port, :type, :b64, :fp, :method, :note, :by, :by_name)
           ON CONFLICT DO NOTHING"""),
        {"device": device_id, "host": host, "port": port, "type": key_type, "b64": key_b64,
         "fp": fingerprint(key_type, key_b64), "method": method, "note": note,
         "by": getattr(actor, "id", None), "by_name": getattr(actor, "username", None) or "system"})


async def retire(db, host: str, port: int, *, actor, reason: str) -> int:
    result = await db.execute(text(
        """UPDATE ncm_host_keys SET retired_at=now(), retired_by_name=:by, retire_reason=:reason
           WHERE host=:host AND port=:port AND retired_at IS NULL"""),
        {"host": host, "port": port, "by": getattr(actor, "username", None) or "system", "reason": reason})
    return result.rowcount or 0


def compare(known: list[tuple[str, str]], key_type: str, key_b64: str) -> tuple[str, Optional[str]]:
    """('match' | 'unknown' | 'changed', previous fingerprint of the same type)."""
    same_type = [b64 for t, b64 in known if t == key_type]
    if key_b64 in same_type:
        return "match", None
    if same_type:
        return "changed", fingerprint(key_type, same_type[0])
    return "unknown", None


async def observe_backup_key(db, device_id, host: str, port: int,
                             observed: Optional[dict]) -> Optional[dict]:
    """Warn-only trust for NCCM. Records the first key of each type a device
    presents; a different key of a known type is reported, never blocked."""
    if not observed or not observed.get("type") or not observed.get("base64"):
        return None
    key_type, key_b64 = observed["type"], observed["base64"]
    status, previous = compare(await active_keys(db, host, port), key_type, key_b64)
    current = fingerprint(key_type, key_b64)
    if status == "unknown":
        await record(db, host, port, key_type, key_b64, method=FIRST_USE,
                     note="Recorded on first NCCM connection", device_id=device_id)
        return {"status": "first_use", "fingerprint": current, "key_type": key_type}
    if status == "changed":
        logger.warning("SSH host key for %s:%s changed (%s, was %s)", host, port, current, previous)
        return {"status": "changed", "fingerprint": current, "previous_fingerprint": previous,
                "key_type": key_type}
    return {"status": "match", "fingerprint": current, "key_type": key_type}
