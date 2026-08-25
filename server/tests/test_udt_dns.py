from __future__ import annotations

import asyncio

from app.services import udt_sweeper


def test_timed_out_reverse_dns_lookup_is_not_cancelled(monkeypatch):
    state = {"cancelled": False, "completed": False}

    async def slow_resolver(_address, _flags):
        try:
            await asyncio.sleep(0.02)
            state["completed"] = True
            return "endpoint.example", "0"
        except asyncio.CancelledError:
            state["cancelled"] = True
            raise

    async def scenario():
        result = await udt_sweeper._resolve_dns_name(
            "192.0.2.10", resolver=slow_resolver
        )
        await asyncio.sleep(0.03)
        return result

    monkeypatch.setattr(udt_sweeper, "DNS_TIMEOUT_S", 0.001)

    assert asyncio.run(scenario()) is None
    assert state == {"cancelled": False, "completed": True}
