"""Separate local console credentials; no web password or MFA state is changed."""
import fcntl
from contextlib import contextmanager
import json
import os
import secrets
import time
from pathlib import Path


class AuthError(Exception):
    pass


def password_context():
    from passlib.context import CryptContext
    return CryptContext(schemes=['bcrypt_sha256', 'bcrypt'], deprecated='auto')


def atomic_private(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    temp = path.with_name('.' + path.name + '.' + secrets.token_hex(8))
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


class ConsoleAuth:
    def __init__(self, directory='/var/lib/zentryc', owner_uid=0, clock=time.time,
                 crypto=None):
        self.directory = Path(directory)
        self.path = self.directory / 'console-auth.json'
        self.attempts = self.directory / 'console-login-attempts.json'
        self.owner_uid = owner_uid
        self.clock = clock
        self.crypto = crypto or password_context()

    @contextmanager
    def locked(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        fd = os.open(self.directory / 'console-auth.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read(self, path):
        if path.is_symlink():
            raise AuthError('Console credential storage is unsafe')
        info = path.stat()
        if info.st_uid != self.owner_uid or info.st_mode & 0o077:
            raise AuthError('Console credential permissions are unsafe')
        return json.loads(path.read_text(encoding='utf-8'))

    def initialize(self, setup_password):
        with self.locked():
            if not self.path.exists():
                if (self.directory / 'console-claimed.json').exists():
                    raise AuthError('Console credentials missing; authorized recovery is required')
                atomic_private(self.path, {'format': 1, 'username': 'admin',
                                          'password_hash': self.crypto.hash(setup_password),
                                          'must_change_password': True, 'revision': 1})
            return self._read(self.path)

    def authenticate(self, username, password):
        with self.locked():
            return self._authenticate(username, password)

    def _authenticate(self, username, password):
        state = self._read(self.path)
        attempts = self._read(self.attempts) if self.attempts.exists() else {}
        now = self.clock()
        if attempts.get('locked_until', 0) > now:
            raise AuthError('Console login is temporarily locked; retry in one minute')
        if now - attempts.get('window_started', 0) > 60:
            attempts = {'failures': 0, 'window_started': now}
        # Verify even for a wrong username to avoid a username timing oracle.
        valid = self.crypto.verify(password, state['password_hash'])
        if username != state['username'] or not valid:
            attempts['failures'] = attempts.get('failures', 0) + 1
            if attempts['failures'] >= 5:
                attempts['locked_until'] = now + 60
            atomic_private(self.attempts, attempts)
            raise AuthError('Console login failed')
        atomic_private(self.attempts, {'failures': 0, 'window_started': now})
        return {'username': state['username'], 'revision': state['revision'],
                'must_change_password': state['must_change_password']}

    def validate(self, session):
        current = self._read(self.path)
        if not session or session['username'] != current['username'] or \
                session['revision'] != current['revision']:
            raise AuthError('Console session revoked; sign in again')
        if current['must_change_password']:
            raise AuthError('Initial console password must be changed')
        return current

    def change_password(self, new_password, setup_password):
        with self.locked():
            return self._change_password(new_password, setup_password)

    def _change_password(self, new_password, setup_password):
        if not 12 <= len(new_password) <= 128:
            raise AuthError('Use a console password with 12 through 128 characters')
        if new_password == setup_password:
            raise AuthError('Choose a password different from the setup password')
        if not all((any(c.islower() for c in new_password),
                    any(c.isupper() for c in new_password),
                    any(c.isdigit() for c in new_password),
                    any(not c.isalnum() for c in new_password))):
            raise AuthError('Use uppercase, lowercase, a number and a symbol')
        current = self._read(self.path)
        if self.crypto.verify(new_password, current['password_hash']):
            raise AuthError('Choose a password different from the current password')
        current.update(password_hash=self.crypto.hash(new_password),
                       must_change_password=False, revision=current['revision'] + 1)
        atomic_private(self.path, current)
        return {'username': current['username'], 'revision': current['revision'],
                'must_change_password': False}
