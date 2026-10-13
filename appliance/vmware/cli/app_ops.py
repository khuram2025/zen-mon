#!/opt/zenplus/venv/bin/python
"""Fixed local SDK operations. Installed root-owned, executed as zenplus only.

This is not an HTTP endpoint or an alternate web authentication mechanism.
The authenticated physical-console administrator invokes it through the root CLI.
Secrets arrive through stdin and never appear in arguments or response output.
"""
import asyncio
import json
import logging
import os
import pwd
import sys
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID


def register_models():
    import importlib
    import pkgutil
    import app.models
    # The web app imports all routers before using ORM models. This smaller SDK
    # process must register the complete model graph without starting the web app.
    for module in pkgutil.iter_modules(app.models.__path__):
        importlib.import_module('app.models.'+module.name)


async def dispatch(request):
    register_models()
    from sqlalchemy import select, text
    from app.core.database import AsyncSessionLocal, engine
    from app.models.user import User
    actor = SimpleNamespace(id=None, username='console:admin', role='console_admin')
    operation = request.get('operation')
    async with AsyncSessionLocal() as db:
        if operation in ('setup-status', 'setup-web-password'):
            user = (await db.execute(select(User).where(User.username == 'admin').with_for_update())).scalar_one()
            if operation == 'setup-status':
                return {'must_change_password': bool(user.must_change_password)}
            if not user.must_change_password:
                raise ValueError('Web administrator is already configured; use the dashboard to change its password')
            from app.api.v1.users import ChangePasswordRequest, change_own_password
            return await change_own_password(ChangePasswordRequest(
                current_password=request['current_password'], new_password=request['new_password']),
                db=db, current_user=user)
        if operation == 'license':
            row = (await db.execute(text('SELECT plan,status,started_at,expires_at,max_devices,max_service_checks,max_users FROM subscriptions ORDER BY started_at DESC LIMIT 1'))).mappings().first()
            return dict(row) if row else {'status': 'unavailable'}
        if operation == 'license-activate':
            from app.api.v1.system_updates import RegisterRequest, register_appliance
            result = await register_appliance(RegisterRequest(license_key=request['license_key']), user=actor, db=db)
            # Never return registration credentials to the terminal.
            return {'status': 'registered', 'message': 'License activation completed'}
        if operation == 'updates':
            from app.api.v1.system_updates import get_update_status
            result = await get_update_status(user=actor)
            data = result.model_dump(mode='json')
            return {k: data[k] for k in ('current_version','auto_update','timer_active','last_check','next_check','updater_running','last_update')}
        if operation in ('backups', 'backup-create', 'backup-restore'):
            from app.api.v1.storage_management import list_backups, create_backup, restore_backup, CreateBackupRequest, RestoreRequest
            if operation == 'backups':
                return await list_backups(db=db, user=actor)
            if operation == 'backup-create':
                result = await create_backup(CreateBackupRequest(include_clickhouse=bool(request.get('full')), note='Created from Zentryc console'), db=db, user=actor)
            else:
                result = await restore_backup(UUID(request['id']), RestoreRequest(components=['postgres','config']), db=db, user=actor)
            # Routes create background tasks. Keep this service-account process alive
            # until they finish, otherwise a CLI-created backup would never complete.
            pending = asyncio.all_tasks() - {asyncio.current_task()}
            if pending:
                await asyncio.gather(*pending)
            rows = await list_backups(db=db, user=actor)
            selected = next((r for r in rows if str(r['id']) == str(request.get('id', result.get('id')))), None)
            if selected and (selected['status'] != 'completed' or (operation == 'backup-restore' and selected['last_restore_status'] != 'completed')):
                raise ValueError('Backup operation failed; inspect show backups')
            return selected or result
    await engine.dispose()
    raise ValueError('Unsupported application operation')


def main():
    if os.geteuid() != pwd.getpwnam('zenplus').pw_uid:
        raise SystemExit('Run only as the appliance service account')
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise SystemExit('Request too large')
    request = json.loads(raw)
    os.chdir('/opt/zenplus')
    # dotenv handles quoting without displaying the installed environment.
    from dotenv import load_dotenv
    load_dotenv('/opt/zenplus/.env')
    sys.path[:0] = ['/opt/zenplus/server', '/opt/zenplus']
    logging.disable(logging.CRITICAL)
    try:
        result = asyncio.run(dispatch(request))
        print(json.dumps({'ok': True, 'result': result}, default=str))
    except Exception as exc:
        # Framework HTTPException details are intended user-facing errors. Hide
        # other exception strings, which may include URLs containing credentials.
        from fastapi import HTTPException
        message = str(exc.detail) if isinstance(exc, HTTPException) else 'Application operation failed; check service health and audit log'
        print(json.dumps({'ok': False, 'error': message}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
