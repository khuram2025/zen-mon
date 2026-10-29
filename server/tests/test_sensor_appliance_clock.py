"""Appliance boot must select a supported host clock or fail closed."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def clock_entry(monkeypatch):
    helper = Mock()
    monkeypatch.setitem(sys.modules, "appliance_clock", SimpleNamespace(initialize_clock=helper))
    path = Path(__file__).resolve().parents[2] / "sensor-appliance/scripts/initialize-clock.py"
    spec = importlib.util.spec_from_file_location("sensor_clock_entry", path)
    entry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(entry)
    return entry, helper


@pytest.mark.parametrize("detected,profile", [
    ("vmware", "vmware"), ("kvm", "kvm"), ("qemu", "kvm"), ("microsoft", "hyperv"),
])
def test_appliance_clock_selects_host_time_source(clock_entry, monkeypatch, detected, profile):
    entry, helper = clock_entry
    monkeypatch.setattr(entry.subprocess, "run", Mock(return_value=SimpleNamespace(stdout=detected + "\n")))
    entry.main()
    helper.assert_called_once_with(profile)


def test_unknown_hypervisor_does_not_use_an_unrelated_clock(clock_entry, monkeypatch):
    entry, helper = clock_entry
    monkeypatch.setattr(entry.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="other\n")))
    with pytest.raises(RuntimeError, match="Unsupported"):
        entry.main()
    helper.assert_not_called()


def test_failed_hypervisor_detection_stops_boot(clock_entry, monkeypatch):
    entry, helper = clock_entry
    monkeypatch.setattr(entry.subprocess, "run", Mock(side_effect=subprocess.CalledProcessError(1, "detect")))
    with pytest.raises(subprocess.CalledProcessError):
        entry.main()
    helper.assert_not_called()


def test_host_clock_failure_propagates_to_systemd(clock_entry, monkeypatch):
    entry, helper = clock_entry
    monkeypatch.setattr(entry.subprocess, "run", Mock(return_value=SimpleNamespace(stdout="vmware\n")))
    helper.side_effect = RuntimeError("host time unavailable")
    with pytest.raises(RuntimeError, match="unavailable"):
        entry.main()
