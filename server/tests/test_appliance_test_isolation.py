"""Collecting the suite must not discover and mutate the installed appliance."""
from pathlib import Path
import runpy
import pytest
import requests


@pytest.mark.parametrize("module", ["test_netflow_api.py", "test_server_monitoring_api.py", "test_link_utilization_api.py"])
def test_live_smoke_collection_requires_an_explicit_target(monkeypatch, module):
    monkeypatch.delenv("ZENPLUS_API", raising=False)
    def unexpected_network(*args, **kwargs):
        pytest.fail("test collection attempted to contact a live appliance")
    monkeypatch.setattr(requests, "get", unexpected_network)
    namespace = runpy.run_path(str(Path(__file__).with_name(module)))
    assert namespace["API"] == ""
    assert namespace["_api_up"]() is False
