"""Read-only CLI collection with explicit completion and format checks."""
from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
from app.services.ncm_content import checked_content, MAX_CONFIG_BYTES

# Whole-capture deadline for one configuration command, in seconds. Resolution
# (capture_deadline): device override > connection profile > driver default
# (``capture_timeout`` in DRIVERS) > DEFAULT_CAPTURE_TIMEOUT. Configured values
# are bounded to CAPTURE_TIMEOUT_MIN..CAPTURE_TIMEOUT_MAX.
DEFAULT_CAPTURE_TIMEOUT = 240
CAPTURE_TIMEOUT_MIN = 60
CAPTURE_TIMEOUT_MAX = 3600
DEFAULT_IDLE_TIMEOUT = 20

# One entry per implemented collector. `scope` states what the artifact is
# (and is not) so recovery claims stay honest; `omissions` lists what the
# vendor commonly leaves out of a CLI text capture. `tested` is the evidence
# level shipped with the product: every driver is fixture-tested; none is
# certified against real hardware until an operator records a restore test.
DRIVERS = {
    'cisco_ios': dict(label='Cisco IOS / IOS-XE', vendor='Cisco', running='show running-config',
                      startup='show startup-config', pager='terminal length 0',
                      marker=r'(?m)^(?:hostname\s|version\s|interface\s)', end=r'(?m)^end\s*$',
                      scope='Running/startup configuration text visible to the account.',
                      omissions='Low privilege levels or parser views can hide sections; certificates private keys are not exported.'),
    'cisco_xe': dict(label='Cisco IOS-XE', vendor='Cisco', running='show running-config',
                     startup='show startup-config', pager='terminal length 0',
                     marker=r'(?m)^(?:hostname\s|version\s|interface\s)', end=r'(?m)^end\s*$',
                     scope='Running/startup configuration text visible to the account.',
                     omissions='Low privilege levels or parser views can hide sections; certificates private keys are not exported.',
                     hidden=True),
    'cisco_nxos': dict(label='Cisco NX-OS', vendor='Cisco', running='show running-config',
                       startup='show startup-config', pager='terminal length 0',
                       marker=r'(?m)^(?:hostname\s|version\s|interface\s)',
                       scope='Running/startup configuration text for the default VDC.',
                       omissions='Other VDCs, licences and checkpoint files are not captured.'),
    'cisco_asa': dict(label='Cisco ASA', vendor='Cisco', running='show running-config',
                      startup='show startup-config', pager='terminal pager 0',
                      marker=r'(?m)^(?:hostname\s|ASA Version\s|interface\s)',
                      scope='Running/startup configuration of the current context.',
                      omissions='Other security contexts, certificates private keys, AnyConnect images and licences are not captured.'),
    'arista_eos': dict(label='Arista EOS', vendor='Arista', running='show running-config',
                       startup='show startup-config', pager='terminal length 0',
                       marker=r'(?m)^(?:hostname\s|! device:|interface\s)', end=r'(?m)^end\s*$',
                       scope='Running/startup configuration text.',
                       omissions='Flash files, extensions and certificates private keys are not captured.'),
    'juniper_junos': dict(label='Juniper Junos', vendor='Juniper', running='show configuration | display set',
                          pager='set cli screen-length 0',
                          marker=r'(?m)^set\s+(?:system|interfaces|routing-options|security|protocols)\s',
                          scope='Committed configuration in set format.',
                          omissions='Rescue configuration, candidate changes and licence keys are not captured.'),
    'paloalto_panos': dict(label='Palo Alto PAN-OS', vendor='Palo Alto', running='show config running',
                           pager='set cli pager off',
                           marker=r'(?m)^\s*(?:<config(?:\s|>)|config\s*\{|devices\s*\{)',
                           scope='Running configuration text (not a device-state bundle).',
                           omissions='Candidate configuration, device-state, certificates private keys and Panorama-pushed objects are not captured.'),
    # Large FortiGate configurations are multi-megabyte and, with the console
    # in its default "output more" mode, page every screen; the pager is walked
    # (config system console is persistent configuration and never changed).
    'fortinet': dict(label='Fortinet FortiOS', vendor='Fortinet', running='show full-configuration',
                     capture_timeout=900, idle_timeout=60,
                     marker=r'(?m)^config\s+system\s',
                     scope='CLI text of the configuration visible to the administrator profile (global and VDOMs it can read).',
                     omissions='Restricted profiles hide sections; this is not the native encrypted backup file.'),
    'hp_comware': dict(label='HPE Comware', vendor='HPE', running='display current-configuration',
                       startup='display saved-configuration', pager='screen-length disable',
                       marker=r'(?m)^(?:\s*sysname\s|\s*version\s)', end=r'(?m)^return\s*$',
                       scope='Current/saved configuration text.', omissions='Keys and licence files are not captured.'),
    'huawei': dict(label='Huawei VRP', vendor='Huawei', running='display current-configuration',
                   startup='display saved-configuration', pager='screen-length 0 temporary',
                   marker=r'(?m)^(?:\s*sysname\s|!Software Version)', end=r'(?m)^return\s*$',
                   scope='Current/saved configuration text.', omissions='Keys and licence files are not captured.'),
    'aruba_aoscx': dict(label='Aruba AOS-CX', vendor='Aruba', running='show running-config',
                        startup='show startup-config', pager='no page',
                        marker=r'(?m)^(?:!Version ArubaOS-CX|hostname\s|interface\s|vlan\s)',
                        scope='Running/startup configuration text.',
                        omissions='Checkpoints, certificates private keys and VSX peer configuration are not captured.'),
    'aruba_osswitch': dict(label='ArubaOS-Switch / ProCurve', vendor='Aruba', running='show running-config',
                           startup='show config', pager='no page',
                           marker=r'(?m)^(?:;\s*\S+\s+Configuration Editor|hostname\s)',
                           scope='Running/startup configuration text.',
                           omissions='Password manager secrets may be hidden unless include-credentials is set; flash images are not captured.'),
    'aruba_os': dict(label='ArubaOS Mobility Controller', vendor='Aruba', running='show running-config',
                     startup='show startup-config', pager='no paging',
                     marker=r'(?m)^(?:version\s+\d|hostname\s|interface\s)',
                     scope='Running/startup configuration of this controller.',
                     omissions='Mobility Conductor hierarchy, certificates and licences are not captured.'),
}
# Drivers deliberately not offered, with the reason shown in the support matrix.
UNSUPPORTED = {
    'F5': 'Netmiko session preparation for TMOS changes persistent CLI preferences, and a CLI text dump is not a UCS recovery archive. A native UCS collector is planned.',
}
# Netmiko autodetection names mapped to the collector that handles them.
DETECT_ALIASES = {'hp_procurve': 'aruba_osswitch', 'aruba_procurve': 'aruba_osswitch', 'cisco_xe': 'cisco_ios'}

