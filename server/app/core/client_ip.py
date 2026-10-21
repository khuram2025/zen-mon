"""The caller's address as nginx saw it.

uvicorn trusts forwarded headers only from the local reverse proxy
(``--forwarded-allow-ips 127.0.0.1``) and resolves the right-most untrusted
X-Forwarded-For hop into ``request.client``. Reading the header directly,
especially its left-most hop, would hand the client a field it controls.
The management access check and the login audit use the same address.
"""
from __future__ import annotations

from typing import Optional

from fastapi import Request


def client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None
