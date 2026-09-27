"""Accept gzip-compressed request bodies from agents.

Agents compress their uploads once the controller advertises
``accepts_gzip`` in the heartbeat response. Host-result batches are JSON and
compress roughly tenfold, which also leaves far more headroom under nginx's
1 MiB request limit (that limit applies to the compressed bytes).

The decompressed size is capped so a small compressed body cannot expand
into an unbounded amount of memory.
"""

from __future__ import annotations

import zlib
from typing import Awaitable, Callable

AGENT_PATH_PREFIX = "/api/v1/agents/"
MAX_COMPRESSED_BYTES = 4 * 1024 * 1024
MAX_DECOMPRESSED_BYTES = 32 * 1024 * 1024

Scope = dict
Receive = Callable[[], Awaitable[dict]]
Send = Callable[[dict], Awaitable[None]]


class GzipRequestMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope.get("type") != "http"
            or not scope.get("path", "").startswith(AGENT_PATH_PREFIX)
            or _header(scope, b"content-encoding").strip().lower() != b"gzip"
        ):
            await self.app(scope, receive, send)
            return

        compressed = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            compressed.extend(message.get("body", b""))
            if len(compressed) > MAX_COMPRESSED_BYTES:
                await _reject(send, 413, b"compressed request body is too large")
                return
            if not message.get("more_body", False):
                break

        try:
            body = _gunzip(bytes(compressed))
        except _TooLarge:
            await _reject(send, 413, b"decompressed request body is too large")
            return
        except (zlib.error, EOFError):
            await _reject(send, 400, b"request body is not valid gzip")
            return

        headers = [
            (name, value) for name, value in scope.get("headers", [])
            if name.lower() not in (b"content-encoding", b"content-length")
        ]
        headers.append((b"content-length", str(len(body)).encode()))
        scope = dict(scope, headers=headers)
        delivered = False

        async def replay() -> dict:
            nonlocal delivered
            if delivered:
                return await receive()
            delivered = True
            return {"type": "http.request", "body": body, "more_body": False}

        await self.app(scope, replay, send)


class _TooLarge(Exception):
    pass


def _gunzip(data: bytes) -> bytes:
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    body = decoder.decompress(data, MAX_DECOMPRESSED_BYTES + 1)
    if len(body) > MAX_DECOMPRESSED_BYTES or decoder.unconsumed_tail:
        raise _TooLarge()
    if not decoder.eof:
        raise EOFError("truncated gzip stream")
    return body


def _header(scope: Scope, name: bytes) -> bytes:
    for key, value in scope.get("headers", []):
        if key.lower() == name:
            return value
    return b""


async def _reject(send: Send, status: int, detail: bytes) -> None:
    body = b'{"detail":"' + detail + b'"}'
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode())],
    })
    await send({"type": "http.response.body", "body": body})
