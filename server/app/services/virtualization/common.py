from __future__ import annotations

import asyncio
import ipaddress
import math
import socket
import ssl
from dataclasses import dataclass, field
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ConnectorError(Exception):
    """Only static, credential-free messages may cross this boundary."""


class ConnectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["nutanix_pc", "nutanix_pe", "vmware", "hyperv"]
    endpoint: str = Field(max_length=512)
    username: str = Field(default="", max_length=255)
    secret: SecretStr
    auth_type: Literal["password", "api_key"] = "password"
    ca_pem: str = Field(default="", max_length=65536)
    api_version: Literal["v4.0", "v4.1", "v4.2", "v4.3"] = "v4.0"

    @model_validator(mode="after")
    def validate_connection(self):
        try:
            u = urlsplit(self.endpoint)
            port = u.port
        except ValueError:
            raise ValueError("Invalid HTTPS endpoint") from None
        if (u.scheme != "https" or not u.hostname or u.username or u.password
                or u.query or u.fragment or u.path not in ("", "/")):
            raise ValueError("Use an HTTPS origin without credentials, path, query or fragment")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("Invalid endpoint port")
        self.endpoint = self.endpoint.rstrip("/")
        if not self.secret.get_secret_value():
            raise ValueError("A dedicated monitoring credential is required")
        if self.auth_type == "api_key" and self.provider != "nutanix_pc":
            raise ValueError("API keys are supported only by Prism Central v4")
        if self.auth_type == "password" and not self.username.strip():
            raise ValueError("Username is required")
        tls_context(self.ca_pem)
        return self

    def private_dict(self):
        value = self.model_dump()
        value["secret"] = self.secret.get_secret_value()
        return value


def tls_context(ca_pem=""):
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if ca_pem:
        try:
            ctx.load_verify_locations(cadata=ca_pem)
        except (ssl.SSLError, ValueError):
            raise ValueError("Invalid PEM certificate authority bundle") from None
    return ctx


async def validate_target(endpoint):
    u = urlsplit(endpoint)
    try:
        addresses = await asyncio.to_thread(socket.getaddrinfo, u.hostname, u.port or 443,
                                            type=socket.SOCK_STREAM)
    except OSError:
        raise ConnectorError("DNS resolution failed") from None
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0].split("%")[0])
        ip = getattr(ip, "ipv4_mapped", None) or ip
        if ip.is_loopback or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
            raise ConnectorError("Loopback, link-local and multicast targets are not permitted")


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None


def percent(used, capacity):
    a, b = number(used), number(capacity)
    return min(100.0, a * 100 / b) if a is not None and b else None


def health_from_score(score):
    """Provider health-check score (0-100) to a coarse state; missing scores stay unknown."""
    n = number(score)
    if n is None:
        return "unknown"
    return "healthy" if n >= 80 else ("warning" if n >= 50 else "critical")


def clean_attributes(attributes):
    """Descriptive provider fields: short strings or short string lists only, never nested provider bodies."""
    out = {}
    for key, value in (attributes or {}).items():
        if value is None or isinstance(value, (dict, bytes)):
            continue
        if isinstance(value, (list, tuple, set)):
            items = [str(v)[:120] for v in value if v not in (None, "") and not isinstance(v, (dict, list))]
            items = list(dict.fromkeys(items))[:20]
            if items:
                out[str(key)[:64]] = items
            continue
        if isinstance(value, bool):
            value = "yes" if value else "no"
        text = str(value).strip()
        if text:
            out[str(key)[:64]] = text[:300]
    return out


@dataclass
class Snapshot:
    resources: list[dict] = field(default_factory=list)
    alerts: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    capabilities: dict = field(default_factory=dict)
    version: str = ""

    def resource(self, kind, rid, name, *, parent_id=None, health="unknown", power_state=None,
                 metrics=None, url=None, attributes=None):
        if not rid:
            raise ConnectorError("Provider returned a resource without a stable identifier")
        metrics = {k: n for k, v in (metrics or {}).items() if (n := number(v)) is not None}
        if health == "unknown" and "health_score" in metrics:
            health = health_from_score(metrics["health_score"])
        item = dict(id=f"{kind}:{rid}", external_id=str(rid), kind=kind, name=str(name or rid),
                    parent_id=parent_id, health=health, power_state=power_state,
                    metrics=metrics, url=url, attributes=clean_attributes(attributes))
        self.resources.append(item)
        return item

    def summary(self):
        return dict(version=self.version, capabilities=self.capabilities, warnings=self.warnings,
                    counts={k: sum(r["kind"] == k for r in self.resources)
                            for k in ("cluster", "host", "vm", "storage")},
                    alert_count=len(self.alerts))
