"""Use the poller's verifier for interactive HTTP/TLS checks as well.

Each origin is verified first, then the actual HTTP connection is pinned to
that verified leaf. No HTTP credentials are passed to the helper. Contexts
last only for one workflow, so removal of trust takes effect on the next test.
"""
import asyncio
import json
import os
import ssl
import subprocess
from urllib.parse import urlsplit

import httpx
from requests.adapters import HTTPAdapter
from requests.exceptions import SSLError as RequestSSLError


def verified_context(host, port, timeout, policy):
    binary = os.environ.get("ZENPLUS_PROBE_TLS_BINARY", "/opt/zenplus/bin/zenplus-poller")
    try:
        proc = subprocess.run([binary, "--verify-service-tls"], input=json.dumps({"host": host, "port": port, "timeout": timeout, "policy": policy}), capture_output=True, text=True, timeout=timeout + 1, check=False)
    except subprocess.TimeoutExpired:
        raise ssl.SSLError("TLS verification timed out") from None
    except OSError:
        raise ssl.SSLError("The service probe TLS verifier is unavailable; complete the appliance update") from None
    if proc.returncode:
        raise ssl.SSLError(proc.stderr.strip()[:2048] or "TLS certificate verification failed")
    try:
        certificate = json.loads(proc.stdout)["pem"]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        # This is a pin to a leaf already verified by the Go helper, not an
        # intermediate downloaded from an untrusted URL.
        ctx.load_verify_locations(cadata=certificate)
        ctx.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        return ctx
    except (ValueError, KeyError):
        raise ssl.SSLError("Invalid response from the service probe TLS verifier") from None


class ProbeTrustTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy, timeout):
        self.policy, self.timeout = policy, timeout
        self.transports = {}

    async def handle_async_request(self, request):
        origin = (request.url.scheme, request.url.host, request.url.port)
        if origin not in self.transports:
            verify = True
            if request.url.scheme == "https":
                try:
                    verify = await asyncio.to_thread(verified_context, request.url.host, request.url.port or 443, self.timeout, self.policy)
                except ssl.SSLError as exc:
                    raise httpx.ConnectError(str(exc), request=request) from exc
            self.transports[origin] = httpx.AsyncHTTPTransport(verify=verify)
        return await self.transports[origin].handle_async_request(request)

    async def aclose(self):
        for transport in self.transports.values():
            await transport.aclose()


class ProbeTrustAdapter(HTTPAdapter):
    """Requests/NTLM equivalent, with a separate verified pin for every origin."""
    def __init__(self, policy, timeout):
        self.policy, self.timeout = policy, timeout
        self.contexts = {}
        super().__init__()

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        parsed = urlsplit(request.url)
        origin = (parsed.hostname, parsed.port or 443)
        if origin not in self.contexts:
            try:
                self.contexts[origin] = verified_context(*origin, self.timeout, self.policy)
            except ssl.SSLError as exc:
                raise RequestSSLError(str(exc), request=request) from exc
        pool_kwargs["ssl_context"] = self.contexts[origin]
        pool_kwargs.pop("ca_certs", None)
        pool_kwargs.pop("ca_cert_dir", None)
        pool_kwargs["cert_reqs"] = "CERT_REQUIRED"
        return host_params, pool_kwargs

    def cert_verify(self, conn, url, verify, cert):
        # The per-origin SSLContext above owns verification and hostname checks.
        pass
