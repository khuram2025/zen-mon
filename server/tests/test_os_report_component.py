import hashlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import os_report_component as component


class ReportingComponentTests(unittest.TestCase):
    def test_reject_firmware_manifest_or_unknown_base(self):
        manifest = dict(release_profile=component.PROFILE, version=component.VERSION,
                        supported_firmware=[component.BASE],
                        source={"tag": component.TAG, "dirty": False, "commit": "test"},
                        payload_files={**component.FILES, "os_report_component.py":
                                       component.digest(Path(component.__file__).read_bytes())})
        component.validate(manifest)
        for overrides in ({"supported_firmware": ["1.25.2"]}, {"steps": []},
                          {"min_version": component.BASE}, {"payload_files": {}}):
            with self.assertRaises(ValueError):
                component.validate({**manifest, **overrides})

    @unittest.skipUnless(os.name == "posix", "Linux appliance filesystem")
    def test_retry_restore_and_unknown_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp, "appliance")
            updater = root / "updater"
            updater.mkdir(parents=True)
            version = root / ".version"
            version.write_text(component.BASE + "\n")
            payload = Path(tmp, "signed")
            payload.mkdir()
            for name, data in (("original-agent.py", b"original"), ("agent.py", b"updated"),
                               ("os_patch_check.py", b"collector")):
                (payload / name).write_bytes(data)
            agent = updater / "agent.py"
            agent.write_bytes(b"original")
            with (patch("updater.trust.require_root_owned"),
                  patch.multiple(component, ORIGINAL=hashlib.sha256(b"original").hexdigest(),
                                 UPDATED=hashlib.sha256(b"updated").hexdigest(),
                                 COLLECTOR=hashlib.sha256(b"collector").hexdigest())):
                for action in ("install", "install"):
                    component.apply(root, payload, action)
                    self.assertEqual(agent.read_bytes(), b"updated")
                for action in ("restore", "restore"):
                    component.apply(root, payload, action)
                    self.assertEqual(agent.read_bytes(), b"original")
                    self.assertFalse((updater / "os_patch_check.py").exists())
                agent.write_bytes(b"custom")
                with self.assertRaises(ValueError):
                    component.apply(root, payload, "install")
                self.assertFalse((updater / "os_patch_check.py").exists())
                version.write_text("1.25.2\n")
                agent.write_bytes(b"original")
                with self.assertRaises(ValueError):
                    component.apply(root, payload, "install")
            self.assertEqual(version.read_text(), "1.25.2\n")
