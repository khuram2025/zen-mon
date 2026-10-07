"""`configure dns` and `configure ntp` must apply what the administrator typed,
to the right interface, and persist it."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
NTP_HELPER = ROOT / "bin" / "zenplus-apply-ntp.sh"


@pytest.fixture(scope="module")
def cli():
    loader = importlib.machinery.SourceFileLoader("zenplus_cli_under_test", str(ROOT / "bin" / "zenplus-cli"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def completed(returncode=0, stdout="", stderr=""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class Recorder:
    """Stands in for subprocess.run and records every command."""

    def __init__(self, route="default via 192.168.8.1 dev ens33 proto static\n", result=None):
        self.calls = []
        self.route = route
        self.result = result or completed()

    def __call__(self, cmd, **kwargs):
        self.calls.append((list(cmd), kwargs))
        if cmd[:2] == ["ip", "-o"]:
            return completed(stdout=self.route)
        return self.result

    def commands(self):
        return [c for c, _ in self.calls if c[:2] != ["ip", "-o"]]


# ── interface detection ──────────────────────────────────────────────────────

@pytest.mark.parametrize("route,expected", [
    ("default via 192.168.8.1 dev ens33 proto static\n", "ens33"),
    ("default via 10.0.0.1 dev eth0 proto dhcp src 10.0.0.5 metric 100\n", "eth0"),
    ("default dev wg0 scope link\n", "wg0"),
    ("", None),
    ("default via 10.0.0.1\n", None),
])
def test_default_route_interface_is_read_from_the_dev_field(cli, monkeypatch, route, expected):
    monkeypatch.setattr(cli.subprocess, "run", Recorder(route=route))
    assert cli._detect_iface() == expected


def test_no_default_route_means_no_guess(cli, monkeypatch):
    """The old code fell back to a hard-coded 'ens34'."""
    def boom(*a, **k):
        raise OSError("no ip command")
    monkeypatch.setattr(cli.subprocess, "run", boom)
    assert cli._detect_iface() is None


# ── DNS file rewriting ───────────────────────────────────────────────────────

def test_dns_lines_are_replaced_in_place(cli):
    original = "[Match]\nName=ens33\n\n[Network]\nAddress=10.0.0.5/24\nGateway=10.0.0.1\nDNS=8.8.8.8\nDNS=1.1.1.1\nLinkLocalAddressing=no\n"
    result = cli._with_dns(original, ["10.1.1.1", "10.1.1.2"])
    assert result.count("DNS=") == 2
    assert "DNS=10.1.1.1\nDNS=10.1.1.2\n" in result
    assert "8.8.8.8" not in result and "1.1.1.1" not in result.replace("10.1.1.1", "")
    assert "Address=10.0.0.5/24" in result and "LinkLocalAddressing=no" in result


def test_dns_is_added_when_the_file_has_none(cli):
    result = cli._with_dns("[Match]\nName=ens33\n\n[Network]\nDHCP=yes\n", ["10.1.1.1"])
    assert result.endswith("DNS=10.1.1.1\n") and "DHCP=yes" in result


def test_dns_goes_before_a_following_section(cli):
    result = cli._with_dns("[Network]\nDHCP=yes\n\n[DHCPv4]\nUseDNS=no\n", ["10.1.1.1"])
    assert result.index("DNS=10.1.1.1") < result.index("[DHCPv4]")
    assert "UseDNS=no" in result, "UseDNS= must not be mistaken for a DNS= line"


def test_a_file_without_a_network_section_gets_one(cli):
    result = cli._with_dns("[Match]\nName=ens33\n", ["10.1.1.1"])
    assert "[Network]\nDNS=10.1.1.1\n" in result


def test_dns_lines_with_odd_spacing_and_case_are_replaced(cli):
    result = cli._with_dns("[Network]\n dns = 8.8.8.8\nDNS=1.1.1.1\n", ["9.9.9.9"])
    assert result.count("DNS") == 1 and "9.9.9.9" in result and "8.8.8.8" not in result


# ── configure dns ────────────────────────────────────────────────────────────

def test_dns_is_applied_to_the_default_route_interface_not_link_1(cli, monkeypatch, tmp_path, capsys):
    rec = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", rec)
    monkeypatch.setattr(cli, "NETWORKD_DIR", tmp_path)  # no networkd file for ens33
    cli._cmd_configure_dns(["10.1.1.1", "10.1.1.2"])
    assert rec.commands() == [["sudo", "-n", "resolvectl", "dns", "ens33", "10.1.1.1", "10.1.1.2"]]
    out = capsys.readouterr().out
    assert "Not persistent" in out and "ens33" in out


def test_dns_is_persisted_in_the_networkd_file_when_there_is_one(cli, monkeypatch, tmp_path, capsys):
    (tmp_path / "10-ens33.network").write_text(
        "[Match]\nName=ens33\n\n[Network]\nAddress=10.0.0.5/24\nDNS=8.8.8.8\n")
    rec = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", rec)
    monkeypatch.setattr(cli, "NETWORKD_DIR", tmp_path)
    cli._cmd_configure_dns(["10.1.1.1"])
    (cmd, kwargs), = [c for c in rec.calls if c[0][:2] != ["ip", "-o"]]
    assert cmd == ["sudo", "-n", "/opt/zenplus/bin/zenplus-apply-network.sh", "ens33"]
    assert "DNS=10.1.1.1" in kwargs["input"] and "8.8.8.8" not in kwargs["input"]
    assert "Address=10.0.0.5/24" in kwargs["input"]
    assert "saved in" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["not-an-ip", "300.1.1.1", "1.2.3", "8.8.8.8; reboot", "$(id)"])
def test_invalid_dns_servers_are_rejected_before_anything_runs(cli, monkeypatch, capsys, bad):
    rec = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", rec)
    cli._cmd_configure_dns(["10.1.1.1", bad])
    assert rec.calls == []
    assert "Invalid DNS server" in capsys.readouterr().out


def test_ipv6_dns_servers_are_accepted(cli, monkeypatch, tmp_path):
    rec = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", rec)
    monkeypatch.setattr(cli, "NETWORKD_DIR", tmp_path)
    cli._cmd_configure_dns(["2001:4860:4860::8888"])
    assert rec.commands()[0][-1] == "2001:4860:4860::8888"


def test_dns_reports_a_failure_instead_of_claiming_success(cli, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(cli.subprocess, "run", Recorder(result=completed(1, stderr="sudo: a password is required")))
    monkeypatch.setattr(cli, "NETWORKD_DIR", tmp_path)
    cli._cmd_configure_dns(["10.1.1.1"])
    out = capsys.readouterr().out
    assert "Failed to apply" in out and "password is required" in out
    assert "set to" not in out


def test_dns_with_no_default_route_says_so(cli, monkeypatch, capsys):
    rec = Recorder(route="")
    monkeypatch.setattr(cli.subprocess, "run", rec)
    cli._cmd_configure_dns(["10.1.1.1"])
    assert rec.commands() == [] and "No interface" in capsys.readouterr().out


# ── configure ntp ────────────────────────────────────────────────────────────

def test_ntp_servers_are_passed_to_the_helper(cli, monkeypatch, capsys):
    rec = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", rec)
    cli._cmd_configure_ntp(["pool.ntp.org", "10.0.0.9"])
    assert rec.commands() == [["sudo", "-n", cli.APPLY_NTP_HELPER, "pool.ntp.org", "10.0.0.9"]]
    assert "NTP servers set to: pool.ntp.org 10.0.0.9" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["bad host", "a;b", "$(id)", "-rf", "x" * 254, ""])
def test_invalid_ntp_servers_never_reach_the_helper(cli, monkeypatch, capsys, bad):
    rec = Recorder()
    monkeypatch.setattr(cli.subprocess, "run", rec)
    cli._cmd_configure_ntp([bad])
    assert rec.calls == []


def test_ntp_failure_is_reported(cli, monkeypatch, capsys):
    monkeypatch.setattr(cli.subprocess, "run", Recorder(result=completed(2, stderr="Invalid NTP server: x")))
    cli._cmd_configure_ntp(["pool.ntp.org"])
    out = capsys.readouterr().out
    assert "Failed to apply" in out and "NTP servers set" not in out


# ── the NTP helper itself (as root it writes /etc; here into a sandbox) ──────

def run_helper(tmp_path, *args):
    stubs = tmp_path / "stubs"
    stubs.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    for name in ("timedatectl", "systemctl", "chown"):
        stub = stubs / name
        stub.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n')
        stub.chmod(0o755)
    env = {"PATH": f"{stubs}:{os.environ['PATH']}", "ZENPLUS_TIMESYNCD_DIR": str(tmp_path / "conf.d")}
    result = subprocess.run(["bash", str(NTP_HELPER), *args], env=env, capture_output=True, text=True)
    calls = log.read_text().splitlines() if log.exists() else []
    return result, calls


def test_helper_writes_a_persistent_dropin_and_restarts_time_sync(tmp_path):
    result, calls = run_helper(tmp_path, "pool.ntp.org", "10.0.0.9")
    assert result.returncode == 0, result.stderr
    conf = tmp_path / "conf.d" / "10-zenplus.conf"
    assert conf.read_text() == "[Time]\nNTP=pool.ntp.org 10.0.0.9\n"
    assert oct(conf.stat().st_mode & 0o777) == "0o644"
    assert "timedatectl set-ntp true" in calls and "systemctl restart systemd-timesyncd" in calls
    assert not [p for p in (tmp_path / "conf.d").iterdir() if p.name.startswith(".")], "temp file left behind"


def test_helper_replaces_the_previous_servers(tmp_path):
    run_helper(tmp_path, "old.example.com")
    run_helper(tmp_path, "new.example.com")
    assert (tmp_path / "conf.d" / "10-zenplus.conf").read_text() == "[Time]\nNTP=new.example.com\n"


@pytest.mark.parametrize("args", [
    [], ["a", "b", "c", "d", "e"], ["bad host"], ["x;reboot"], ["-flag"], ["a\nNTP=evil"], ["$(id)"], ["a.b."],
])
def test_helper_rejects_bad_input_and_changes_nothing(tmp_path, args):
    result, calls = run_helper(tmp_path, *args)
    assert result.returncode == 2
    assert calls == [] and not (tmp_path / "conf.d" / "10-zenplus.conf").exists()


# ── host validation shared by ping, traceroute and ntp ───────────────────────

@pytest.mark.parametrize("value", ["pool.ntp.org", "10.0.0.1", "host-1", "2001:db8::1", "::1", "fe80::1"])
def test_valid_hosts_are_accepted(cli, value):
    assert cli._validate_host(value)


@pytest.mark.parametrize("value", ["", "-f", "--help", "-rf", "a b", "a;b", "$(id)", "x" * 254, "a|b", "host\nname"])
def test_hosts_that_could_be_options_or_shell_syntax_are_rejected(cli, value):
    assert not cli._validate_host(value)
