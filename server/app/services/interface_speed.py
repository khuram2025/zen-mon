"""The one interface-speed rule (utilization denominators everywhere).

An interface's capacity is, in order of preference:

1. ``configured`` -- the operator's circuit speed on the device interface
   (``device_interfaces.configured_speed_bps``; a 50 Mbps WAN on a 1 Gbps
   port is 50 Mbps, whatever SNMP says);
2. ``override`` -- a NetFlow per-interface capacity override
   (``netflow_interface_overrides.speed_in_bps`` / ``speed_out_bps``), for
   exporters whose interfaces are not polled or whose direction differs;
3. ``snmp`` -- the polled ``ifSpeed``/``ifHighSpeed``.

Zero, negative and missing values are "not known" and fall through; when
nothing is known the speed is ``None`` and a utilization percentage must not
be shown (never divide into a guess). Link utilization and NetFlow capacity
share this so a circuit never reads 5 % on one page and 100 % on another.
"""

from __future__ import annotations

SOURCE_CONFIGURED = "configured"
SOURCE_OVERRIDE = "override"
SOURCE_SNMP = "snmp"
SOURCE_UNKNOWN = "unknown"


def _positive(value) -> int | None:
    if value is None:
        return None
    try:
        v = int(value)
    except (TypeError, ValueError):
        return None
    return v if v > 0 else None


def effective_speed_source(configured_speed_bps=None, override_bps=None, if_speed=None) -> tuple[int | None, str]:
    """(speed in bit/s or None, which input it came from)."""
    for value, source in (
        (configured_speed_bps, SOURCE_CONFIGURED),
        (override_bps, SOURCE_OVERRIDE),
        (if_speed, SOURCE_SNMP),
    ):
        v = _positive(value)
        if v is not None:
            return v, source
    return None, SOURCE_UNKNOWN


def effective_speed(configured_speed_bps=None, override_bps=None, if_speed=None) -> int | None:
    """Capacity in bit/s: configured, else override, else SNMP; None if unknown."""
    return effective_speed_source(configured_speed_bps, override_bps, if_speed)[0]


def effective_speed_sql(configured: str, override: str | None, snmp: str) -> str:
    """The same rule as a PostgreSQL expression over column names."""
    parts = [configured] + ([override] if override else []) + [snmp]
    return "COALESCE(" + ", ".join(f"NULLIF(GREATEST({p}, 0), 0)" for p in parts) + ")"
