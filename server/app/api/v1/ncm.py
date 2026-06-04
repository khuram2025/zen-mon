"""Network Configuration Management (NCM).

Slice 1: versioned config storage (content-hash dedup) + unified diff.
Slice 2 (professional): connection profiles (CLI credentials), per-device
enrollment, and REAL SSH config retrieval via netmiko, plus a manual paste
fallback. Per-device routes are under /devices/{id}/..., fleet + credential
management under /ncm/...
"""
import asyncio
import hashlib
import difflib
import json
import re
import time
from uuid import UUID
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import get_current_user, require_operator_user
from app.core import crypto
from app.models.user import User

router = APIRouter(prefix="/ncm", tags=["NCM"])
device_router = APIRouter(prefix="/devices", tags=["NCM"])

# netmiko device_type -> command used to dump the running config.
PLATFORM_COMMANDS = {
    "cisco_ios": "show running-config",
    "cisco_xe": "show running-config",
    "cisco_nxos": "show running-config",
    "cisco_asa": "show running-config",
    "arista_eos": "show running-config",
    "juniper_junos": "show configuration | display set",
    "paloalto_panos": "show config running",
    "fortinet": "show full-configuration",
    "hp_comware": "display current-configuration",
    "huawei": "display current-configuration",
    "linux": "cat /etc/os-release",
}

# Friendly labels offered in the UI; value is the netmiko device_type.
SUPPORTED_PLATFORMS = [
    {"value": "autodetect", "label": "Auto-detect"},
    {"value": "cisco_ios", "label": "Cisco IOS / IOS-XE"},
    {"value": "cisco_nxos", "label": "Cisco NX-OS"},
    {"value": "cisco_asa", "label": "Cisco ASA"},
    {"value": "arista_eos", "label": "Arista EOS"},
    {"value": "juniper_junos", "label": "Juniper Junos"},
    {"value": "paloalto_panos", "label": "Palo Alto PAN-OS"},
    {"value": "fortinet", "label": "Fortinet FortiOS"},
    {"value": "hp_comware", "label": "HPE Comware"},
    {"value": "huawei", "label": "Huawei VRP"},
]


def _netmiko_fetch(host: str, platform: str, username: str, password: str,
                   enable: str, port: int) -> tuple[str, str]:
    """Blocking SSH fetch — run via asyncio.to_thread. Returns (platform, config)."""
    from netmiko import ConnectHandler, SSHDetect

    base = {
        "host": host,
        "username": username,
        "password": password or "",
        "port": port or 22,
        "fast_cli": False,
        "conn_timeout": 20,
        "timeout": 60,
    }
    if enable:
        base["secret"] = enable

    resolved = platform
    if platform in (None, "", "autodetect"):
        guesser = SSHDetect(**{**base, "device_type": "autodetect"})
        resolved = guesser.autodetect() or "cisco_ios"

    conn = ConnectHandler(**{**base, "device_type": resolved})
    try:
        if enable:
            try:
                conn.enable()
            except Exception:
                pass
        _disable_paging(conn, resolved)
        try:
            prompt = conn.find_prompt()
        except Exception:
            prompt = ""
        cmd = PLATFORM_COMMANDS.get(resolved, "show running-config")
        # Send via the raw channel (NOT send_command, which mishandles the
        # pager) and walk any "--More--" with a space until the prompt returns.
        output = _run_command(conn, cmd, prompt)
        return resolved, _clean_cli(output)
    finally:
        try:
            conn.disconnect()
        except Exception:
            pass


def _is_paged(text: str) -> bool:
    low = (text or "").lower()
    return "--more--" in low or "---(more" in low or "<--- more --->" in low


def _disable_paging(conn, platform: str) -> None:
    """Best-effort: turn off the device pager so the full config returns in one
    read. Silently ignored when the account lacks permission (read-only)."""
    try:
        p = platform or ""
        if p == "fortinet":
            for c in ("config system console", "set output standard", "end"):
                conn.send_command_timing(c, read_timeout=15)
        elif p.startswith("cisco") or p == "arista_eos":
            conn.send_command_timing("terminal length 0", read_timeout=15)
        elif p == "paloalto_panos":
            conn.send_command_timing("set cli pager off", read_timeout=15)
        elif p == "hp_comware":
            conn.send_command_timing("screen-length disable", read_timeout=15)
        elif p == "huawei":
            conn.send_command_timing("screen-length 0 temporary", read_timeout=15)
        elif p == "juniper_junos":
            conn.send_command_timing("set cli screen-length 0", read_timeout=15)
    except Exception:
        pass


