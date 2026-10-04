"""First-use SSH trust. Never learns over existing or retired trust."""
import asyncio
from sqlalchemy import text
from app.services import ncm_events
from app.services.ncm_collection import probe_host_key
from app.services.ncm_errors import PrerequisiteError
from app.services.ncm_readiness import trust_state


async def lock_target(db, host, port):
    # Shared by first-use, manual registration and retirement. Different device
    # records can address the same SSH server; serialize by address AND port.
    await db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:target,0))'),
                     {'target': f'ncm-trust:{host}:{int(port or 22)}'})


async def ensure_trust(db, host, port, *, device_id, credential_id, actor=None,
                       auto_trust=False, legacy_compat=False):
    await lock_target(db, host, port)
    state = await trust_state(db, host, port, auto_trust=auto_trust)
    if state['state'] == 'pass':
        return
    if state['state'] != 'pending':
        raise PrerequisiteError(state['message'], 'host_key_untrusted')
    # Probe has no password or private key. The subsequent authenticated
    # connection remains strict and pins exactly this persisted public key.
    key = await asyncio.to_thread(probe_host_key, host, port, legacy_compat=legacy_compat)
    record = (await db.execute(text('''INSERT INTO ncm_host_keys
        (device_id,host,port,key_type,key_base64,fingerprint_sha256,
         verification_method,verification_note,registered_by_name)
        VALUES(:d,:h,:p,:kt,:kb,:fp,'first_use',:note,'automatic first use') RETURNING id'''),
        {'d': device_id, 'h': str(host), 'p': int(port or 22), 'kt': key['key_type'],
         'kb': key['key_base64'], 'fp': key['fingerprint_sha256'],
         'note': 'Automatically learned on first connection; not independently verified.'})).scalar()
    await ncm_events.audit(db, actor, 'trust.learn', device_id=device_id, resource_id=record,
                           metadata={'host': str(host), 'port': int(port or 22),
                                     'fingerprint': key['fingerprint_sha256'],
                                     'credential_id': str(credential_id), 'method': 'first_use'})
    # The caller records and commits BOTH successful and failed captures. Do
    # not release its device/host transaction locks before strict collection.