PLATFORM_COMMANDS = {k: v['running'] for k, v in DRIVERS.items()}
STARTUP_COMMANDS = {k: v['startup'] for k, v in DRIVERS.items() if v.get('startup')}


def platform_catalog():
    """Public driver metadata for the UI: capability, scope and omissions."""
    rows = [{'value': 'autodetect', 'label': 'Auto-detect', 'config_types': ['running', 'startup'],
             'capability_resolved': False,
             'scope': 'Resolved at the first capture; startup support depends on the detected driver.'}]
    for key, d in DRIVERS.items():
        if d.get('hidden'):
            continue
        rows.append({'value': key, 'label': d['label'], 'vendor': d['vendor'], 'capability_resolved': True,
                     'config_types': ['running', 'startup'] if d.get('startup') else ['running'],
                     'scope': d['scope'], 'omissions': d['omissions'], 'artifact_format': 'cli_text',
                     'capture_timeout_default': d.get('capture_timeout', DEFAULT_CAPTURE_TIMEOUT)})
    return rows


def capture_deadline(platform, device_timeout=None, profile_timeout=None):
    """(seconds, source) of the capture deadline for one attempt. ``source`` is
    device, profile, driver or system, so the UI can say where it comes from."""
    for value, source in ((device_timeout, 'device'), (profile_timeout, 'profile')):
        if value is not None:
            return max(CAPTURE_TIMEOUT_MIN, min(CAPTURE_TIMEOUT_MAX, int(value))), source
    driver = DRIVERS.get(platform or '', {})
    if driver.get('capture_timeout'):
        return driver['capture_timeout'], 'driver'
    return DEFAULT_CAPTURE_TIMEOUT, 'system'


