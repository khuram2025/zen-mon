import subprocess
import types
import unittest
from unittest.mock import patch
from updater import os_patch_check as check


class PatchCheckTests(unittest.TestCase):
    def test_a_full_package_plan_is_required(self):
        rows = [{"name": "example", "required": "2"}]
        plan = "Inst example [1] (2 Ubuntu:noble-security [amd64])\nConf example (2)"
        for extra, allowed in [("", True), ("Remv extra [1]", False),
                               ("Inst extra (2)", False), ("Conf extra (2)", False)]:
            with self.subTest(extra=extra), patch.object(check.subprocess, "run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, plan+"\n"+extra, "")
                self.assertEqual(check.simulate(rows), allowed)
                self.assertIn("--simulate", run.call_args.args[0])
                self.assertNotIn("--no-download", run.call_args.args[0])

    def test_unsupported_platform_never_runs_apt(self):
        with patch.object(check.platform, "freedesktop_os_release", return_value={"ID": "debian"}):
            self.assertEqual(check.assess()["status"], "unsupported")

    def test_probe_failure_keeps_the_heartbeat_available(self):
        with patch.object(check.subprocess, "run", side_effect=OSError()):
            self.assertEqual(check.collect_os_patch_check()["status"], "unknown")

    def test_versions_above_floor_missing_targets_and_held_packages(self):
        origin = types.SimpleNamespace(trusted=True, origin="Ubuntu", archive="noble-security")
        version = types.SimpleNamespace(version="2", origins=[origin])
        pkg = types.SimpleNamespace(shortname="example", is_installed=True,
                                    installed=types.SimpleNamespace(version="3", architecture="amd64"),
                                    versions=[version], _pkg=types.SimpleNamespace(selected_state=0))
        class Cache(dict):
            broken_count = 0
            def __iter__(self):
                return iter([pkg])
        cache = Cache(dpkg=pkg)
        fake_apt = types.SimpleNamespace(Cache=lambda memonly: cache)
        fake_pkg = types.SimpleNamespace(SELSTATE_HOLD=2, version_compare=lambda a,b: int(a)-int(b))
        with patch.dict("sys.modules", {"apt":fake_apt, "apt_pkg":fake_pkg}), \
             patch.dict(check.TARGETS, {"example":"2", "absent":"1"}, clear=True), \
             patch.object(check.platform, "machine", return_value="x86_64"), \
             patch.object(check.platform, "freedesktop_os_release", return_value={
                 "ID":"ubuntu", "VERSION_CODENAME":"noble"}), patch.object(check,"simulate") as sim:
            self.assertEqual(check.assess()["status"], "meets_baseline")
            pkg.installed.version = "2"
            self.assertEqual(check.assess()["status"], "meets_baseline")
            pkg.installed.version = "1"
            pkg._pkg.selected_state = 2
            self.assertEqual(check.assess()["status"], "blocked")
            sim.assert_not_called()
            pkg._pkg.selected_state = 0
            origin.trusted = False
            self.assertEqual(check.assess()["status"], "blocked")
            origin.trusted = True
            sim.return_value = True
            self.assertEqual(check.assess()["status"], "updates_needed")