def _run_command(conn, cmd: str, prompt: str) -> str:
    """Send a command over the raw channel and stream the full output, advancing
    the device pager by sending a BURST of spaces whenever a '--More--' prompt is
    seen (far faster than one page per round-trip). Terminates when the device
    prompt returns or the channel stays silent. Works with or without paging."""
    base = (prompt or "").strip()
    try:
        conn.clear_buffer()
    except Exception:
        pass
    conn.write_channel(cmd + "\n")

    out = ""
    deadline = time.time() + 240
    idle = 0
    while time.time() < deadline:
        time.sleep(0.2)
        data = conn.read_channel()
        if data:
            out += data
            idle = 0
        else:
            idle += 1

        if _is_paged(out[-30:]):
            conn.write_channel(" " * 20)     # advance up to 20 pages at once
            time.sleep(0.1)
            continue

        if idle >= 5:                        # ~1s quiet, no pending pager
            if base and base in out[-120:]:  # prompt returned -> done
                break
            if idle >= 30:                   # ~6s total silence -> stop
                break

    # Drop the echoed command line at the top, if present.
    nl = out.find("\n")
    if nl != -1 and cmd.split()[0] in out[:nl + 2]:
        out = out[nl + 1:]
    return out


def _clean_cli(text: str) -> str:
    """Strip pager artifacts, ANSI escapes and backspaces from CLI output."""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)       # ANSI escape seqs
    text = text.replace("\x08", "")                          # backspaces
    # FortiOS prints "--More--" then erases it with CR + spaces + CR.
    text = re.sub(r"--More--[ \t]*\r?[ \t]*\r?", "", text, flags=re.I)
    text = re.sub(r"---\(more[^)]*\)---[ \t]*\r?", "", text, flags=re.I)  # Cisco
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)                    # trailing ws
    text = re.sub(r"\n{3,}", "\n\n", text)                    # collapse blank runs
    return text.strip("\n") + "\n"


# Volatile fields that change on every `show` even when the config is unchanged.
# Devices re-encrypt secrets and rotate serials/timestamps per display, so they
# must be masked before hashing/diffing or every backup looks like a change.
_VOLATILE_PATTERNS = [
    # PEM private-key/certificate bodies — re-encrypted/re-emitted each show.
    (re.compile(r"-----BEGIN [^\n]+-----.*?-----END [^\n]+-----", re.S), "<pem masked>"),
    (re.compile(r"^#conf_file_ver=.*$", re.M), "#conf_file_ver=<masked>"),     # FortiOS
    (re.compile(r"\bENC\s+[A-Za-z0-9+/=]{12,}"), "ENC <masked>"),              # FortiOS secrets
    (re.compile(r'(\bpassword\s+)(7|8|9)\s+\S+', re.I), r"\1<masked>"),         # Cisco type-7/8/9
    (re.compile(r'(secret\s+)(5|8|9)\s+\S+', re.I), r"\1<masked>"),             # Cisco enable secret
    (re.compile(r"^! Last configuration change.*$", re.M), ""),                 # Cisco
    (re.compile(r"^! NVRAM config last updated.*$", re.M), ""),                 # Cisco
    (re.compile(r"^ntp clock-period \d+$", re.M), ""),                          # Cisco
    (re.compile(r"^Building configuration.*$", re.M), ""),                      # Cisco header
    (re.compile(r"^Current configuration : \d+ bytes$", re.M), ""),            # Cisco header
]


def _normalize_config(content: str) -> str:
    """Mask volatile per-display fields so dedup/diff reflect real changes."""
    out = content
    for pat, repl in _VOLATILE_PATTERNS:
        out = pat.sub(repl, out)
    return re.sub(r"\n{2,}", "\n", out).strip()