class CaptureProgress:
    """Counters of a running capture (never content), readable from another
    thread. The job worker persists ``snapshot()`` on the job row."""

    def __init__(self, deadline_seconds=None):
        self._lock = threading.Lock()
        self._started = time.monotonic()
        self._capture_started = None
        self._data = {'phase': 'connecting', 'command': None, 'platform': None, 'bytes': 0, 'lines': 0,
                      'pages': 0, 'deadline_seconds': deadline_seconds}

    def update(self, **values):
        with self._lock:
            if values.get('phase') == 'capturing' and self._capture_started is None:
                self._capture_started = time.monotonic()
            self._data.update(values)

    def snapshot(self):
        with self._lock:
            now = time.monotonic()
            data = dict(self._data)
            data['elapsed_seconds'] = round(now - self._started, 1)
            data['capture_elapsed_seconds'] = round(now - self._capture_started, 1) if self._capture_started is not None else None
            return data


class CaptureError(ValueError):
    """A safe-to-display collection error, never containing CLI output."""

    def __init__(self, message, category='incomplete'):
        super().__init__(message)
        self.category = category


def clean_cli(value: str) -> str:
    value = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', value).replace('\x08', '')
    # FortiOS erases an answered "--More-- " with CR, spaces, CR. Netmiko's
    # read_channel turns each lone CR into LF, so the erase arrives as
    # LF/spaces/LF; remove it whole, or every page boundary of a large
    # configuration gains two blank lines.
    value = re.sub(r'--More--[ \t]*(?:[\r\n][ \t]+[\r\n]|\r?[ \t]*\r?)|---\(more[^)]*\)---|<--- more --->|-- MORE --, next page: Space, next line: Enter, quit: Control-C', '', value, flags=re.I)
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'[ \t]+\n', '\n', value).strip('\n') + '\n'


_PAGER = r'--more--|---\(more[^)]*\)---|<--- more --->|-- MORE --'


def is_paged(value: str) -> bool:
    return bool(re.search(_PAGER, value, re.I))


def _mib(n):
    return f'{n / 1048576:.1f} MiB' if n >= 1048576 else f'{n / 1024:.0f} KiB'


