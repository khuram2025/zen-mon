import hashlib
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import httpx

from integrations.zentryc_ota import report_component as portal
from updater import report_component as receiver


class ReportingDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.appliance = SimpleNamespace(id="reference", is_active=True, arch="amd64", current_version="1.25.1",
                                         subscription=SimpleNamespace(subscription_type="ota", is_active=True,
                                                                      is_expired=False))

    def test_enrollment_and_firmware_are_both_required(self):
        def allowed(target="reference", version="1.25.1"):
            return portal.eligible(self.appliance, version, target)
        self.assertTrue(allowed())
        self.assertFalse(allowed(None))
        self.assertFalse(allowed("other"))
        self.assertFalse(allowed(version="1.25.30"))
        for attribute, value in [("current_version", "1.25.30"), ("arch", "arm64"),
                                 ("is_active", False), ("subscription", None)]:
            with patch.object(self.appliance, attribute, value):
                self.assertFalse(allowed())
        for attribute, value in [("subscription_type", "zenai"), ("is_active", False), ("is_expired", True)]:
            with patch.object(self.appliance.subscription, attribute, value):
                self.assertFalse(allowed())

    def test_bridge_is_private_even_if_a_policy_is_broadened(self):
        a = self.appliance
        a.id, a.rollout_group = portal.REFERENCE_ID, portal.PILOT_GROUP
        policy = SimpleNamespace(stage="canary", auto_promote=False, completed_at=None,
                                 target_group=portal.PILOT_GROUP, target_pct=100)
        self.assertTrue(portal.bridge_allowed(a, policy, a.id))
        self.assertFalse(portal.bridge_allowed(a, policy, a.id, "1.25.9002"))
        with patch.object(a, "current_version", "1.25.9001"):
            self.assertTrue(portal.bridge_allowed(a, policy, a.id, "1.25.9002"))
            self.assertFalse(portal.bridge_allowed(a, policy, a.id))
            with patch.object(a, "id", "another"):
                self.assertFalse(portal.bridge_allowed(a, policy, portal.REFERENCE_ID, "1.25.9002"))
        for key, value in (("stage", "full"), ("auto_promote", True), ("target_group", "")):
            with patch.object(policy, key, value):
                self.assertFalse(portal.bridge_allowed(a, policy, a.id))
        with patch.object(a, "id", "other"):
            self.assertFalse(portal.bridge_allowed(a, policy, portal.REFERENCE_ID))

    def test_qualified_download_and_changed_artifact(self):
        body = b"reporter fixture"
        digest = hashlib.sha256(body).hexdigest()
        with tempfile.TemporaryDirectory() as tmp, patch.object(portal, "DIGEST", digest):
            artifact = Path(tmp) / portal.FILENAME
            artifact.write_bytes(body)

            def request(req):
                self.assertEqual(str(req.url), receiver.ENDPOINT)
                self.assertEqual(req.headers["X-ZenPlus-Firmware"], "1.25.1")
                return httpx.Response(200, content=portal.read_artifact(tmp))

            with httpx.Client(transport=httpx.MockTransport(request)) as client:
                with patch.object(receiver, "DIGEST", digest):
                    self.assertEqual(receiver.fetch(client, {}, "1.25.1"), body)
            artifact.write_bytes(b"replaced")
            with self.assertRaises(ValueError):
                portal.read_artifact(tmp)

    def test_empty_offer_and_unexpected_downloads(self):
        for status, body, error in [(204, b"", None), (200, b"untrusted", ValueError),
                                    (200, b"x" * (receiver.MAX_BYTES + 1), ValueError),
                                    (302, b"", httpx.HTTPStatusError), (403, b"", httpx.HTTPStatusError)]:
            with self.subTest(status=status), httpx.Client(transport=httpx.MockTransport(
                    lambda req: httpx.Response(status, content=body, headers={"Location": "https://invalid.test/"}))) as client:
                if error:
                    with self.assertRaises(error):
                        receiver.fetch(client, {}, "1.25.1")
                else:
                    self.assertIsNone(receiver.fetch(client, {}, "1.25.1"))