async def _save_config_version(db: AsyncSession, device_id, config_type: str,
                               content: str, captured_by: str, source_note: Optional[str]):
    content = content.replace("\r\n", "\n")
    chash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    nhash = hashlib.sha256(_normalize_config(content).encode("utf-8")).hexdigest()
    latest = (await db.execute(
        text("""SELECT id, content_hash, norm_hash, content FROM device_configs
                WHERE device_id = :d AND config_type = :t
                ORDER BY captured_at DESC LIMIT 1"""),
        {"d": device_id, "t": config_type},
    )).first()
    # No real change if the normalized config matches (mask out re-encrypted
    # secrets / rotating serials). Fall back to raw hash for pre-norm_hash rows.
    if latest and ((latest.norm_hash and latest.norm_hash == nhash)
                   or (not latest.norm_hash and latest.content_hash == chash)):
        return {"is_change": False, "version_id": str(latest.id)}
    row = (await db.execute(
        text("""INSERT INTO device_configs
                (device_id, config_type, content, content_hash, norm_hash, size_bytes, line_count, captured_by, source_note)
                VALUES (:d, :t, :c, :h, :nh, :sz, :lc, :by, :note)
                RETURNING id, captured_at"""),
        {"d": device_id, "t": config_type, "c": content, "h": chash, "nh": nhash,
         "sz": len(content.encode("utf-8")), "lc": content.count("\n") + 1,
         "by": captured_by, "note": source_note},
    )).first()
    # A new version that supersedes a prior one is a config CHANGE — raise an
    # alert (gated by the device's alert_on_change toggle). The very first
    # capture for a device is not a change, so it is skipped.
    if latest is not None:
        await _raise_change_alert(db, device_id, config_type, latest, str(row.id), content, captured_by)
    return {"is_change": True, "version_id": str(row.id), "captured_at": row.captured_at.isoformat()}


async def _raise_change_alert(db: AsyncSession, device_id, config_type, prior,
                              new_version_id: str, new_content: str, source: str):
    """Insert a config-change alert into the Alert Center (alerts-only, no
    channel dispatch). Only for enrolled devices with alert_on_change=true."""
    en = (await db.execute(
        text("SELECT alert_on_change FROM device_ncm WHERE device_id = :d"), {"d": device_id}
    )).first()
    if not (en and en.alert_on_change):
        return
    try:
        # Diff on NORMALIZED configs so re-encrypted secrets / rotating serials
        # don't show up as changes.
        diff = list(difflib.unified_diff(
            _normalize_config(prior.content or "").splitlines(),
            _normalize_config(new_content).splitlines(), lineterm=""))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        hn = (await db.execute(
            text("SELECT hostname FROM devices WHERE id = :d"), {"d": device_id})).scalar()
        await db.execute(
            text("""INSERT INTO alerts (device_id, rule_id, status, severity, message, triggered_at, metadata)
                    VALUES (:d, NULL, 'active', 'warning', :msg, :t, CAST(:meta AS jsonb))"""),
            {
                "d": device_id,
                "msg": f"Running-config changed on {hn or device_id} (+{added} / -{removed} lines)",
                "t": datetime.now(timezone.utc),
                "meta": json.dumps({
                    "config_change": True, "config_type": config_type,
                    "from_version": str(prior.id), "to_version": new_version_id,
                    "added": added, "removed": removed, "source": source,
                }),
            },
        )
    except Exception as e:  # never let alerting break a backup
        print(f"NCM config-change alert failed: {e}")