def run_command(conn, command: str, prompt: str, *, timeout: float = DEFAULT_CAPTURE_TIMEOUT,
                idle_timeout: float = DEFAULT_IDLE_TIMEOUT, progress: CaptureProgress | None = None) -> str:
    """Send ``command`` and read until the exact device prompt returns.

    Incremental: each read is appended once and only the new data (plus a
    short overlap) is scanned for a pager, so multi-megabyte, heavily paged
    output (FortiOS "--More--" every screen) costs linear time. While data is
    flowing the channel is polled every 20 ms instead of 200 ms, which is what
    bounded a 1 MB paged FortiOS capture to the old fixed deadline.
    Completion still needs the final prompt line followed by a quiet second;
    the caller validates the whole configuration (end markers) afterwards.
    """
    prompt = (prompt or '').strip()
    if not prompt or '\n' in prompt or '\r' in prompt:
        raise CaptureError('Cannot establish a reliable device prompt', 'driver')
    conn.clear_buffer()
    conn.write_channel(command + '\n')
    started = time.monotonic()
    deadline = started + timeout
    last_data = started
    chunks: list[str] = []
    received_chars = received_bytes = lines = pages = 0
    tail = ''                  # newest raw output, for prompt detection
    pager_advanced_at = -1     # absolute character offset of the last pager answered
    if progress is not None:
        progress.update(phase='capturing', command=command, deadline_seconds=int(timeout))
    while True:
        now = time.monotonic()
        if now >= deadline:
            break
        data = conn.read_channel()
        if data:
            overlap = tail[-128:]
            scan_base = received_chars - len(overlap)
            chunks.append(data)
            received_chars += len(data)
            received_bytes += len(data.encode('utf-8'))
            lines += data.count('\n')
            last_data = time.monotonic()
            if received_bytes > MAX_CONFIG_BYTES:
                raise CaptureError('Configuration exceeds the 16 MiB limit', 'format')
            tail = (tail + data)[-4096:]
            pager = None
            for pager in re.finditer(_PAGER, overlap + data, re.I):
                pass
            if pager is not None and scan_base + pager.end() > pager_advanced_at:
                conn.write_channel(' ')
                pager_advanced_at = scan_base + pager.end()
                pages += 1
            if progress is not None:
                progress.update(bytes=received_bytes, lines=lines, pages=pages)
            continue
        quiet = time.monotonic() - last_data
        # An exact final prompt line followed by a quiet interval, not a
        # substring in a banner, establishes command completion.
        if quiet >= 1:
            tail_lines = clean_cli(tail).rstrip().splitlines()
            if tail_lines and tail_lines[-1].strip() == prompt:
                if progress is not None:
                    progress.update(phase='validating', bytes=received_bytes, lines=lines, pages=pages)
                cleaned = clean_cli(''.join(chunks)).rstrip()
                out = cleaned.splitlines()
                out.pop()
                if out and out[0].strip() in (command, prompt + command, prompt + ' ' + command):
                    out.pop(0)
                return checked_content('\n'.join(out) + '\n')
        if quiet >= idle_timeout:
            raise CaptureError(f'Incomplete capture: device prompt did not return '
                               f'(no output for {int(idle_timeout)} s after {_mib(received_bytes)})')
        time.sleep(.02 if quiet < 1 else .2)
    raise CaptureError(f'Incomplete capture: collection deadline exceeded ({int(timeout)} s, '
                       f'{_mib(received_bytes)} and {pages} pages received); raise the capture deadline '
                       f'on the device or connection profile if the configuration is this large')


def validate_config(content: str, platform: str) -> str:
    content = checked_content(content)
    if platform not in DRIVERS:
        raise CaptureError('Unsupported device platform; select a supported driver', 'driver')
    if re.search(r'(?im)^\s*(?:%\s*(?:invalid|error|unknown|incomplete|ambiguous|access denied)|'
                 r'(?:error:|syntax error|permission denied|command not found|unknown command|'
                 r'invalid command|invalid input|authorization failed|access denied|command fail))', content):
        raise CaptureError('Device rejected the configuration command: verify read permissions and the selected driver', 'privilege')
    if is_paged(content):
        raise CaptureError('Incomplete capture: unresolved pager')
    driver = DRIVERS[platform]
    if not re.search(driver['marker'], content):
        raise CaptureError('Configuration does not match the selected device format', 'format')
    if driver.get('end') and not re.search(driver['end'], content):
        marker = 'return' if 'return' in driver['end'] else 'end'
        raise CaptureError(f'Incomplete capture: missing configuration {marker} marker')
    if platform == 'paloalto_panos' and content.lstrip().startswith('<'):
        import xml.etree.ElementTree as ET
        try:
            ET.fromstring(content)
        except ET.ParseError:
            raise CaptureError('Incomplete or invalid XML configuration') from None
    if platform == 'fortinet':
        opens = len(re.findall(r'(?m)^\s*config\s+', content))
        closes = len(re.findall(r'(?m)^\s*end\s*$', content))
        if opens != closes:
            raise CaptureError('Incomplete configuration blocks')
    return content


def disable_paging(conn, platform: str) -> None:
    # FortiOS config system console changes persistent configuration. Walk its
    # pager instead. Failure of session-only preparation is safe: the reader
    # still requires a complete response and handles pagination.
    command = DRIVERS.get(platform, {}).get('pager')
    if command:
        try:
            conn.send_command_timing(command, read_timeout=15)
        except Exception:
            pass


