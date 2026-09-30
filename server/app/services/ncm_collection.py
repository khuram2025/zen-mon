"""Read-only CLI collection with explicit completion and format checks."""
from __future__ import annotations

import base64
import hashlib
import os
import re
import tempfile
import time
from app.services.ncm_content import checked_content, MAX_CONFIG_BYTES

KNOWN_HOSTS = '/etc/zenplus/known_hosts'

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
    'fortinet': dict(label='Fortinet FortiOS', vendor='Fortinet', running='show full-configuration',
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
                     'scope': d['scope'], 'omissions': d['omissions'], 'artifact_format': 'cli_text'})
    return rows


class CaptureError(ValueError):
    """A safe-to-display collection error, never containing CLI output."""

    def __init__(self, message, category='incomplete'):
        super().__init__(message)
        self.category = category


def clean_cli(value: str) -> str:
    value = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', value).replace('\x08', '')
    value = re.sub(r'--More--[ \t]*\r?[ \t]*\r?|---\(more[^)]*\)---|<--- more --->|-- MORE --, next page: Space, next line: Enter, quit: Control-C', '', value, flags=re.I)
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'[ \t]+\n', '\n', value).strip('\n') + '\n'


_PAGER = r'--more--|---\(more[^)]*\)---|<--- more --->|-- MORE --'


def is_paged(value: str) -> bool:
    return bool(re.search(_PAGER, value, re.I))


def run_command(conn, command: str, prompt: str, *, timeout: float = 240, idle_timeout: float = 20) -> str:
    prompt = (prompt or '').strip()
    if not prompt or '\n' in prompt or '\r' in prompt:
        raise CaptureError('Cannot establish a reliable device prompt', 'driver')
    conn.clear_buffer()
    conn.write_channel(command + '\n')
    deadline = time.monotonic() + timeout
    last_data = time.monotonic()
    output = ''
    pager_advanced_at = -1
    while time.monotonic() < deadline:
        data = conn.read_channel()
        if data:
            output += data
            last_data = time.monotonic()
            if len(output.encode('utf-8')) > MAX_CONFIG_BYTES:
                raise CaptureError('Configuration exceeds the 16 MiB limit', 'format')
        cleaned = clean_cli(output).rstrip()
        lines = cleaned.splitlines()
        # An exact final prompt line followed by a quiet interval, not a
        # substring in a banner, establishes command completion.
        if lines and lines[-1].strip() == prompt and time.monotonic() - last_data >= 1:
            lines.pop()
            if lines and lines[0].strip() in (command, prompt + command, prompt + ' ' + command):
                lines.pop(0)
            return checked_content('\n'.join(lines) + '\n')
        pagers = list(re.finditer(_PAGER, output, re.I))
        if pagers and pagers[-1].end() > pager_advanced_at:
            conn.write_channel(' ')
            pager_advanced_at = pagers[-1].end()
        if time.monotonic() - last_data >= idle_timeout:
            raise CaptureError('Incomplete capture: device prompt did not return')
        time.sleep(.2)
    raise CaptureError('Incomplete capture: collection deadline exceeded')


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


def probe_host_key(host, port=22, timeout=10):
    """Read the SSH host key a device presents. No authentication is attempted
    and nothing is saved: the operator must confirm the fingerprint against an
    independent source before registering it."""
    import socket
    import paramiko
    sock = socket.create_connection((str(host), int(port or 22)), timeout=timeout)
    transport = paramiko.Transport(sock, disabled_algorithms={'keys': ['ssh-rsa']})
    try:
        transport.start_client(timeout=timeout)
        key = transport.get_remote_server_key()
        b64 = key.get_base64()
        return {'key_type': key.get_name(), 'key_base64': b64, 'fingerprint_sha256': key_fingerprint(key.get_name(), b64),
                'fingerprint_md5': ':'.join(f'{b:02x}' for b in key.get_fingerprint()), 'bits': key.get_bits()}
    finally:
        transport.close()


def known_hosts_file(entries):
    """Write a private known_hosts combining the appliance trust file and the
    operator-managed entries for this connection. Returns the path; the caller
    removes it. Strict checking still happens in paramiko."""
    fd, path = tempfile.mkstemp(prefix='zenplus-ncm-kh-')
    with os.fdopen(fd, 'w') as out:
        try:
            with open(KNOWN_HOSTS) as system:
                out.write(system.read().rstrip('\n') + '\n')
        except OSError:
            pass
        for host, port, key_type, key_b64 in entries:
            name = str(host) if int(port or 22) == 22 else f'[{host}]:{int(port)}'
            out.write(f'{name} {key_type} {key_b64}\n')
    return path


def load_private_key(text, passphrase=None):
    import io
    import paramiko
    for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
        try:
            return cls.from_private_key(io.StringIO(text), password=passphrase or None)
        except paramiko.PasswordRequiredException:
            raise ValueError('The private key is encrypted; supply its passphrase') from None
        except (paramiko.SSHException, ValueError, IndexError, TypeError):
            continue
    raise ValueError('Unsupported or invalid private key (use an OpenSSH/PEM Ed25519, ECDSA or RSA key)')


def fetch_config(host, platform, username, password, enable, port, config_type='running', *, pkey=None, known_hosts=None):
    from netmiko import ConnectHandler, SSHDetect
    if platform not in (None, '', 'autodetect') and platform not in DRIVERS:
        raise CaptureError('Unsupported device platform', 'driver')
    base = dict(host=host, username=username, password=password or '', port=port or 22,
                fast_cli=False, ssh_strict=True, disabled_algorithms={'keys': ['ssh-rsa'], 'pubkeys': ['ssh-rsa']},
                alt_host_keys=True, alt_key_file=known_hosts or KNOWN_HOSTS, conn_timeout=20, timeout=60,
                allow_agent=False)
    if pkey is not None:
        base.update(use_keys=True, pkey=pkey)
    if enable:
        base['secret'] = enable
    resolved = platform
    if platform in (None, '', 'autodetect'):
        detector = SSHDetect(**base, device_type='autodetect')
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
        # FortinetSSH replaces disabled_algorithms wholesale when no 'kex' key
        # is given, silently dropping the SHA-1 host-key restriction. Supply
        # its FortiOS key-exchange policy ourselves so both survive.
        import paramiko
        base['disabled_algorithms'] = {**base['disabled_algorithms'],
                                       'kex': sorted(set(paramiko.Transport._preferred_kex) - FortinetSSH.preferred_kex)}
        conn = ReadOnlyFortinet(**base, device_type=resolved)
    else:
        conn = ConnectHandler(**base, device_type=resolved)
    try:
        if enable:
            try:
                conn.enable()  # denied privilege is a failed capture, never ignored
            except Exception:
                raise CaptureError('Privilege elevation failed: verify the enable secret and command authorization; do not grant unrestricted write access', 'privilege') from None
        disable_paging(conn, resolved)
        output = run_command(conn, command, conn.find_prompt())
        return resolved, validate_config(output, resolved)
    finally:
        conn.disconnect()