# --------------------------------------------------------------------------- #
# Connection profiles (CLI credentials)
# --------------------------------------------------------------------------- #
class CredentialIn(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    description: Optional[str] = None
    protocol: str = Field(default="ssh", pattern="^(ssh|telnet)$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(..., min_length=1, max_length=120)
    password: Optional[str] = None
    enable_password: Optional[str] = None
    is_default: bool = False


@router.get("/platforms")
async def list_platforms(user: User = Depends(get_current_user)):
    return {"data": SUPPORTED_PLATFORMS}


@router.get("/credentials")
async def list_credentials(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(text("""
        SELECT c.id, c.name, c.description, c.protocol, c.port, c.username, c.is_default,
               (c.password_enc IS NOT NULL) AS has_password,
               (c.enable_password_enc IS NOT NULL) AS has_enable,
               (SELECT count(*) FROM device_ncm n WHERE n.credential_id = c.id) AS used_by
        FROM ncm_credentials c ORDER BY c.is_default DESC, c.name
    """))).fetchall()
    return {"data": [{
        "id": str(r.id), "name": r.name, "description": r.description,
        "protocol": r.protocol, "port": r.port, "username": r.username,
        "is_default": r.is_default, "has_password": r.has_password,
        "has_enable": r.has_enable, "used_by": r.used_by,
    } for r in rows]}


@router.post("/credentials", status_code=201)
async def create_credential(data: CredentialIn, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_operator_user)):
    row = (await db.execute(
        text("""INSERT INTO ncm_credentials
                (name, description, protocol, port, username, password_enc, enable_password_enc, is_default)
                VALUES (:n, :d, :p, :port, :u, :pw, :en, :def)
                RETURNING id"""),
        {"n": data.name, "d": data.description, "p": data.protocol, "port": data.port,
         "u": data.username, "pw": crypto.encrypt(data.password),
         "en": crypto.encrypt(data.enable_password), "def": data.is_default},
    )).first()
    if data.is_default:
        await db.execute(text("UPDATE ncm_credentials SET is_default = (id = :id)"), {"id": row.id})
    await db.commit()
    return {"id": str(row.id)}


@router.put("/credentials/{cred_id}")
async def update_credential(cred_id: UUID, data: CredentialIn, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_operator_user)):
    sets = ["name=:n", "description=:d", "protocol=:p", "port=:port", "username=:u",
            "is_default=:def", "updated_at=NOW()"]
    params = {"id": cred_id, "n": data.name, "d": data.description, "p": data.protocol,
              "port": data.port, "u": data.username, "def": data.is_default}
    # Only overwrite secrets when a new value is supplied.
    if data.password is not None:
        sets.append("password_enc=:pw"); params["pw"] = crypto.encrypt(data.password)
    if data.enable_password is not None:
        sets.append("enable_password_enc=:en"); params["en"] = crypto.encrypt(data.enable_password)
    r = (await db.execute(text(f"UPDATE ncm_credentials SET {', '.join(sets)} WHERE id=:id RETURNING id"), params)).first()
    if not r:
        raise HTTPException(status_code=404, detail="Credential not found")
    if data.is_default:
        await db.execute(text("UPDATE ncm_credentials SET is_default = (id = :id)"), {"id": cred_id})
    await db.commit()
    return {"id": str(cred_id)}


@router.delete("/credentials/{cred_id}", status_code=204)
async def delete_credential(cred_id: UUID, db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_operator_user)):
    r = (await db.execute(text("DELETE FROM ncm_credentials WHERE id=:id RETURNING id"), {"id": cred_id})).first()
    await db.commit()
    if not r:
        raise HTTPException(status_code=404, detail="Credential not found")


# --------------------------------------------------------------------------- #
# Device enrollment
# --------------------------------------------------------------------------- #
class NcmEnroll(BaseModel):
    credential_id: Optional[UUID] = None
    platform: str = Field(default="autodetect", max_length=40)
    enabled: bool = True
    schedule_enabled: bool = False
    schedule_interval_hours: int = Field(default=24, ge=1, le=720)
    alert_on_change: bool = True


