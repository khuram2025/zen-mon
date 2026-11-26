"""Exercise the signed reporting overlay against a disposable appliance tree."""
import runpy
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
apply = runpy.run_path(str(ROOT / "scripts/os-patch-overlay.py"))["run"]


class ReportingOverlayTests(unittest.TestCase):
    def test_retry_restore_and_reject_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "installed"
            payload = Path(tmp) / "payload"
            (payload / "code/updater").mkdir(parents=True)
            (root / "updater").mkdir(parents=True)
            (root / ".version").write_text("1.25.30\n")
            agent = root / "updater/agent.py"
            probe = root / "updater/os_patch_check.py"
            agent.write_bytes(b"base")
            (payload / "original-agent.py").write_bytes(b"base")
            (payload / "code/updater/agent.py").write_bytes(b"patched")
            (payload / "code/updater/os_patch_check.py").write_bytes(b"probe")
            for action in ("check", "install", "install"):
                apply(action, root, payload)
            self.assertEqual(agent.read_bytes(), b"patched")
            self.assertEqual(probe.read_bytes(), b"probe")
            for action in ("restore", "restore"):
                apply(action, root, payload)
            self.assertEqual(agent.read_bytes(), b"base")
            self.assertFalse(probe.exists())
            agent.write_bytes(b"local change")
            with self.assertRaises(ValueError):
                apply("install", root, payload)
            self.assertFalse(probe.exists())