def key_fingerprint(key_type: str, key_b64: str) -> str:
    digest = hashlib.sha256(base64.b64decode(key_b64)).digest()
    return 'SHA256:' + base64.b64encode(digest).decode().rstrip('=')


class KeyLoadError(CaptureError):
    """The profile's private key could not be loaded. ``reason`` is one of
    key_format, key_passphrase_required, key_passphrase_wrong,
    key_unsupported. Messages are application-authored (never key material)."""

    def __init__(self, message, reason):
        super().__init__(message, 'key')
        self.reason = reason


class KeyRejectedError(CaptureError):
    """The device refused the profile's SSH key (or offers no key login)."""

    def __init__(self, message):
        super().__init__(message, 'authentication')
        self.reason = 'key_rejected'


KEY_HELP = 'Paste an OpenSSH or PEM private key (Ed25519, ECDSA P-256/384/521 or RSA).'
_PASSPHRASE_REQUIRED = 'The private key is encrypted: save its passphrase on the connection profile.'


def load_private_key(text, passphrase=None):
    """Paramiko key from pasted text, or KeyLoadError naming the cause.

    Accepts OpenSSH (``BEGIN OPENSSH PRIVATE KEY``), traditional PEM (RSA/EC)
    and PKCS#8 (``BEGIN [ENCRYPTED] PRIVATE KEY``), encrypted or not. The key
    stays in memory: it is handed to Paramiko as ``pkey`` and never written
    to disk.
    """
    import io
    import warnings
    import paramiko
    from cryptography.exceptions import UnsupportedAlgorithm
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import dsa, ec, ed25519, rsa

    text = (text or '').strip().replace('\r\n', '\n')
    if not text:
        raise KeyLoadError('No private key is saved on the connection profile. ' + KEY_HELP, 'key_format')
    if text.startswith('PuTTY-User-Key-File-'):
        raise KeyLoadError('PuTTY .ppk keys are not supported: export the key in OpenSSH format '
                           '(PuTTYgen > Conversions > Export OpenSSH key) and save it again.', 'key_unsupported')
    if re.match(r'(?:ssh-|ecdsa-|sk-)\S+\s+AAAA', text) or 'PUBLIC KEY-----' in text:
        raise KeyLoadError('This is a public key. Save the PRIVATE key on the profile; the public key '
                           'belongs on the device account.', 'key_format')
    data = text.encode() + b'\n'
    secret = passphrase.encode() if passphrase else None
    with warnings.catch_warnings():
        # DSA keys are refused below; do not log cryptography's deprecation.
        warnings.simplefilter('ignore')
        try:
            if text.startswith('-----BEGIN OPENSSH PRIVATE KEY-----'):
                try:
                    key = serialization.load_ssh_private_key(data, password=secret)
                except TypeError:
                    # A passphrase was supplied for an unencrypted key: ignore it.
                    if secret is None:
                        raise
                    key = serialization.load_ssh_private_key(data, password=None)
            elif re.match(r'-----BEGIN (?:RSA |EC |DSA |ENCRYPTED )?PRIVATE KEY-----', text):
                try:
                    key = serialization.load_pem_private_key(data, password=secret)
                except TypeError:
                    if secret is None:
                        raise KeyLoadError(_PASSPHRASE_REQUIRED, 'key_passphrase_required') from None
                    # A passphrase was supplied for an unencrypted key: ignore it.
                    key = serialization.load_pem_private_key(data, password=None)
            else:
                raise KeyLoadError('The private key is not in a recognised format. ' + KEY_HELP, 'key_format')
        except KeyLoadError:
            raise
        except UnsupportedAlgorithm:
            raise KeyLoadError('Unsupported private key type or encryption (for example a FIDO/security-key or '
                               'Ed448 key). ' + KEY_HELP, 'key_unsupported') from None
        except (TypeError, ValueError) as exc:
            message = str(exc).lower()
            if secret is None and ('password' in message or 'encrypted' in message):
                raise KeyLoadError(_PASSPHRASE_REQUIRED, 'key_passphrase_required') from None
            if secret is not None and ('password' in message or 'decrypt' in message or 'checksum' in message):
                raise KeyLoadError('The saved passphrase does not decrypt the private key: re-enter the passphrase '
                                   'on the connection profile.', 'key_passphrase_wrong') from None
            raise KeyLoadError('The private key is damaged or incomplete (copy the whole BEGIN ... END block). '
                               + KEY_HELP, 'key_format') from None
    if isinstance(key, dsa.DSAPrivateKey):
        raise KeyLoadError('DSA (ssh-dss) keys are not supported: modern SSH servers and the bundled client '
                           'disable them. ' + KEY_HELP, 'key_unsupported')
    if isinstance(key, ec.EllipticCurvePrivateKey) and key.curve.name not in ('secp256r1', 'secp384r1', 'secp521r1'):
        raise KeyLoadError(f'Unsupported ECDSA curve {key.curve.name}. ' + KEY_HELP, 'key_unsupported')
    classes = ((ed25519.Ed25519PrivateKey, paramiko.Ed25519Key), (ec.EllipticCurvePrivateKey, paramiko.ECDSAKey),
               (rsa.RSAPrivateKey, paramiko.RSAKey))
    cls = next((pk for ck, pk in classes if isinstance(key, ck)), None)
    if cls is None:
        raise KeyLoadError('Unsupported private key type. ' + KEY_HELP, 'key_unsupported')
    # Re-encode unencrypted, in memory only, in the one format every Paramiko
    # key class reads.
    plain = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
                              serialization.NoEncryption()).decode()
    try:
        return cls.from_private_key(io.StringIO(plain))
    except (paramiko.SSHException, ValueError, TypeError, IndexError):
        raise KeyLoadError('The private key could not be loaded by the SSH client. ' + KEY_HELP,
                           'key_unsupported') from None


