"""SSH test terminal (Settings > General > Monitoring). Administrators only.

Open a session with a username and password, stream its output, send
keystrokes. The password is used once and never stored or logged; the audit
trail records who opened a session to which host and as which account.

Host keys are confirmed on first use: an unknown key answers 409
``host_key_unknown`` with its fingerprint, and the client retries with
``trust_fingerprint`` once the administrator has compared it with the device.
A changed key answers 409 ``host_key_changed`` until the stored key is
forgotten (``DELETE /host-keys``).
"""
from typing import Optional

import base64
import binascii

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_permission
from app.models.user import User
from app.services import ssh_host_keys
from app.services import ssh_terminal as term
from app.services.audit_service import write_audit_log

router = APIRouter(prefix="/system/ssh-terminal", tags=["SSH terminal"])

_HOST = r'^[A-Za-z0-9._:\-\[\]%]+$'


class OpenIn(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=_HOST)
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(min_length=1, max_length=128, pattern=r'^[^\x00-\x1f\x7f]+$')
    password: str = Field(min_length=1, max_length=1024, repr=False)
    cols: int = Field(default=120, ge=20, le=500)
    rows: int = Field(default=32, ge=5, le=200)
    trust_fingerprint: Optional[str] = Field(default=None, pattern=r'^SHA256:[A-Za-z0-9+/]{43}$')


class ForgetKeyIn(BaseModel):
    host: str = Field(min_length=1, max_length=253, pattern=_HOST)
    port: int = Field(default=22, ge=1, le=65535)


class InputIn(BaseModel):
    data: str = Field(max_length=term.MAX_INPUT_BYTES * 2)


class ResizeIn(BaseModel):
    cols: int = Field(ge=20, le=500)
    rows: int = Field(ge=5, le=200)


def _fail(exc: term.TerminalError):
    if exc.detail:
        raise HTTPException(exc.status, {**exc.detail, 'message': str(exc)}) from None
    raise HTTPException(exc.status, str(exc)) from None


@router.post("/sessions", status_code=201)
async def open_terminal(data: OpenIn, db: AsyncSession = Depends(get_db),
                        user: User = Depends(require_permission("settings.manage"))):
    known = await ssh_host_keys.active_keys(db, data.host, data.port)
    try:
        session_id, accepted = await term.open_session(user, data.host, data.port, data.username,
                                                       data.password, data.cols, data.rows,
                                                       known_keys=known, confirmed=data.trust_fingerprint)
    except term.TerminalError as exc:
        metadata = {"host": data.host, "port": data.port, "username": data.username, "error": str(exc)}
        if exc.detail:
            metadata.update({k: v for k, v in exc.detail.items() if k != "message"})
        await write_audit_log(db, actor=user, action="ssh_terminal.open_failed", resource_type="ssh_terminal",
                              metadata=metadata)
        await db.commit()
        _fail(exc)
    if accepted:
        key_type, key_b64 = accepted
        await ssh_host_keys.record(db, data.host, data.port, key_type, key_b64,
                                   method=ssh_host_keys.TERMINAL_CONFIRMED,
                                   note="Fingerprint confirmed in the SSH terminal", actor=user)
        await write_audit_log(db, actor=user, action="ssh_terminal.host_key_trusted", resource_type="ssh_terminal",
                              resource_id=session_id,
                              metadata={"host": data.host, "port": data.port, "key_type": key_type,
                                        "fingerprint": ssh_host_keys.fingerprint(key_type, key_b64)})
    await write_audit_log(db, actor=user, action="ssh_terminal.open", resource_type="ssh_terminal",
                          resource_id=session_id,
                          metadata={"host": data.host, "port": data.port, "username": data.username})
    await db.commit()
    return {"session_id": session_id, "idle_timeout_seconds": term.IDLE_SECONDS,
            "max_seconds": term.MAX_SECONDS}


@router.get("/sessions/{session_id}/stream")
async def stream_terminal(session_id: str, after: str = "0",
                          user: User = Depends(require_permission("settings.manage"))):
    try:
        await term.meta_for(session_id, user)
    except term.TerminalError as exc:
        _fail(exc)
    return StreamingResponse(term.stream(session_id, user, after), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/sessions/{session_id}/input", status_code=204)
async def terminal_input(session_id: str, data: InputIn,
                         user: User = Depends(require_permission("settings.manage"))):
    try:
        if len(base64.b64decode(data.data, validate=True)) > term.MAX_INPUT_BYTES:
            raise HTTPException(413, "Input too large")
        await term.send_input(session_id, user, {"t": "d", "d": data.data})
    except binascii.Error:
        raise HTTPException(422, "Input must be base64") from None
    except term.TerminalError as exc:
        _fail(exc)


@router.post("/sessions/{session_id}/resize", status_code=204)
async def terminal_resize(session_id: str, data: ResizeIn,
                          user: User = Depends(require_permission("settings.manage"))):
    try:
        await term.send_input(session_id, user, {"t": "r", "cols": data.cols, "rows": data.rows})
    except term.TerminalError as exc:
        _fail(exc)


@router.delete("/sessions/{session_id}", status_code=204)
async def close_terminal(session_id: str, user: User = Depends(require_permission("settings.manage"))):
    try:
        await term.close_session(session_id, user)
    except term.TerminalError as exc:
        _fail(exc)


@router.delete("/host-keys")
async def forget_host_key(data: ForgetKeyIn, db: AsyncSession = Depends(get_db),
                          user: User = Depends(require_permission("settings.manage"))):
    """Forget the stored keys for host:port (after a device was replaced or
    re-keyed). The next connection asks for confirmation again, and the next
    NCCM backup records the new key."""
    count = await ssh_host_keys.retire(db, data.host, data.port, actor=user,
                                       reason="Forgotten in the SSH terminal")
    await write_audit_log(db, actor=user, action="ssh_terminal.host_key_forgotten", resource_type="ssh_terminal",
                          metadata={"host": data.host, "port": data.port, "keys": count})
    await db.commit()
    return {"forgotten": count}
