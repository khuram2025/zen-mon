"""Stable names for the background jobs whose old integer keys collided.

Names are a persisted coordination contract: never rename a running job's key
without a coordinated worker restart. This registry changes only lock identity,
not transaction/session lifetime. Dedicated session-lock connections still
require a direct or session-pooled PostgreSQL endpoint (not transaction pooling).
These locks coordinate workers on one PostgreSQL authority; they are not a
fence against a paused old primary or an external notification provider.
"""
from hashlib import sha256
from types import MappingProxyType

_NAMES = (
    "apm.synthetic.sweep",
    "udt.sweep",
    "managed_device.child_sync",
    "netflow.rollup",
    "netflow.sweep",
    "udt.ad",
    "apm.service_graph",
    "udt.gateway",
    "netpath.enrichment",
)

# Python hash() is process-randomized. Signed SHA-256 prefix values are stable
# across processes and versions and use PostgreSQL's one-bigint lock API.
JOB_LOCK_KEYS = MappingProxyType({
    name: int.from_bytes(sha256(("zenplus:job:v1:" + name).encode("ascii")).digest()[:8], "big", signed=True)
    for name in _NAMES
})
if len(set(JOB_LOCK_KEYS.values())) != len(_NAMES):
    raise RuntimeError("background job advisory lock collision")