def key_connect_args(pkey, password=None):
    """Netmiko 4.7 arguments for an in-memory key.

    ``use_keys=True`` makes BaseConnection._key_check demand an existing
    ``key_file`` (ValueError for an in-memory key), and in Paramiko it only
    enables ``look_for_keys`` (keys under ~/.ssh). Paramiko tries ``pkey``
    whenever it is given, so the supported path is pkey with use_keys=False,
    no key_file and no agent. A saved password is still offered for devices
    that require key + password; otherwise no password attempt is made.
    """
    return dict(pkey=pkey, use_keys=False, key_file=None, allow_agent=False, password=password or None)


LEGACY_KEX = ('diffie-hellman-group1-sha1', 'diffie-hellman-group14-sha1',
              'diffie-hellman-group-exchange-sha1')


def ssh_algorithms(*, legacy_compat=False):
    """Algorithm policy for device SSH sessions. SHA1 key exchange and
    signatures need explicit opt-in on the connection profile. The explicit
    'kex' entry keeps Netmiko's Fortinet driver from replacing the policy."""
    return {'keys': [] if legacy_compat else ['ssh-rsa'],
            'pubkeys': [] if legacy_compat else ['ssh-rsa'],
            'kex': [] if legacy_compat else list(LEGACY_KEX)}


def presented_host_key(conn):
    """(key type, base64) of the host key the device presented, or None."""
    try:
        channel = getattr(conn, 'remote_conn', None)
        transport = channel.get_transport() if channel is not None else None
        if transport is None and getattr(conn, 'remote_conn_pre', None) is not None:
            transport = conn.remote_conn_pre.get_transport()
        key = transport.get_remote_server_key() if transport is not None else None
        return (key.get_name(), key.get_base64()) if key is not None else None
    except Exception:
        return None


