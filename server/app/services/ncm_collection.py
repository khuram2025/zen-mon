"""Read-only CLI collection with explicit completion and format checks."""
from __future__ import annotations

import re
import time
from app.services.ncm_content import checked_content, MAX_CONFIG_BYTES

KNOWN_HOSTS = '/etc/zenplus/known_hosts'

PLATFORM_COMMANDS = {
    'cisco_ios': 'show running-config', 'cisco_xe': 'show running-config',
    'cisco_nxos': 'show running-config', 'cisco_asa': 'show running-config',
    'arista_eos': 'show running-config', 'juniper_junos': 'show configuration | display set',
    'paloalto_panos': 'show config running', 'fortinet': 'show full-configuration',
    'hp_comware': 'display current-configuration', 'huawei': 'display current-configuration',
}
STARTUP_COMMANDS = {
    'cisco_ios': 'show startup-config', 'cisco_xe': 'show startup-config',
    'cisco_nxos': 'show startup-config', 'cisco_asa': 'show startup-config',
    'arista_eos': 'show startup-config', 'hp_comware': 'display saved-configuration',
    'huawei': 'display saved-configuration',
}


class CaptureError(ValueError):
    """A safe-to-display collection error, never containing CLI output."""


def clean_cli(value: str) -> str:
    value = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', value).replace('\x08', '')
    value = re.sub(r'--More--[ \t]*\r?[ \t]*\r?|---\(more[^)]*\)---|<--- more --->', '', value, flags=re.I)
    value = value.replace('\r\n', '\n').replace('\r', '\n')
    return re.sub(r'[ \t]+\n', '\n', value).strip('\n') + '\n'


def is_paged(value: str) -> bool:
    return bool(re.search(r'--more--|---\(more|<--- more --->', value, re.I))


def run_command(conn, command: str, prompt: str, *, timeout: float = 240, idle_timeout: float = 20) -> str:
    prompt = (prompt or '').strip()
    if not prompt or '\n' in prompt or '\r' in prompt:
        raise CaptureError('Cannot establish a reliable device prompt')
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
                raise CaptureError('Configuration exceeds the 16 MiB limit')
        cleaned = clean_cli(output).rstrip()
        lines = cleaned.splitlines()
        # An exact final prompt line followed by a quiet interval, not a
        # substring in a banner, establishes command completion.
        if lines and lines[-1].strip() == prompt and time.monotonic() - last_data >= 1:
            lines.pop()
            if lines and lines[0].strip() in (command, prompt + command, prompt + ' ' + command):
                lines.pop(0)
            return checked_content('\n'.join(lines) + '\n')
        pagers = list(re.finditer(r'--more--|---\(more[^)]*\)---|<--- more --->', output, re.I))
        if pagers and pagers[-1].end() > pager_advanced_at:
            conn.write_channel(' ')
            pager_advanced_at = pagers[-1].end()
        if time.monotonic() - last_data >= idle_timeout:
            raise CaptureError('Incomplete capture: device prompt did not return')
        time.sleep(.2)
    raise CaptureError('Incomplete capture: collection deadline exceeded')


