"""Server inventory facts for discovery rules and dynamic device groups.

What a server's agent reports, as conditions can test it:

* ``services`` / ``running_services``: services (``server_service_inventory``).
  The agent reports **every** service with its state and start mode, not only
  the services its policy watches, refreshed when anything changes and at least
  every 10 minutes; a removed service is pruned. Windows agents report Windows
  services; Linux agents with systemd service collection report systemd
  services (``nginx`` for ``nginx.service``, the unit's description as display
  name). An older Linux agent reports none, so the fact is "not reported" for
  that server. A service matches by its service name (``MSSQLSERVER``) or its
  display name (``SQL Server (MSSQLSERVER)``).
* ``software`` / ``software_publishers``: installed software and its publishers
  (``server_software_inventory``, the agent's inventory snapshot every 6 hours;
  Windows uninstall entries). The Linux agent sends no software list, so the
  fact is "not reported" there. A very long list may be capped by the agent
  (``servers.software_truncated``).
* ``watched_processes``: processes on the agent policy's watch list that are
  running. The agent reports only its top processes by CPU and memory plus the
  watched ones, so "any process" cannot be tested reliably; put a process on
  the policy's watch list to test it.

A server with nothing reported for a fact (no agent, a Linux server for
services, inventory not collected yet) has the fact *missing*: every test fails
except "is not reported". Unknown never means "does not have it".

A list the agent has not refreshed for :data:`STALE_AFTER_HOURS` is missing too.
A reporting agent refreshes every list continuously (services at least every
10 minutes, watched processes on every upload, software with each inventory
snapshot, every 6 hours), so an older list means the agent stopped reporting
(uninstalled, server retired or offline); its last list no longer counts. A
short outage changes nothing.

Lists can be thousands of rows per server, so they are never loaded whole: the
values conditions name are collected first (:func:`needles`) and only matching
rows are read, plus one existence check per server and table.
"""

from __future__ import annotations

from typing import Any, Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# key -> (label, help); the condition type is "inventory" for all of them.
FIELDS: dict[str, tuple[str, str]] = {
    "services": ("Services", "Services on the server (name or display name), as its agent reports them: every "
                 "service, not only watched ones. Windows services, or systemd services (nginx for nginx.service) "
                 "from Linux agents that collect them; older Linux agents report none."),
    "running_services": ("Running services", "Services whose state is running, by name or display name (Windows "
                         "services, or systemd services from Linux agents that collect them)."),
    "software": ("Installed software", "Installed programs from the agent's inventory (every 6 hours). Windows "
                 "agents only; Linux servers report no software list."),
    "software_publishers": ("Software publishers", "Publishers of the installed software (Microsoft Corporation, "
                            "Oracle ...)."),
    "watched_processes": ("Watched processes running", "Processes on the agent policy's watch list that are running. "
                          "The agent reports only its busiest processes plus watched ones, so add a process to the "
                          "policy's watch list to test it here."),
}

# A list older than this is "not reported" (see the module docstring).
STALE_AFTER_HOURS = 24
_FRESH = f"NOW() - INTERVAL '{STALE_AFTER_HOURS} hours'"

_LIKE_ESCAPE = str.maketrans({"\\": "\\\\", "%": "\\%", "_": "\\_"})


def needles(conditions: Iterable[Any]) -> dict[str, dict[str, set[str]]]:
    """{field: {"exact": {...}, "contains": {...}}} for every inventory field the
    condition trees use (lower case). A field used only with "is reported"
    appears with empty sets."""
    out: dict[str, dict[str, set[str]]] = {}

    def walk(node):
        if not isinstance(node, dict):
            return
        if "op" in node:
            for c in node.get("conditions") or []:
                walk(c)
            return
        field = node.get("field")
        if field not in FIELDS:
            return
        slot = out.setdefault(field, {"exact": set(), "contains": set()})
        op, value = node.get("operator"), node.get("value")
        if op in ("has", "has_none"):
            slot["exact"].update(str(v).strip().lower() for v in (value if isinstance(value, list) else [value])
                                 if str(v or "").strip())
        elif op in ("has_containing", "has_none_containing") and str(value or "").strip():
            slot["contains"].add(str(value).strip().lower())

    for c in conditions:
        walk(c)
    return out


def merge(*parts: dict[str, dict[str, set[str]]]) -> dict[str, dict[str, set[str]]]:
    out: dict[str, dict[str, set[str]]] = {}
    for p in parts:
        for f, slot in (p or {}).items():
            o = out.setdefault(f, {"exact": set(), "contains": set()})
            o["exact"] |= slot["exact"]
            o["contains"] |= slot["contains"]
    return out


