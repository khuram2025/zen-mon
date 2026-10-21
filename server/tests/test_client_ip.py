"""Client addresses come from the proxy-resolved peer, never a client-supplied
X-Forwarded-For hop (VAPT ZP-12)."""
from starlette.requests import Request

from app.api.v1 import agents, apm, rum
from app.core.client_ip import client_ip


def _request(peer="198.51.100.7", forwarded="203.0.113.66, 198.51.100.7"):
    headers = [(b"x-forwarded-for", forwarded.encode()), (b"x-real-ip", b"203.0.113.66")]
    return Request({"type": "http", "headers": headers, "client": (peer, 40000)})


def test_forged_forwarded_hop_is_ignored():
    request = _request()
    assert client_ip(request) == "198.51.100.7"
    assert agents._client_ip(request) == "198.51.100.7"
    assert apm._client_ip(request) == "198.51.100.7"
    assert rum._client_ip(request) == "198.51.100.7"


def test_missing_peer():
    request = Request({"type": "http", "headers": [], "client": None})
    assert client_ip(request) is None
    assert rum._client_ip(request) == ""
