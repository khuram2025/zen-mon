"""Use the poller's verifier for interactive HTTP/TLS checks as well.

Each origin is verified first, then the actual HTTP connection is pinned to
that verified leaf. No HTTP credentials are passed to the helper. Contexts
last only for one workflow, so removal of trust takes effect on the next test.

Manual host addresses (docs/service-check-connections.md) are applied here the
way the poller applies them: the connection goes to the address, while the
Host header, the TLS server name and the certificate name check keep the name.
"""
import asyncio
import json
import os
import ssl
import subprocess
from urllib.parse import urlsplit

import ipaddress

import httpx
from requests.adapters import HTTPAdapter
from requests.exceptions import ConnectionError as RequestConnectionError, SSLError as RequestSSLError

from app.services.host_overrides import lookup as manual_address


def address_family_error(host, address, ip_version):
    """Plain words when a manual address is of the family the check excludes."""
    version = ipaddress.ip_address(address).version
    if ip_version == "ipv4" and version != 4:
        return f"The manual address {address} for {host} is IPv6, but this check is set to IPv4 only"
    if ip_version == "ipv6" and version != 6:
        return f"The manual address {address} for {host} is IPv4, but this check is set to IPv6 only"
    return None


def verified_context(host, port, timeout, policy, ip_version="auto", address=None):
    """Verify ``host``'s certificate, connecting to ``address`` when one is given."""
    binary = os.environ.get("ZENPLUS_PROBE_TLS_BINARY", "/opt/zenplus/bin/zenplus-poller")
    request = {"host": host, "port": port, "timeout": timeout, "policy": policy, "ip_version": ip_version}
    if address:
        request["address"] = address
    try:
        proc = subprocess.run([binary, "--verify-service-tls"], input=json.dumps(request), capture_output=True, text=True, timeout=timeout + 1, check=False)
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


def _manual_target(request, overrides, ip_version, used):
    """The manual address for this request's host, or None. Records what was used."""
    host = request.url.host
    address = manual_address(overrides, host)
    if address is None:
        return None
    problem = address_family_error(host, address, ip_version)
    if problem:
        raise httpx.ConnectError(problem, request=request)
    if used is not None:
        used.setdefault(host.rstrip(".").lower(), address)
    return address


async def _send_at(transport, request, address):
    """Send ``request`` over a connection to ``address``.

    Only the connection target changes. The Host header was fixed when the
    request was built, the TLS server name (and with it the certificate name
    check) is pinned to the name, and the caller still sees the request it
    made, so redirects and the reported URL keep the name.
    """
    if address is None:
        return await transport.handle_async_request(request)
    target = httpx.Request(
        request.method, request.url.copy_with(host=address), headers=request.headers, stream=request.stream,
        extensions={**request.extensions, "sni_hostname": request.url.host},
    )
    return await transport.handle_async_request(target)


class HostOverrideTransport(httpx.AsyncBaseTransport):
    """Manual host addresses for probes that do not go through ProbeTrustTransport.

    One inner transport per origin by name: two names on one address never
    share a connection, so a connection is only reused for the name it was
    opened (and, with verification on, verified) for.
    """
    def __init__(self, factory, overrides, ip_version="auto", used=None):
        self.factory, self.overrides, self.ip_version, self.used = factory, overrides, ip_version, used
        self.transports = {}

    async def handle_async_request(self, request):
        address = _manual_target(request, self.overrides, self.ip_version, self.used)
        origin = (request.url.scheme, request.url.host, request.url.port)
        if origin not in self.transports:
            self.transports[origin] = self.factory()
        return await _send_at(self.transports[origin], request, address)

    async def aclose(self):
        for transport in self.transports.values():
            await transport.aclose()


class ProbeTrustTransport(httpx.AsyncBaseTransport):
    def __init__(self, policy, timeout, ip_version="auto", overrides=None, used=None):
        self.ip_version = ip_version
        self.policy, self.timeout = policy, timeout
        self.overrides, self.used = overrides, used
        self.transports = {}

    async def handle_async_request(self, request):
        address = _manual_target(request, self.overrides, self.ip_version, self.used)
        origin = (request.url.scheme, request.url.host, request.url.port)
        if origin not in self.transports:
            verify = True
            if request.url.scheme == "https":
                try:
                    verify = await asyncio.to_thread(verified_context, request.url.host, request.url.port or 443, self.timeout, self.policy, self.ip_version, address)
                except ssl.SSLError as exc:
                    raise httpx.ConnectError(str(exc), request=request) from exc
            self.transports[origin] = httpx.AsyncHTTPTransport(verify=verify, local_address=probe_local_address(self.ip_version))
        return await _send_at(self.transports[origin], request, address)

    async def aclose(self):
        for transport in self.transports.values():
            await transport.aclose()


def probe_local_address(ip_version):
    return {"ipv4": "0.0.0.0", "ipv6": "::"}.get(ip_version)


class ProbeAddressAdapter(HTTPAdapter):
    """Requests adapter for probes: source address family and manual host addresses."""
    def __init__(self, ip_version="auto", overrides=None, used=None):
        self.ip_version = ip_version
        self.overrides, self.used = overrides, used
        super().__init__()

    def _manual_target(self, request):
        host = urlsplit(request.url).hostname
        address = manual_address(self.overrides, host)
        if address is None:
            return None
        problem = address_family_error(host, address, self.ip_version)
        if problem:
            raise RequestConnectionError(problem, request=request)
        if self.used is not None:
            self.used.setdefault(host.rstrip(".").lower(), address)
        return address

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        address = self._manual_target(request)
        if address:
            # Connect to the address; the pool stays specific to the name.
            host = urlsplit(request.url).hostname
            host_params["host"] = address
            if host_params.get("scheme") == "https":
                pool_kwargs["server_hostname"] = host
                if verify:
                    pool_kwargs["assert_hostname"] = host
        return host_params, pool_kwargs

    def add_headers(self, request, **kwargs):
        super().add_headers(request, **kwargs)
        if manual_address(self.overrides, urlsplit(request.url).hostname) and "Host" not in request.headers:
            # The connection is to an address; the service must still see its name.
            parsed = urlsplit(request.url)
            default = 443 if parsed.scheme == "https" else 80
            request.headers["Host"] = parsed.hostname if parsed.port in (None, default) else f"{parsed.hostname}:{parsed.port}"

    def init_poolmanager(self, connections, maxsize, block=False, **kwargs):
        address = probe_local_address(self.ip_version)
        if address:
            kwargs["source_address"] = (address, 0)
        return super().init_poolmanager(connections, maxsize, block=block, **kwargs)

    def proxy_manager_for(self, proxy, **kwargs):
        address = probe_local_address(self.ip_version)
        if address:
            kwargs["source_address"] = (address, 0)
        return super().proxy_manager_for(proxy, **kwargs)


class ProbeTrustAdapter(ProbeAddressAdapter):
    """Requests/NTLM equivalent, with a separate verified pin for every origin."""
    def __init__(self, policy, timeout, ip_version="auto", overrides=None, used=None):
        self.policy, self.timeout = policy, timeout
        self.contexts = {}
        super().__init__(ip_version, overrides, used)

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        host_params, pool_kwargs = super().build_connection_pool_key_attributes(request, verify, cert)
        parsed = urlsplit(request.url)
        origin = (parsed.hostname, parsed.port or 443)
        if origin not in self.contexts:
            try:
                self.contexts[origin] = verified_context(*origin, self.timeout, self.policy, self.ip_version, manual_address(self.overrides, parsed.hostname))
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