def _filter(cols: list[str], slot: dict[str, set[str]], params: dict, n: int) -> str:
    """SQL matching any of ``cols`` against the exact names or substrings."""
    exact, contains = sorted(slot["exact"]), sorted(slot["contains"])
    parts = []
    if exact:
        params[f"ex{n}"] = exact
        parts += [f"lower({c}) = ANY(CAST(:ex{n} AS text[]))" for c in cols]
    if contains:
        params[f"pat{n}"] = ["%" + s.translate(_LIKE_ESCAPE) + "%" for s in contains]
        # backslash is LIKE's default escape character (wildcards in the text are literal)
        parts += [f"lower({c}) LIKE ANY(CAST(:pat{n} AS text[]))" for c in cols]
    return "(" + " OR ".join(parts) + ")" if parts else "FALSE"


_TABLES = {
    "services": ("server_service_inventory", ["service_name", "display_name"], None),
    "running_services": ("server_service_inventory", ["service_name", "display_name"], "lower(state) = 'running'"),
    "software": ("server_software_inventory", ["package_name"], None),
    "software_publishers": ("server_software_inventory", ["vendor"], "vendor IS NOT NULL"),
    "watched_processes": ("server_process_inventory", ["name"], "watchlisted AND running"),
}

# Which rows are current, per table (``t`` = the table, ``i`` = the server id).
# Service and process rows are stamped on every report; software rows change
# only when the software does, so the server's inventory snapshot time counts.
_CURRENT = {
    "server_service_inventory": f"t.updated_at >= {_FRESH}",
    "server_process_inventory": f"t.updated_at >= {_FRESH}",
    "server_software_inventory": "EXISTS (SELECT 1 FROM servers sv WHERE sv.id = t.server_id"
                                 f" AND sv.inventory_updated_at >= {_FRESH})",
}


async def load(db: AsyncSession, server_ids: list[str], wanted: dict[str, dict[str, set[str]]]) -> dict[str, dict]:
    """{server id: {field: [matching names] | None}} for the fields in ``wanted``.
    None = not reported for that server."""
    ids = [str(i) for i in server_ids]
    out: dict[str, dict] = {i: {} for i in ids}
    if not ids or not wanted:
        return out
    present: dict[str, set[str]] = {}
    for table in sorted({_TABLES[f][0] for f in wanted}):
        rows = (await db.execute(text(
            f"SELECT i::text FROM unnest(CAST(:ids AS uuid[])) AS i"
            f" WHERE EXISTS (SELECT 1 FROM {table} t WHERE t.server_id = i AND {_CURRENT[table]})"),
            {"ids": ids})).all()
        present[table] = {r[0] for r in rows}
    for n, (field, slot) in enumerate(sorted(wanted.items())):
        table, cols, extra = _TABLES[field]
        params: dict = {"ids": ids}
        match = _filter(cols, slot, params, n)
        where = f"{_CURRENT[table]} AND " + (f"({extra}) AND {match}" if extra else match)
        found: dict[str, list[str]] = {}
        if match != "FALSE":
            select = ", ".join(cols)
            for row in (await db.execute(text(
                    f"SELECT t.server_id::text, {select} FROM {table} t"
                    f" WHERE t.server_id = ANY(CAST(:ids AS uuid[])) AND {where}"), params)).all():
                found.setdefault(row[0], []).extend(str(v) for v in row[1:] if v)
        for i in ids:
            out[i][field] = sorted(set(found.get(i, [])), key=str.lower) if i in present[table] else None
    return out


def evaluate(op: str, have: Any, want: Any) -> bool:
    """One inventory condition (``conditions`` calls it). ``have`` is the
    matching names, or None when not reported: then only "is not reported"
    holds."""
    if have is None:
        return op == "not_reported"
    if op == "reported":
        return True
    if op == "not_reported":
        return False
    names = [str(n).lower() for n in have]
    if op in ("has", "has_none"):
        wants = [str(w).strip().lower() for w in (want if isinstance(want, list) else [want])]
        hit = any(w in names for w in wants)
        return hit if op == "has" else not hit
    if op in ("has_containing", "has_none_containing"):
        w = str(want or "").strip().lower()
        hit = any(w in n for n in names)
        return hit if op == "has_containing" else not hit
    return False


HASH_PARTS = {
    # changed-pass fingerprint pieces (discovery rules), per server ``s``; a
    # list going stale changes its piece (to NULL), so the server is re-evaluated
    "services": "(SELECT md5(string_agg(lower(service_name) || ':' || COALESCE(lower(state), ''), ','"
                " ORDER BY service_name)) FROM server_service_inventory"
                f" WHERE server_id = s.id AND updated_at >= {_FRESH})",
    "software": f"(CASE WHEN s.inventory_updated_at >= {_FRESH} THEN s.inventory_updated_at::text END)",
    "processes": "(SELECT md5(string_agg(lower(name), ',' ORDER BY name)) FROM server_process_inventory"
                 f" WHERE server_id = s.id AND watchlisted AND running AND updated_at >= {_FRESH})",
}