def fetch_config(host, platform, username, password, enable, port, config_type='running', *, pkey=None,
                 legacy_compat=False, observed_key=None, capture_timeout=None, progress=None):
    """``observed_key``: optional dict filled with the presented host key
    (``type``/``base64``) so the caller can apply warn-only trust.
    ``capture_timeout``: explicit deadline (device/profile setting) or None
    for the resolved driver's default. ``progress``: CaptureProgress."""
    from netmiko import ConnectHandler, SSHDetect
    from paramiko import AuthenticationException
    if platform not in (None, '', 'autodetect') and platform not in DRIVERS:
        raise CaptureError('Unsupported device platform', 'driver')
    base = dict(host=host, username=username, password=password or '', port=port or 22,
                fast_cli=False, ssh_strict=False,
                # RSA SHA-1 ('ssh-rsa') stays disabled for key auth too unless
                # the profile opts into legacy compatibility.
                disabled_algorithms=ssh_algorithms(legacy_compat=legacy_compat), conn_timeout=20, timeout=60,
                allow_agent=False)
    if pkey is not None:
        base.update(key_connect_args(pkey, password))
    if enable:
        base['secret'] = enable
    if progress is not None:
        progress.update(phase='connecting')

    def connect(factory, **kwargs):
        try:
            return factory(**base, **kwargs)
        except AuthenticationException as exc:
            if pkey is None:
                raise
            from paramiko.ssh_exception import BadAuthenticationType
            # Netmiko re-raises Paramiko's error as NetmikoAuthenticationException.
            seen, cause = set(), exc
            while cause is not None and id(cause) not in seen and not isinstance(cause, BadAuthenticationType):
                seen.add(id(cause))
                cause = cause.__cause__ or cause.__context__
            offered = list(getattr(cause, 'allowed_types', None) or [])
            if cause is not None and 'publickey' not in offered:
                raise KeyRejectedError('The device does not accept SSH key login for this account '
                                       f'(it offers: {", ".join(offered) or "none"}); '
                                       'use a password profile or enable public-key authentication.') from exc
            raise KeyRejectedError("The device rejected the SSH key: install the profile's public key on the "
                                   'device account and confirm the key type is allowed (RSA keys need '
                                   'rsa-sha2-256/512).') from exc

    resolved = platform
    if platform in (None, '', 'autodetect'):
        detector = connect(SSHDetect, device_type='autodetect')
        try:
            resolved = detector.autodetect()
        finally:
            detector.connection.disconnect()
        resolved = DETECT_ALIASES.get(resolved, resolved)
    if resolved not in DRIVERS:
        raise CaptureError('Device detection failed or returned an unsupported platform', 'driver')
    command = DRIVERS[resolved].get('running' if config_type == 'running' else 'startup' if config_type == 'startup' else '')
    if not command:
        raise CaptureError('This driver does not support the requested configuration type', 'unsupported_type')
    if resolved == 'fortinet':
        # Netmiko's stock Fortinet preparation AND cleanup mutate console
        # configuration. Override both, rather than only our pager helper.
        from netmiko.fortinet.fortinet_ssh import FortinetSSH

        class ReadOnlyFortinet(FortinetSSH):
            def session_preparation(self):
                data = self._test_channel_read(pattern=f'to accept|{self.prompt_pattern}')
                if 'to accept' in data:
                    self.write_channel('a\r')
                    self._test_channel_read(pattern=self.prompt_pattern)
                self.set_base_prompt()

            def cleanup(self, command='exit'):
                # Base cleanup may call inherited configuration mode methods;
                # close the SSH transport without any configuration commands.
                self.write_channel(command + '\r')
        conn = connect(ReadOnlyFortinet, device_type=resolved)
    else:
        conn = connect(ConnectHandler, device_type=resolved)
    if observed_key is not None:
        presented = presented_host_key(conn)
        if presented:
            observed_key.update(type=presented[0], base64=presented[1])
    try:
        if enable:
            try:
                conn.enable()  # denied privilege is a failed capture, never ignored
            except Exception:
                raise CaptureError('Privilege elevation failed: verify the enable secret and command authorization; do not grant unrestricted write access', 'privilege') from None
        disable_paging(conn, resolved)
        deadline = capture_timeout if capture_timeout is not None else capture_deadline(resolved)[0]
        idle = DRIVERS[resolved].get('idle_timeout', DEFAULT_IDLE_TIMEOUT)
        if progress is not None:
            progress.update(platform=resolved, deadline_seconds=int(deadline))
        output = run_command(conn, command, conn.find_prompt(), timeout=deadline, idle_timeout=idle,
                             progress=progress)
        return resolved, validate_config(output, resolved)
    finally:
        conn.disconnect()
