import runpy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx


ROOT = Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts/reference-os-report-bridge.py"


@unittest.skipUnless(__import__("os").name == "posix", "Linux updater modules")
class ReferenceBridgeTests(unittest.TestCase):
    def test_verified_download_is_used_and_os_drift_blocks_reporting(self):
        from updater import agent, config, inventory
        hook = runpy.run_path(str(HOOK))["main"]
        cfg = SimpleNamespace(appliance=SimpleNamespace(id=hook.__globals__["REFERENCE"]))
        fetch = Mock(return_value=(Path("/fixture/artifact"), Path("/fixture/verified")))
        posted = []

        def handle(req):
            posted.append(req)
            return httpx.Response(200, json={"update_available": False})

        client = httpx.Client(transport=httpx.MockTransport(handle))
        with (patch.object(config, "load_config", return_value=cfg),
              patch.object(inventory, "get_current_version", return_value="1.25.1"),
              patch.object(agent, "_api_headers", return_value={}),
              patch("runpy.run_path", return_value={"verified_component": fetch}),
              patch("subprocess.check_output", return_value=b'{"status":"blocked","packages":[]}') as check,
              patch("httpx.Client", return_value=client),
              patch.dict(hook.__globals__, {"inventory": Mock(side_effect=[["one"], ["one"], ["one"], ["two"]])})):
            hook()
            with self.assertRaisesRegex(ValueError, "inventory changed"):
                hook()
            self.assertEqual(fetch.call_count, 2)
            for call in check.call_args_list:
                self.assertEqual(call.args[0], ["/usr/bin/python3", "-I", "-B",
                                                "/fixture/verified/os_patch_check.py"])
        self.assertEqual(len(posted), 1)
        self.assertEqual(str(posted[0].url), "https://zentryc.com/api/v1/appliances/checkin")