def validate_config(content: str, platform: str) -> str:
    content = checked_content(content)
    if platform not in PLATFORM_COMMANDS:
        raise CaptureError('Unsupported device platform; select a supported driver')
    if re.search(r'(?im)^\s*(?:%\s*(?:invalid|error|unknown|incomplete|ambiguous|access denied)|'
                 r'(?:error:|syntax error|permission denied|command not found|unknown command|'
                 r'invalid command|authorization failed|access denied|command fail))', content):
        raise CaptureError('Device rejected the configuration command')
    if is_paged(content):
        raise CaptureError('Incomplete capture: unresolved pager')
    markers = {
        'cisco_ios': r'(?m)^(?:hostname\s|version\s|interface\s)',
        'cisco_xe': r'(?m)^(?:hostname\s|version\s|interface\s)',
        'cisco_nxos': r'(?m)^(?:hostname\s|version\s|interface\s)',
        'cisco_asa': r'(?m)^(?:hostname\s|ASA Version\s|interface\s)',
        'arista_eos': r'(?m)^(?:hostname\s|! device:|interface\s)',
        'juniper_junos': r'(?m)^set\s+(?:system|interfaces|routing-options|security|protocols)\s',
        'paloalto_panos': r'(?m)^\s*(?:<config(?:\s|>)|config\s*\{|devices\s*\{)',
        'fortinet': r'(?m)^config\s+system\s',
        'hp_comware': r'(?m)^(?:sysname\s|version\s)',
        'huawei': r'(?m)^(?:sysname\s|!Software Version)',
    }
    if not re.search(markers[platform], content):
        raise CaptureError('Configuration does not match the selected device format')
    if platform in ('cisco_ios', 'cisco_xe', 'arista_eos') and not re.search(r'(?m)^end\s*$', content):
        raise CaptureError('Incomplete capture: missing configuration end marker')
    if platform in ('hp_comware','huawei') and not re.search(r'(?m)^return\s*$',content):
        raise CaptureError('Incomplete capture: missing configuration return marker')
    if platform=='paloalto_panos' and content.lstrip().startswith('<'):
        import xml.etree.ElementTree as ET
        try:
            ET.fromstring(content)
        except ET.ParseError:
            raise CaptureError('Incomplete or invalid XML configuration') from None
    if platform=='fortinet':
        opens=len(re.findall(r'(?m)^\s*config\s+',content))
        closes=len(re.findall(r'(?m)^\s*end\s*$',content))
        if opens!=closes:
            raise CaptureError('Incomplete configuration blocks')
    return content


def disable_paging(conn, platform: str) -> None:
    commands = {'cisco_ios':'terminal length 0','cisco_xe':'terminal length 0',
                'cisco_nxos':'terminal length 0','cisco_asa':'terminal pager 0',
                'arista_eos':'terminal length 0','paloalto_panos':'set cli pager off',
                'hp_comware':'screen-length disable','huawei':'screen-length 0 temporary',
                'juniper_junos':'set cli screen-length 0'}
    # FortiOS config system console changes persistent configuration. Walk its
    # pager instead. Failure of session-only preparation is safe: the reader
    # still requires a complete response and handles pagination.
    if platform in commands:
        try:
            conn.send_command_timing(commands[platform], read_timeout=15)
        except Exception:
            pass


def fetch_config(host, platform, username, password, enable, port, config_type='running'):
    from netmiko import ConnectHandler, SSHDetect
    if platform not in (None, '', 'autodetect') and platform not in PLATFORM_COMMANDS:
        raise CaptureError('Unsupported device platform')
    base = dict(host=host, username=username, password=password or '', port=port or 22,
                fast_cli=False, ssh_strict=True, disabled_algorithms={'keys':['ssh-rsa'],'pubkeys':['ssh-rsa']},
                alt_host_keys=True, alt_key_file=KNOWN_HOSTS, conn_timeout=20, timeout=60)
    if enable:
        base['secret'] = enable
    resolved = platform
    if platform in (None, '', 'autodetect'):
        detector = SSHDetect(**base, device_type='autodetect')
        try:
            resolved = detector.autodetect()
        finally:
            detector.connection.disconnect()
    if resolved not in PLATFORM_COMMANDS:
        raise CaptureError('Device detection failed or returned an unsupported platform')
    commands = PLATFORM_COMMANDS if config_type == 'running' else STARTUP_COMMANDS if config_type == 'startup' else {}
    if resolved not in commands:
        raise CaptureError('This driver does not support the requested configuration type')
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
        conn = ReadOnlyFortinet(**base, device_type=resolved)
    else:
        conn = ConnectHandler(**base, device_type=resolved)
    try:
        if enable:
            conn.enable()  # denied privilege is a failed capture, never ignored
        disable_paging(conn, resolved)
        output = run_command(conn, commands[resolved], conn.find_prompt())
        return resolved, validate_config(output, resolved)
    finally:
        conn.disconnect()
