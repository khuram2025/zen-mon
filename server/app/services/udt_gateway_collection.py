"""Bounded, read-only gateway collection and strict vendor response parsers.

SNMP remains the primary cross-vendor transport. CLI only supplements tables
which the gateway does not expose, using its existing enabled NCCM profile.
No configuration, ARP clear, MAC clear, privilege elevation or API write is sent.
"""

from __future__ import annotations

import ipaddress
import re
import time

from app.services.ncm_collection import (
    CaptureError,
    run_command,
    clean_cli,
    is_paged,
    ssh_algorithms,
    load_private_key,
)
from app.core.crypto import decrypt

SWITCH_ROOT = "1.3.6.1.4.1.12356.101.24.1.1.1"
PORT_ROOT = "1.3.6.1.4.1.12356.101.24.2.1.1"
ARP_COMMANDS = {
    "fortinet": "get system arp",
    "paloalto_panos": "show arp all",
    "cisco_ios": "show ip arp",
    "cisco_xe": "show ip arp",
    "cisco_nxos": "show ip arp",
    "cisco_asa": "show arp",
    "arista_eos": "show ip arp",
    "juniper_junos": "show arp no-resolve",
    "aruba_aoscx": "show arp",
    "aruba_osswitch": "show arp",
    "aruba_os": "show arp",
    "huawei": "display arp",
    "hp_comware": "display arp",
}
MAC_PATTERN = (
    r"(?:[0-9a-fA-F]{2}[:-]){5}[0-9a-fA-F]{2}|(?:[0-9a-fA-F]{4}[.\-]){2}[0-9a-fA-F]{4}"
)


def canonical_mac(raw: str) -> str | None:
    value = re.sub(r"[:.\-]", "", raw)
    if not re.fullmatch(r"[0-9a-fA-F]{12}", value):
        return None
    octets = bytes.fromhex(value)
    if not any(octets) or octets[0] & 1:
        return None
    return ":".join(f"{b:02x}" for b in octets)


def snmp_cells(lines: list[str], root: str) -> dict[str, str]:
    out = {}
    for line in lines:
        match = re.match(
            r"^\.?" + re.escape(root) + r"\.([0-9.]+)\s+=\s+[^:]+:\s*(.*)$", line
        )
        if not match:
            continue
        value = match[2].strip().strip('"')
        if "\x00" not in value:
            out[match[1]] = value[:255]
    return out


def fortinet_switches(columns: dict[str, dict[str, str]]) -> list[dict]:
    out = []
    for instance, serial in columns.get("serial", {}).items():
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", serial):
            continue
        out.append(
            {
                "instance": instance,
                "serial": serial,
                "hostname": columns.get("name", {}).get(instance) or serial,
                "status": {"1": "up", "0": "down"}.get(
                    columns.get("status", {}).get(instance), "unknown"
                ),
            }
        )
    return out


def fortinet_ports(
    columns: dict[str, dict[str, str]], switches: list[dict]
) -> list[dict]:
    by_instance = {s["instance"]: s["serial"] for s in switches}
    out = []
    for index, name in columns.get("name", {}).items():
        instance, sep, port = index.rpartition(".")
        if (
            not sep
            or instance not in by_instance
            or not port.isdigit()
            or not 0 < int(port) <= 65535
        ):
            continue
        pvid = columns.get("pvid", {}).get(index, "")
        out.append(
            {
                "serial": by_instance[instance],
                "port_number": int(port),
                "if_name": name,
                "oper_status": {"0": "down", "1": "up"}.get(
                    columns.get("status", {}).get(index), "unknown"
                ),
                "pvid": (
                    int(pvid) if pvid.isdigit() and 1 <= int(pvid) <= 4094 else None
                ),
            }
        )
    return out


def switch_aliases(switches: list[dict]) -> dict[str, str]:
    aliases = {s["serial"]: s["serial"] for s in switches}
    for switch in switches:
        name = switch["hostname"]
        if sum(s["hostname"] == name for s in switches) == 1 and name not in aliases:
            aliases[name] = switch["serial"]
    return aliases


def fortinet_mac_cache(output: str, switches=()) -> list[dict]:
    """Only numeric port+VLAN observations under an explicit switch section.

    Port 0 is the switch's own MAC, not an endpoint attachment. Unknown output
    formats fail closed rather than attributing a MAC to a guessed port.
    """
    aliases = switch_aliases(list(switches))
    out = []
    serial = None
    seen = set()
    for line in output.splitlines():
        section = re.search(r"^\s*managed-switch:\s*([A-Za-z0-9_-]+)", line)
        if section:
            serial = aliases.get(section[1]) if switches else section[1]
            continue
        row = re.match(r"^\s*(\d+)\s+(\d+)\s+(" + MAC_PATTERN + r")(?:\s|$)", line)
        if not row or not serial:
            continue
        tail = line[row.end() :].split()
        # The cache includes age and interface name on current FortiOS.
        if tail and tail[0].isdigit() and int(tail[0]) > 900:
            continue
        interface = tail[1] if len(tail) > 1 else None
        vlan, port = int(row[1]), int(row[2])
        mac = canonical_mac(row[3])
        key = (serial, port, vlan, mac)
        if not mac or port <= 0 or not 1 <= vlan <= 4094 or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "serial": serial,
                "port_number": port,
                "vlan_id": vlan,
                "mac": mac,
                "is_direct": False,
                **({"if_name": interface} if interface else {}),
            }
        )
    return out