@device_router.put("/{device_id}/ncm")
async def enroll_device(device_id: UUID, data: NcmEnroll, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_operator_user)):
    dev = (await db.execute(text("SELECT id FROM devices WHERE id=:id"), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    await db.execute(
        text("""INSERT INTO device_ncm (device_id, credential_id, platform, enabled, schedule_enabled, schedule_interval_hours, alert_on_change)
                VALUES (:d, :c, :p, :e, :s, :h, :ac)
                ON CONFLICT (device_id) DO UPDATE
                  SET credential_id=:c, platform=:p, enabled=:e, schedule_enabled=:s, schedule_interval_hours=:h, alert_on_change=:ac"""),
        {"d": device_id, "c": data.credential_id, "p": data.platform,
         "e": data.enabled, "s": data.schedule_enabled, "h": data.schedule_interval_hours,
         "ac": data.alert_on_change},
    )
    await db.commit()
    return {"device_id": str(device_id), "enrolled": True}


@device_router.delete("/{device_id}/ncm", status_code=204)
async def unenroll_device(device_id: UUID, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_operator_user)):
    await db.execute(text("DELETE FROM device_ncm WHERE device_id=:d"), {"d": device_id})
    await db.commit()


class _FetchError(Exception):
    pass


async def _do_fetch(db: AsyncSession, device_id) -> dict:
    """Core SSH-backup logic shared by the on-demand endpoint and the scheduler.
    Raises _FetchError(message) on failure (status already recorded)."""
    row = (await db.execute(
        text("""SELECT host(d.ip_address) AS ip, n.platform, n.credential_id,
                       c.username, c.password_enc, c.enable_password_enc, c.port
                FROM devices d
                LEFT JOIN device_ncm n ON n.device_id = d.id
                LEFT JOIN ncm_credentials c ON c.id = n.credential_id
                WHERE d.id = :id"""),
        {"id": device_id},
    )).first()
    if not row:
        raise _FetchError("Device not found")
    if not row.credential_id:
        raise _FetchError("Device is not enrolled with a connection profile")

    password = crypto.decrypt(row.password_enc) if row.password_enc else ""
    enable = crypto.decrypt(row.enable_password_enc) if row.enable_password_enc else ""
    now = datetime.now(timezone.utc)

    try:
        platform, content = await asyncio.to_thread(
            _netmiko_fetch, row.ip, row.platform or "autodetect",
            row.username, password, enable, row.port or 22,
        )
    except Exception as e:
        msg = str(e).splitlines()[0][:400] if str(e) else "connection failed"
        await db.execute(
            text("""UPDATE device_ncm SET last_status='failed', last_error=:e, last_attempt_at=:t
                    WHERE device_id=:d"""),
            {"e": msg, "t": now, "d": device_id},
        )
        await db.commit()
        raise _FetchError(msg)

    result = await _save_config_version(db, device_id, "running", content, "ssh", f"ssh:{platform}")
    await db.execute(
        text("""UPDATE device_ncm
                SET last_status='success', last_error=NULL, last_attempt_at=:t, last_success_at=:t,
                    platform = CASE WHEN platform='autodetect' THEN :pf ELSE platform END
                WHERE device_id=:d"""),
        {"t": now, "pf": platform, "d": device_id},
    )
    await db.commit()
    return {**result, "platform": platform}


@device_router.post("/{device_id}/config-fetch")
async def fetch_config(device_id: UUID, db: AsyncSession = Depends(get_db),
                       user: User = Depends(require_operator_user)):
    """Real SSH config backup via netmiko using the device's enrolled credential."""
    try:
        return await _do_fetch(db, device_id)
    except _FetchError as e:
        msg = str(e)
        if msg == "Device not found":
            raise HTTPException(status_code=404, detail=msg)
        if "not enrolled" in msg:
            raise HTTPException(status_code=400, detail=msg)
        raise HTTPException(status_code=502, detail=f"Config fetch failed: {msg}")


@router.post("/run-scheduled")
async def run_scheduled(db: AsyncSession = Depends(get_db)):
    """Internal: back up every enrolled device whose schedule is due (no auth —
    called by the localhost systemd timer). 'Due' = scheduled, enabled, with a
    credential, and last_success_at older than its interval (or never)."""
    due = (await db.execute(text("""
        SELECT device_id FROM device_ncm
        WHERE schedule_enabled = true AND enabled = true AND credential_id IS NOT NULL
          AND (last_success_at IS NULL
               OR last_success_at < NOW() - make_interval(hours => schedule_interval_hours))
    """))).fetchall()
    ok, failed = 0, 0
    for r in due:
        try:
            await _do_fetch(db, r.device_id)
            ok += 1
        except Exception:
            failed += 1
    return {"due": len(due), "backed_up": ok, "failed": failed}


# --------------------------------------------------------------------------- #
# Manual capture (paste) — slice 1
# --------------------------------------------------------------------------- #
class ConfigCapture(BaseModel):
    content: str = Field(..., min_length=1)
    config_type: str = Field(default="running", pattern="^(running|startup)$")
    source_note: Optional[str] = None
    captured_by: str = Field(default="manual", pattern="^(manual|api|ssh)$")


@device_router.post("/{device_id}/config-backup", status_code=201)
async def capture_config(device_id: UUID, data: ConfigCapture, db: AsyncSession = Depends(get_db),
                         user: User = Depends(require_operator_user)):
    dev = (await db.execute(text("SELECT id FROM devices WHERE id = :id"), {"id": device_id})).first()
    if not dev:
        raise HTTPException(status_code=404, detail="Device not found")
    result = await _save_config_version(db, device_id, data.config_type, data.content,
                                        data.captured_by, data.source_note)
    await db.commit()
    if not result["is_change"]:
        result["message"] = "No change since last backup"
    return result


@device_router.get("/{device_id}/configs")
async def list_configs(device_id: UUID, limit: int = Query(default=50, ge=1, le=500),
                       db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(
        text("""SELECT id, config_type, content_hash, size_bytes, line_count,
                       captured_at, captured_by, source_note
                FROM device_configs WHERE device_id = :d
                ORDER BY captured_at DESC LIMIT :lim"""),
        {"d": device_id, "lim": limit},
    )).fetchall()
    return {"data": [{
        "id": str(r.id), "config_type": r.config_type, "hash": r.content_hash[:12],
        "size_bytes": r.size_bytes, "line_count": r.line_count,
        "captured_at": r.captured_at.isoformat(), "captured_by": r.captured_by,
        "source_note": r.source_note,
    } for r in rows], "count": len(rows)}


@device_router.get("/{device_id}/configs/{version_id}")
async def get_config(device_id: UUID, version_id: UUID, db: AsyncSession = Depends(get_db),
                     user: User = Depends(get_current_user)):
    r = (await db.execute(
        text("""SELECT id, config_type, content, size_bytes, line_count,
                       captured_at, captured_by, source_note
                FROM device_configs WHERE id = :v AND device_id = :d"""),
        {"v": version_id, "d": device_id},
    )).first()
    if not r:
        raise HTTPException(status_code=404, detail="Config version not found")
    return {"id": str(r.id), "config_type": r.config_type, "content": r.content,
            "size_bytes": r.size_bytes, "line_count": r.line_count,
            "captured_at": r.captured_at.isoformat(), "captured_by": r.captured_by,
            "source_note": r.source_note}


@device_router.get("/{device_id}/configs-diff")
async def diff_configs(device_id: UUID, a: UUID, b: UUID, db: AsyncSession = Depends(get_db),
                       user: User = Depends(get_current_user)):
    rows = (await db.execute(
        text("SELECT id, content, captured_at FROM device_configs WHERE device_id = :d AND id IN (:a, :b)"),
        {"d": device_id, "a": a, "b": b},
    )).fetchall()
    by_id = {str(r.id): r for r in rows}
    if str(a) not in by_id or str(b) not in by_id:
        raise HTTPException(status_code=404, detail="One or both versions not found")
    ra, rb = by_id[str(a)], by_id[str(b)]
    diff = list(difflib.unified_diff(
        _normalize_config(ra.content).splitlines(), _normalize_config(rb.content).splitlines(),
        fromfile=f"{str(a)[:8]} ({ra.captured_at.isoformat()})",
        tofile=f"{str(b)[:8]} ({rb.captured_at.isoformat()})", lineterm="",
    ))
    added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
    return {"diff": "\n".join(diff), "added": added, "removed": removed, "identical": len(diff) == 0}


@router.get("/overview")
async def ncm_overview(db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (await db.execute(text("""
        SELECT d.id, d.hostname, host(d.ip_address) AS ip, d.device_type, d.vendor, d.location,
               n.credential_id, n.platform, n.enabled AS ncm_enabled, n.schedule_enabled,
               n.schedule_interval_hours, n.alert_on_change,
               n.last_status, n.last_error, n.last_attempt_at, n.last_success_at,
               cr.name AS credential_name,
               c.versions, c.last_capture, c.last_by
        FROM devices d
        LEFT JOIN device_ncm n ON n.device_id = d.id
        LEFT JOIN ncm_credentials cr ON cr.id = n.credential_id
        LEFT JOIN (
            SELECT device_id, count(*) AS versions, max(captured_at) AS last_capture,
                   (array_agg(captured_by ORDER BY captured_at DESC))[1] AS last_by
            FROM device_configs GROUP BY device_id
        ) c ON c.device_id = d.id
        ORDER BY (n.device_id IS NULL), (c.last_capture IS NULL), c.last_capture DESC NULLS LAST, d.hostname
    """))).fetchall()
    data, backed_up, enrolled = [], 0, 0
    for r in rows:
        if r.versions:
            backed_up += 1
        is_enrolled = r.platform is not None  # a device_ncm row exists for this device
        if is_enrolled:
            enrolled += 1
        data.append({
            "device_id": str(r.id), "hostname": r.hostname, "ip": r.ip,
            "device_type": r.device_type, "vendor": r.vendor,
            "location": r.location,
            "enrolled": is_enrolled,
            "credential_id": str(r.credential_id) if r.credential_id else None,
            "credential_name": r.credential_name,
            "platform": r.platform,
            "schedule_enabled": r.schedule_enabled,
            "schedule_interval_hours": r.schedule_interval_hours,
            "alert_on_change": r.alert_on_change,
            "last_status": r.last_status,
            "last_error": r.last_error,
            "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
            "versions": r.versions or 0,
            "last_capture": r.last_capture.isoformat() if r.last_capture else None,
            "last_by": r.last_by,
        })
    return {"data": data, "total_devices": len(rows), "backed_up": backed_up, "enrolled": enrolled}
