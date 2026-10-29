"""Fail-closed local admission for a commissioned active/standby controller.

The root supervisor writes a short-lived /run grant only after a protected DB
commit. This is an admission guard, not infrastructure fencing. Standalone
installations remain unchanged when ZENPLUS_CONTROLLER_GRANT is unset.
"""
import json
import os
from pathlib import Path
import time


def validate(path, *, now=None, boot=None):
    p = Path(path)
    st = p.stat()
    if not p.is_file() or p.is_symlink() or st.st_uid != 0 or st.st_mode & 0o022:
        raise ValueError('Controller grant is not root controlled')
    grant = json.loads(p.read_text())
    boot = boot or Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    now = time.clock_gettime(time.CLOCK_BOOTTIME) if now is None else now
    if (grant.get('boot_id') != boot or type(grant.get('generation')) is not int
            or grant['generation'] < 1 or not 0 < grant.get('valid_until', 0)-now <= 12):
        raise ValueError('Controller grant absent, expired or from another boot')
    return grant


def permitted():
    path = os.environ.get('ZENPLUS_CONTROLLER_GRANT')
    if path is None:
        return True
    if not path:
        return False
    try:
        validate(path)
        return True
    except (OSError, ValueError, TypeError, KeyError):
        return False


class ControllerAdmissionMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] in ('http', 'websocket') and not permitted():
            if scope['type'] == 'websocket':
                await send({'type': 'websocket.close', 'code': 1013})
            else:
                await send({'type': 'http.response.start', 'status': 503, 'headers':
                            [(b'content-type', b'application/json'), (b'retry-after', b'3')]})
                await send({'type': 'http.response.body', 'body': b'{"detail":"Controller authority unavailable"}'})
            return
        await self.app(scope, receive, send)