def fortinet_mac_table(output: str, switches: list[dict]) -> list[dict]:
    aliases = switch_aliases(switches)
    out = []
    serial = None
    seen = set()
    for line in output.splitlines():
        section = re.match(r"^\s*Managed Switch\s*:\s*(\S+)", line, re.I)
        if section:
            serial = aliases.get(section[1])
            continue
        row = re.search(
            r"MAC:\s*("
            + MAC_PATTERN
            + r")\s+VLAN:\s*(\d+)\s+Port:\s*(\S+)\(port-id\s+(\d+)\)",
            line,
            re.I,
        )
        if not row or not serial or row[3].lower() == "internal":
            continue
        mac = canonical_mac(row[1])
        vlan = int(row[2])
        port = int(row[4])
        key = (serial, port, vlan, mac)
        if not mac or port <= 0 or not 1 <= vlan <= 4094 or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "serial": serial,
                "port_number": port,
                "if_name": row[3],
                "vlan_id": vlan,
                "mac": mac,
                "is_direct": False,
            }
        )
    return out


def parse_arp(output: str, platform: str) -> list[dict]:
    """Canonical IP/MAC with the reporter's explicit local interface name."""
    out = []
    seen = set()
    for line in output.splitlines():
        if re.search(r"\bincomplete\b|\binvalid\b|\bfailed\b", line, re.I):
            continue
        match = re.search(r"(?<![\w:.-])(" + MAC_PATTERN + r")(?![\w:.-])", line)
        if not match:
            continue
        mac = canonical_mac(match[1])
        tokens = line.split()
        ip = None
        for token in tokens:
            try:
                addr = ipaddress.ip_address(token)
            except ValueError:
                continue
            if not (addr.is_unspecified or addr.is_multicast or addr.is_loopback):
                ip = str(addr)
                break
        if not mac or not ip:
            continue
        if platform == "paloalto_panos":
            interface = tokens[0]
        elif platform == "juniper_junos":
            index = next((i for i, t in enumerate(tokens) if t == ip), -1)
            interface = (
                tokens[index + 1] if index >= 0 and index + 1 < len(tokens) else ""
            )
        elif platform == "cisco_asa":
            interface = tokens[0]
        elif platform == "aruba_aoscx":
            tail = line[match.end() :].split()
            interface = tail[0] if tail else ""
        elif platform == "hp_comware":
            tail = line[match.end() :].split()
            interface = tail[1] if len(tail) > 1 else ""
        elif platform == "huawei":
            row = re.search(
                r"\b[DS][A-Z0-9-]*\s+([A-Za-z][A-Za-z0-9/_.:-]*)(?:\s+([0-9]+/[0-9]+(?:/[0-9]+)*))?",
                line[match.end() :],
            )
            interface = (row[1] + (row[2] or "")) if row else ""
        elif platform in ("cisco_nxos", "arista_eos"):
            # Arista includes both the routed and physical interface; use the
            # routed interface for reporter-local ARP evidence. NX-OS may append flags.
            after_mac = line[match.end() :].split()
            interface = after_mac[0].rstrip(",") if after_mac else ""
        else:
            interface = tokens[-1]
        if platform == "juniper_junos":
            interface = interface.split("[", 1)[0]
        if not re.fullmatch(
            r"(?:[A-Za-z][A-Za-z0-9/_.:-]{0,127}|[0-9]{1,5}(?:/[0-9]{1,5}){0,3})",
            interface,
        ):
            continue
        key = (ip, mac, interface)
        if key in seen:
            continue
        seen.add(key)
        out.append({"ip": ip, "mac": mac, "interface": interface})
    return out


def managed_mac_snapshot(read, switches: list[dict], prompt: str) -> list[dict]:
    """Select only VDOMs observed in SNMP; no edit/set or console changes."""
    contexts = sorted({s.get("vdom") for s in switches if s.get("vdom")})
    original_prompt = prompt
    if contexts:
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,79}", name) for name in contexts):
            raise CaptureError("Invalid observed gateway context", "format")
        base = re.sub(r"\s*(?:\([^)]*\))?\s*[#>]\s*$", "", prompt)
        prompt = base + " (global) #"
        read("config global", prompt)
        # switch-info/mac-cache return explicit VDOM sections on FortiOS.
        # Enter the observed controller VDOM, never assume the account's default.
        read("execute enter " + contexts[0], prompt)
    try:
        try:
            raw = read("diagnose switch-controller switch-info mac-table")
            if not re.search(r"Managed Switch\s*:", raw, re.I):
                raise CaptureError("Unrecognized managed MAC-table response", "format")
            rows = fortinet_mac_table(raw, switches)
        except CaptureError:
            raw = read("diagnose switch-controller mac-cache show")
            if not re.search(r"managed-switch:", raw, re.I):
                raise CaptureError("Unrecognized managed MAC-cache response", "format")
            rows = fortinet_mac_cache(raw, switches)
        return rows
    finally:
        if contexts:
            read("end", original_prompt)


