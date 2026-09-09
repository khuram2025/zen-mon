"""Trust for outbound probes, separate from the appliance's HTTPS identity."""
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import require_admin_user
from app.models.user import User
from app.services.audit_service import write_audit_log
from app.services.probe_trust import (
    MAX_CERTIFICATES, SETTINGS_KEY, load_probe_trust, lock_probe_trust,
    parse_trust_certificate, save_probe_trust,
)

router = APIRouter(prefix="/system/security/probe-trust", tags=["Security Settings"])


def public_status(policy):
    return {"auto_fetch_intermediates": policy.get("auto_fetch_intermediates", True),
            "certificates": [{k: v for k, v in cert.items() if k != "pem"} for cert in policy.get("certificates", [])]}


class ProbeTrustConfig(BaseModel):
    auto_fetch_intermediates: bool


@router.get("")
async def get_trust(db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    return public_status(await load_probe_trust(db))


@router.put("")
async def configure_trust(data: ProbeTrustConfig, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    policy = await lock_probe_trust(db)
    policy["auto_fetch_intermediates"] = data.auto_fetch_intermediates
    await save_probe_trust(db, policy)
    await write_audit_log(db, actor=user, action="security.probe_trust.config", resource_type="system_settings", resource_id=SETTINGS_KEY, metadata=data.model_dump())
    await db.commit()
    return public_status(policy)


@router.post("/certificates")
async def install_certificate(certificate: UploadFile = File(...), label: str = Form("", max_length=100), hosts: str = Form("", max_length=8192), db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    data = await certificate.read(65537)
    try:
        entry = parse_trust_certificate(data, label, hosts)
    except Exception as exc:
        # Parse failures contain no uploaded content or private key material.
        detail = str(exc) if isinstance(exc, ValueError) else "Invalid certificate or self signature"
        raise HTTPException(422, detail) from None
    policy = await lock_probe_trust(db)
    certificates = policy.setdefault("certificates", [])
    if any(cert["id"] == entry["id"] for cert in certificates):
        raise HTTPException(409, "Certificate already installed; remove it first to change its scope")
    if len(certificates) >= MAX_CERTIFICATES:
        raise HTTPException(422, f"At most {MAX_CERTIFICATES} certificates may be installed")
    certificates.append(entry)
    await save_probe_trust(db, policy)
    await write_audit_log(db, actor=user, action="security.probe_trust.install", resource_type="certificate", resource_id=entry["id"], metadata={"label": entry["label"], "hosts": entry["hosts"], "is_ca": entry["is_ca"]})
    await db.commit()
    return public_status(policy)


@router.delete("/certificates/{fingerprint}")
async def remove_certificate(fingerprint: str, db: AsyncSession = Depends(get_db), user: User = Depends(require_admin_user)):
    policy = await lock_probe_trust(db)
    certificates = policy.get("certificates", [])
    remaining = [cert for cert in certificates if cert["id"] != fingerprint]
    if len(remaining) == len(certificates):
        raise HTTPException(404, "Certificate not found")
    policy["certificates"] = remaining
    await save_probe_trust(db, policy)
    await write_audit_log(db, actor=user, action="security.probe_trust.remove", resource_type="certificate", resource_id=fingerprint)
    await db.commit()
    return public_status(policy)