def cli_snapshot(
    profile: dict, *, managed: bool, arp: bool, known_keys=(), switches=()
) -> dict:
    """Fixed show commands; credentials stay in memory, output is never logged."""
    import paramiko

    platform = profile["platform"]
    if platform not in ARP_COMMANDS:
        raise CaptureError("No supplemental UDT collector for this platform", "driver")

    class ExistingProfileTrust(paramiko.MissingHostKeyPolicy):
        def missing_host_key(self, client, hostname, key):
            # Match existing NCCM profile policy; honor enrolled keys when available.
            if known_keys and (key.get_name(), key.get_base64()) not in known_keys:
                raise CaptureError(
                    "Gateway SSH host key does not match its enrolled key",
                    "authentication",
                )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(ExistingProfileTrust())
    result = {"macs": [], "arp": []}
    try:
        auth = {}
        if profile.get("auth_method") == "key":
            auth["pkey"] = load_private_key(
                decrypt(profile["private_key_enc"]),
                decrypt(profile.get("key_passphrase_enc")),
            )
        else:
            auth["password"] = decrypt(profile.get("password_enc")) or ""
        client.connect(
            profile["ip"],
            port=profile.get("port") or 22,
            username=profile["username"],
            look_for_keys=False,
            allow_agent=False,
            timeout=10,
            auth_timeout=15,
            banner_timeout=15,
            disabled_algorithms=ssh_algorithms(
                legacy_compat=profile.get("ssh_legacy_compat", False)
            ),
            **auth,
        )
        channel = client.invoke_shell(width=200, height=1000)
        banner = ""
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if channel.recv_ready():
                banner += channel.recv(65535).decode(errors="replace")
            if banner.rstrip().endswith(("#", ">")):
                break
            time.sleep(0.1)
        if not banner.rstrip().endswith(("#", ">")):
            raise CaptureError("Cannot establish gateway prompt", "driver")
        prompt = banner.strip().splitlines()[-1]

        class Adapter:
            def clear_buffer(self):
                while channel.recv_ready():
                    channel.recv(65535)

            def write_channel(self, value):
                channel.sendall(value.encode())

            def read_channel(self):
                return (
                    channel.recv(65535).decode(errors="replace")
                    if channel.recv_ready()
                    else ""
                )

        conn = Adapter()

        def read(command, expected_prompt=None):
            nonlocal prompt
            # Context selection can change the prompt. Accept only the exact
            # original/expected prompt, including a denied context command.
            if expected_prompt is None or expected_prompt == prompt:
                value = run_command(conn, command, prompt, timeout=60)
            else:
                conn.clear_buffer()
                conn.write_channel(command + "\n")
                deadline = time.monotonic() + 20
                last_data = time.monotonic()
                value = ""
                pager_at = -1
                while time.monotonic() < deadline:
                    data = conn.read_channel()
                    if data:
                        value += data
                        last_data = time.monotonic()
                    if len(value) > 16 * 1024 * 1024:
                        raise CaptureError(
                            "Gateway response exceeds collection limit", "format"
                        )
                    lines = clean_cli(value).rstrip().splitlines()
                    if (
                        lines
                        and lines[-1].strip() in (prompt, expected_prompt)
                        and time.monotonic() - last_data >= 1
                    ):
                        prompt = lines[-1].strip()
                        value = "\n".join(lines[:-1])
                        break
                    pages = list(
                        re.finditer(
                            r"--more--|---\(more[^)]*\)---|<--- more --->|-- MORE --",
                            value,
                            re.I,
                        )
                    )
                    if pages and pages[-1].end() > pager_at:
                        conn.write_channel(" ")
                        pager_at = pages[-1].end()
                    time.sleep(0.1)
                else:
                    raise CaptureError(
                        "Gateway context prompt did not return", "driver"
                    )
                if is_paged(value):
                    value = clean_cli(value)
            if re.search(
                r"command fail|permission denied|not permitted|unknown action|unknown command|invalid input|syntax error|entry is not found|authorization failed|access denied|command not found|%\s*error",
                value,
                re.I,
            ):
                raise CaptureError(
                    "Gateway denied the required read-only UDT command", "privilege"
                )
            return value

        if managed and platform == "fortinet":
            result["macs"] = managed_mac_snapshot(read, list(switches), prompt)
        if arp:
            result["arp"] = parse_arp(read(ARP_COMMANDS[platform]), platform)
        return result
    except CaptureError:
        raise
    except Exception:
        raise CaptureError(
            "Gateway UDT connection failed; check its enabled NCCM profile",
            "collection",
        ) from None
    finally:
        client.close()
