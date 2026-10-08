"""Agent inventory ingest for Inventory & Compliance (doc 02 section 3.3, 5.3; doc 04 T3, T4, T6, T7).

Two layers:

* Unit tests drive ``_upsert_inventory`` and its helpers with a recording
  session, so they need no database: payload aliases, clipping, sanitising,
  same-name dedupe, the truncation and server caps, key fallback, rule-pack
  degradation, planning failures and the snapshot ordering statements.
* Database tests run the real ``_upsert_inventory`` through an AsyncSession on
  the private PostgreSQL cluster from ``pg_fixture`` (never the appliance
  database), with migration 157's triggers live: first snapshot without events,
  a diff snapshot with exactly three, capped lists that prune only what the cut
  cannot hide, same name duplicates that do not flap, version gaps, rule-pack
  revision bumps, out-of-order snapshots, the per-server change budget, hostile
  strings, cross-server isolation, inline identity, and failure isolation.

The software prune only deletes rows older than two minutes, so database tests
age ``server_software_inventory.updated_at`` between uploads to stand in for
the six hours that separate real snapshots.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.schemas.agent import AgentResultsBatch, MetricSample
from app.services import baseline_service, inventory_normalize
from app.services import host_metric_service as metrics
from pg_fixture import PgDatabase, pg_cluster, pg_db, pg_db_module  # noqa: F401

LOGGER = "zenplus.host_metric_service"

WIN_2022 = {
    "name": "Microsoft Windows Server 2022 Standard Evaluation",
    "version": "21H2",
    "kernel_or_build": "10.0.20348.587 Build 20348.587",
    "architecture": "x86_64",
    "fqdn": "srv-ingest.example.test",
}


def sw(name, version="1.0", vendor="Acme Inc", arch="x64", source="hklm", location="", **extra):
    """One Windows agent software entry, shaped like readUninstallEntry's output."""
    return {
        "package_name": name, "name": name, "version": version,
        "vendor": vendor, "publisher": vendor, "install_date": None,
        "install_location": location, "architecture": arch, "source": source, **extra,
    }


def key_of(name, vendor="Acme Inc", version="1.0", source="hklm") -> str:
    """The name_key the normaliser gives a title: events must carry exactly this as subject_key."""
    return inventory_normalize.normalize_software(name, vendor, version, source=source).name_key


def named(count, start=0, version="1.0"):
    """``count`` distinct titles, Pkg <start> upwards."""
    return [sw(f"Pkg {i:04d}", version) for i in range(start, start + count)]


def agent_list(installed):
    """What the Windows agent sends for a host with these titles installed.

    ZenPlus_Agent/internal/collectors/native_windows.go installedSoftware():
    a stable sort by lower-cased name, then the first 1,000 entries. The tail is
    what a capped list loses, never the head.
    """
    return sorted(installed, key=lambda e: e["package_name"].lower())[:1000]


# ----------------------------------------------------------------- recording DB

class _Result:
    rowcount = 1

    def __init__(self, first=None):
        self._first = first

    def mappings(self):
        return self

    def first(self):
        return self._first

    def all(self):
        return []

    def scalar(self):
        return None


class _Savepoint:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RecordingDB:
    """Records every statement; the identity SELECT finds no server row."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params if params is not None else {}))
        return _Result()

    def begin_nested(self):
        return _Savepoint()

    def where(self, needle: str) -> list[tuple[int, dict]]:
        return [(i, p) for i, (sql, p) in enumerate(self.calls) if needle in sql]

    @property
    def upserts(self) -> list[dict]:
        return [p for _, p in self.where("INSERT INTO server_software_inventory")]

    @property
    def prunes(self) -> list[int]:
        return [i for i, _ in self.where("DELETE FROM server_software_inventory")]

    @property
    def blanket_prunes(self) -> list[int]:
        """Software deletes that remove every stale row, not just named ones."""
        return [i for i in self.prunes if "package_name = ANY" not in self.calls[i][0]]

    @property
    def claims(self) -> list[tuple[int, dict]]:
        return self.where("SET inventory_collected_at")

    @property
    def stamps(self) -> list[tuple[int, dict]]:
        return self.where("UPDATE servers SET inventory_updated_at")


class ClaimingDB(RecordingDB):
    """A recording session whose snapshot ordering statement finds a row, or none."""

    def __init__(self, accepts: bool):
        super().__init__()
        self.accepts = accepts

    async def execute(self, statement, params=None):
        result = await super().execute(statement, params)
        if "SET inventory_collected_at" in str(statement):
            return _Result(first=(1,) if self.accepts else None)
        return result


async def run_recorded(inventory: dict, **kwargs) -> RecordingDB:
    db = RecordingDB()
    await metrics._upsert_inventory(str(uuid.uuid4()), inventory, db, **kwargs)
    return db


# ------------------------------------------------------------ unit: aliases, clipping

@pytest.mark.asyncio
async def test_software_and_applications_aliases_and_fields():
    from_applications = await run_recorded({"applications": [
        {"display_name": "Alias App", "display_version": "2.5", "publisher": "Alias Vendor",
         "install_date": "20240131"},
    ]})
    [row] = from_applications.upserts
    assert (row["name"], row["ver"], row["vendor"]) == ("Alias App", "2.5", "Alias Vendor")
    assert row["install_date"] == datetime(2024, 1, 31, tzinfo=timezone.utc)

    preferred = await run_recorded({
        "software": [{"package_name": "Primary", "name": "ignored", "display_name": "ignored"}],
        "applications": [{"name": "Secondary"}],
    })
    assert [r["name"] for r in preferred.upserts] == ["Primary"]

    # An empty "software" falls through to "applications", exactly as before.
    fallthrough = await run_recorded({"software": [], "applications": [{"name": "Secondary"}]})
    assert [r["name"] for r in fallthrough.upserts] == ["Secondary"]


@pytest.mark.asyncio
async def test_unusable_entries_are_skipped_and_never_prune_or_stamp():
    db = await run_recorded({"software": [{"name": "  "}, {"version": "1"}, None, 5, "x"]})
    assert db.upserts == [] and db.prunes == [] and db.stamps == []

    db = await run_recorded({"software": "not a list"})
    assert db.calls == []


@pytest.mark.asyncio
async def test_values_are_clipped_to_column_widths():
    db = await run_recorded({"software": [sw(
        "N" * 300, "9" * 200, "V" * 300, arch="a" * 40, source="s" * 100, location="L" * 5000)]})
    [row] = db.upserts
    assert len(row["name"]) == 255
    assert len(row["ver"]) == 128
    assert len(row["vendor"]) == 255
    assert len(row["arch"]) == 16
    assert len(row["source"]) == 32
    assert len(row["location"]) == 1024
    assert len(row["name_key"]) <= 160 and len(row["pub_key"]) <= 96 and len(row["category"]) <= 32


@pytest.mark.asyncio
async def test_new_text_columns_drop_nul_bytes_empty_and_non_strings():
    db = await run_recorded({"software": [sw(
        "App", arch="x\x0064", source="hk\x00lm", location="C:\\Program\x00 Files\\App")]})
    [row] = db.upserts
    assert (row["arch"], row["source"], row["location"]) == ("x64", "hklm", "C:\\Program Files\\App")

    db = await run_recorded({"software": [sw("App", arch="", source=None, location=12345)]})
    [row] = db.upserts
    assert (row["arch"], row["source"], row["location"]) == (None, None, None)


@pytest.mark.asyncio
async def test_package_name_is_stored_exactly_as_sent_and_case_variants_stay_apart():
    db = await run_recorded({"software": [sw("Foo Bar", "1.0"), sw("FOO BAR", "2.0"), sw("  Foo Bar  ", "3.0")]})
    # The primary key is case sensitive: only exact stored names collapse. The
    # padded copy strips to "Foo Bar" (as before) and wins on its higher version.
    assert {r["name"]: r["ver"] for r in db.upserts} == {"Foo Bar": "3.0", "FOO BAR": "2.0"}


# ------------------------------------------------------------ unit: same-name dedupe

@pytest.mark.asyncio
async def test_names_that_differ_only_beyond_the_column_width_collapse_to_one_row():
    long_a, long_b = "N" * 255 + "-first", "N" * 255 + "-second"
    db = await run_recorded({"software": [sw(long_a, "1.0"), sw(long_b, "2.0")]})
    [row] = db.upserts
    assert row["name"] == "N" * 255 and row["ver"] == "2.0"


def dedupe(*entries):
    return metrics._collapse_duplicate_names(list(entries))


def entry(name, version, source="hklm", **extra):
    return {"name": name, "ver": version, "source": source, **extra}


def test_dedupe_keeps_highest_version_for_hklm_wow6432_hkcu_duplicates():
    entries = [
        entry("Tool", "2.0.1", "hklm"),
        entry("Tool", "2.0.5", "hklm_wow6432"),
        entry("Tool", "2.0.9", "hkcu"),
        entry("Other", "1.0"),
    ]
    for ordering in itertools.permutations(entries):
        kept = {e["name"]: e for e in dedupe(*ordering)}
        assert kept["Tool"]["ver"] == "2.0.9" and kept["Tool"]["source"] == "hkcu"
        assert kept["Other"]["ver"] == "1.0"
        assert len(kept) == 2


def test_dedupe_compares_numerically_not_as_text():
    kept, = dedupe(entry("Tool", "1.2.9"), entry("Tool", "1.2.10"), entry("Tool", "1.10"))
    assert kept["ver"] == "1.10"
    kept, = dedupe(entry("Tool", "4"), entry("Tool", "4a"))
    assert kept["ver"] == "4a"


def test_dedupe_ties_keep_the_first_entry():
    first = entry("Tool", "3.1", "hklm", marker="first")
    second = entry("Tool", "3.1", "hkcu", marker="second")
    assert dedupe(first, second)[0]["marker"] == "first"
    assert dedupe(second, first)[0]["marker"] == "second"


def test_dedupe_preserves_first_appearance_order_and_singletons():
    kept = dedupe(entry("B", "1"), entry("A", "1"), entry("B", "2"), entry("C", None))
    assert [e["name"] for e in kept] == ["B", "A", "C"]
    assert [e["ver"] for e in kept] == ["2", "1", None]


def test_dedupe_semverish_equal_versions_with_different_text_are_order_independent():
    # versioning ranks "1.0" and "1.0.0" equal; the survivor must not depend on order.
    one = dedupe(entry("Tool", "1.0"), entry("Tool", "1.0.0"))[0]["ver"]
    two = dedupe(entry("Tool", "1.0.0"), entry("Tool", "1.0"))[0]["ver"]
    assert one == two == "1.0.0"


@pytest.mark.parametrize("pair, expected", [
    (("1.2.3-1", "1.2.3-2"), "1.2.3-2"),          # packaging revisions: no portable ordering
    (("9.6p1", "9.7"), "9.7"),                    # one version semverish refuses
    (("9.6p1", "9.6p2"), "9.6p2"),
    (("1.2.3-SNAPSHOT", "1.2.3"), "1.2.3-SNAPSHOT"),  # deterministic, not a newness claim
    (("RC1", "rc1"), "rc1"),                      # equal natural key: raw text decides
    ((None, "0.1"), "0.1"),                       # a known version beats a missing one
    (("1.02", "1.2"), "1.2"),
])
def test_dedupe_fallback_is_a_deterministic_total_order(pair, expected):
    a, b = pair
    assert dedupe(entry("Tool", a), entry("Tool", b))[0]["ver"] == expected
    assert dedupe(entry("Tool", b), entry("Tool", a))[0]["ver"] == expected


def test_dedupe_fallback_handles_every_ordering_of_a_larger_group():
    versions = ["1.2.3-1", "1.2.3-10", "1.2.3-2", "1.2.3-9"]
    winners = {dedupe(*(entry("Tool", v) for v in order))[0]["ver"]
               for order in itertools.permutations(versions)}
    assert winners == {"1.2.3-10"}


def test_natural_key_orders_digit_runs_by_value_and_text_case_insensitively():
    key = metrics._natural_key
    assert key("1.9") < key("1.10") < key("1.10.1")
    assert key("2") > key("1.99.99")
    assert key("1.0RC1") == key("1.0rc1")
    assert key("") == key(None) == ()
    assert key("007") == key("7")
    assert key("1." + "9" * 6000) > key("1." + "9" * 5999)  # no int() size limit involved


# ------------------------------------------------------------ unit: truncation guard

@pytest.mark.parametrize("sent, truncated", [
    (0, False), (1, False), (999, False), (1000, True), (1001, True), (5000, True),
])
def test_truncation_decision(sent, truncated):
    assert metrics.SOFTWARE_LIST_AGENT_CAP == 1000
    assert metrics._software_snapshot_truncated(sent) is truncated


@pytest.mark.parametrize("count, truncated", [(999, False), (1000, True), (1001, True)])
@pytest.mark.asyncio
async def test_prune_and_flag_follow_the_cap(count, truncated):
    db = await run_recorded({"software": named(count)})
    assert len(db.upserts) == count
    # A capped list never removes every row it did not mention; what it may remove
    # is tested against the database below.
    assert (db.blanket_prunes == []) is truncated
    [(stamp_index, stamp_params)] = db.stamps
    assert stamp_params["truncated"] is truncated
    # Software prune (when it runs) and stamp both come after every upsert.
    last_upsert = max(i for i, _ in db.where("INSERT INTO server_software_inventory"))
    assert all(last_upsert < i for i in db.prunes) and last_upsert < stamp_index


@pytest.mark.asyncio
async def test_cap_counts_the_entries_sent_not_the_distinct_names():
    # 1,000 listed entries that collapse to 500 names still reached the agent's cap.
    entries = [sw(f"Pkg {i % 500}", f"1.{i}") for i in range(1000)]
    db = await run_recorded({"software": entries})
    assert len(db.upserts) == 500
    assert db.prunes == [] and db.stamps[0][1]["truncated"] is True


@pytest.mark.asyncio
async def test_empty_software_never_prunes_or_stamps_and_other_sections_still_prune():
    db = await run_recorded({"software": []})
    assert db.calls == []

    db = await run_recorded({"services": [{"service_name": "svc"}], "software": []})
    assert any("server_service_inventory" in sql and sql.startswith("DELETE") for sql, _ in db.calls)
    assert db.prunes == [] and db.stamps == []


@pytest.mark.asyncio
async def test_software_prune_runs_below_the_cap_and_stamps_after_it():
    db = await run_recorded({"software": named(3)})
    [prune] = db.prunes
    [(stamp_index, params)] = db.stamps
    assert prune < stamp_index
    assert params["truncated"] is False
    assert "inventory_baselined_at = COALESCE(inventory_baselined_at, NOW())" in db.calls[stamp_index][0]


@pytest.mark.asyncio
async def test_os_only_upload_stamps_freshness_but_not_baseline_or_truncation():
    db = await run_recorded({"os": WIN_2022})
    [(index, params)] = db.stamps
    sql = db.calls[index][0]
    assert "inventory_updated_at = NOW()" in sql
    assert "inventory_baselined_at" not in sql and "software_truncated" not in sql
    assert "truncated" not in params


@pytest.mark.asyncio
async def test_uploads_without_software_or_os_do_no_inventory_work():
    db = await run_recorded({"services": [], "filesystems": [], "network_interfaces": []})
    assert db.calls == []
    db = await run_recorded({"hardware": {"cpu": {"model": "Example CPU"}}})
    assert len(db.calls) == 1 and "UPDATE servers SET" in db.calls[0][0]
    assert "inventory_updated_at" not in db.calls[0][0]


# ------------------------------------------------------------ unit: caps, boundary, pre-stamp

def test_go_lower_is_the_agents_lower_casing_not_pythons():
    lower = metrics._go_lower
    assert lower("Pkg ABC") == "pkg abc"
    assert lower("\u0130stanbul") == "istanbul"            # Go maps U+0130 to a plain i
    assert "\u0130".lower() != "i"                          # Python expands it: the reason this exists
    assert lower("\u0391\u03a3") == "\u03b1\u03c3"          # no final-sigma rule in Go
    assert lower("\u00c4pfel") == "\u00e4pfel"
    assert lower("") == ""


@pytest.mark.parametrize("names, boundary", [
    (["Alpha", "beta", "Gamma"], "gamma"),                  # sorted by lower-cased name
    (["alpha", "ALPHA", "Alpha2"], "alpha2"),               # equal lower-cased names are in order
    (["b", "a"], None),                                     # not the agent's order
    (["Zed", "apple"], None),                               # sorted by raw text, not by lower-cased text
    (["x\uffff", "x\U00010000"], "x\U00010000"),            # code-point (UTF-8 byte) order, not UTF-16 order
    ([], None),
])
def test_prune_boundary_exists_only_for_a_list_in_the_agents_order(names, boundary):
    assert metrics._prune_boundary([{"name": n} for n in names]) == boundary


@pytest.mark.parametrize("extra", [7, 95_000])
@pytest.mark.asyncio
async def test_a_list_beyond_the_server_cap_is_cut_and_counts_as_truncated(extra):
    assert metrics.SOFTWARE_LIST_SERVER_CAP == 5000
    entries = [sw(f"Big {i:06d}") for i in range(metrics.SOFTWARE_LIST_SERVER_CAP + extra)]
    db = await run_recorded({"software": entries})
    assert [r["name"] for r in db.upserts] == [f"Big {i:06d}" for i in range(5000)]
    [(_, stamp)] = db.stamps
    assert stamp["truncated"] is True
    # Cut in listing order, not in the agent's: nothing at all can be proven gone.
    assert db.prunes == [] and db.where("SELECT package_name FROM server_software_inventory") == []


@pytest.mark.asyncio
async def test_a_list_exactly_at_the_server_cap_is_not_cut():
    entries = [sw(f"Big {i:06d}") for i in range(metrics.SOFTWARE_LIST_SERVER_CAP)]
    db = await run_recorded({"software": entries})
    assert len(db.upserts) == 5000


@pytest.mark.parametrize("count, truncated", [(999, False), (1000, True)])
@pytest.mark.asyncio
async def test_the_truncation_flag_is_stamped_before_the_rows_are_written(count, truncated):
    db = await run_recorded({"software": named(count)})
    [(early, params)] = db.where("SET software_truncated")
    assert params["truncated"] is truncated   # also False: a complete list must not inherit an old True
    first_upsert = min(i for i, _ in db.where("INSERT INTO server_software_inventory"))
    assert early < first_upsert
    assert "IS DISTINCT FROM" in db.calls[early][0]  # an unchanged flag writes nothing


@pytest.mark.asyncio
async def test_a_capped_list_in_the_agents_order_looks_for_rows_before_its_last_name():
    db = await run_recorded({"software": named(1000)})
    [(select, _)] = db.where("SELECT package_name FROM server_software_inventory")
    last_upsert = max(i for i, _ in db.where("INSERT INTO server_software_inventory"))
    assert last_upsert < select
    assert db.prunes == []  # the recording session reports no stale rows, so nothing is named


@pytest.mark.asyncio
async def test_a_capped_list_out_of_the_agents_order_prunes_nothing():
    db = await run_recorded({"software": list(reversed(named(1000)))})
    assert db.prunes == [] and db.where("SELECT package_name FROM server_software_inventory") == []


# ------------------------------------------------------------ unit: hostile strings

HOSTILE = "a\x00b\udc80c"
CLEANED = "ab?c"


def test_sanitize_drops_nul_and_replaces_lone_surrogates():
    assert metrics._sanitize(HOSTILE) == CLEANED
    assert metrics._sanitize("plain \u00e9\u4e2d\U0001f600") == "plain \u00e9\u4e2d\U0001f600"
    assert metrics._sanitize("\x00\x00") == ""
    metrics._sanitize("\ud800\udc00").encode("utf-8")  # whatever comes out is storable


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


@pytest.mark.asyncio
async def test_no_statement_receives_a_nul_or_a_lone_surrogate():
    db = await run_recorded({
        "hardware": {"cpu": {"model": HOSTILE, "logical_count": 2}},
        "os": {"name": HOSTILE, "version": HOSTILE, "kernel_or_build": HOSTILE,
               "architecture": HOSTILE, "fqdn": HOSTILE},
        "software": [sw(HOSTILE, HOSTILE, vendor=HOSTILE, arch=HOSTILE, source=HOSTILE, location=HOSTILE)],
    })
    assert db.upserts
    seen = [text for _, params in db.calls for text in _strings(params)]
    assert CLEANED in seen
    for text in seen:
        assert "\x00" not in text
        text.encode("utf-8")  # raises on a lone surrogate


@pytest.mark.asyncio
async def test_os_section_values_are_clipped_to_their_columns_and_non_text_is_ignored():
    db = await run_recorded({"os": {
        "name": "N" * 400, "version": "V" * 400, "kernel_or_build": "K" * 400,
        "architecture": "A" * 400, "fqdn": "F" * 400}})
    [(_, params)] = db.where("os_name = COALESCE")
    assert (len(params["n"]), len(params["v"]), len(params["k"]), len(params["a"]), len(params["f"])) \
        == (255, 128, 128, 32, 255)

    db = await run_recorded({"os": {"name": 5, "version": ["x"], "kernel_or_build": {"a": 1},
                                    "architecture": None, "fqdn": 1.5}})
    [(_, params)] = db.where("os_name = COALESCE")
    assert [params[k] for k in "nvkaf"] == [None] * 5

    # Not an object: nothing to apply, and nothing to fail on.
    for odd in (["Windows"], "Windows", 7):
        db = await run_recorded({"os": odd, "software": [sw("A")]})
        assert db.where("os_name = COALESCE") == []
        assert len(db.upserts) == 1


# ------------------------------------------------------------ unit: best-effort planning

@pytest.mark.parametrize("target", ["_plan_software", "_inventory_packs_usable"])
@pytest.mark.asyncio
async def test_a_planning_failure_writes_unkeyed_rows_and_logs_one_line(monkeypatch, caplog, target):
    def boom(*args, **kwargs):
        raise RuntimeError("planning exploded")

    monkeypatch.setattr(metrics, target, boom)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        db = await run_recorded({"software": named(3) + [sw("Pkg 0000", "9.9")], "os": WIN_2022})
    assert sorted(r["name"] for r in db.upserts) == ["Pkg 0000", "Pkg 0001", "Pkg 0002"]
    for row in db.upserts:
        assert (row["name_key"], row["pub_key"], row["category"], row["norm_rev"]) == (None, None, None, None)
    assert len(db.prunes) == 1 and len(db.stamps) == 1  # the snapshot is still applied as a whole
    planning = [r for r in caplog.records if "planning failed" in r.getMessage()]
    assert len(planning) == 1 and planning[0].exc_info
    assert not [r for r in caplog.records if "keys not computed" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_list_that_cannot_even_be_parsed_leaves_the_software_section_out(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("cannot parse")

    monkeypatch.setattr(metrics, "_software_entries", boom)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        db = await run_recorded({"software": named(3), "os": WIN_2022})
    assert db.upserts == [] and db.prunes == []
    [(_, params)] = db.stamps  # the OS section still stamps freshness
    assert "truncated" not in params
    assert db.where("os_name = COALESCE")
    assert len([r for r in caplog.records if "planning failed" in r.getMessage()]) == 1
    assert len([r for r in caplog.records if "could not be parsed" in r.getMessage()]) == 1


@pytest.mark.asyncio
async def test_planning_runs_off_the_event_loop(monkeypatch):
    seen: list[threading.Thread] = []
    real = metrics._plan_software

    def spy(*args, **kwargs):
        seen.append(threading.current_thread())
        return real(*args, **kwargs)

    monkeypatch.setattr(metrics, "_plan_software", spy)
    await run_recorded({"software": named(5)})
    assert seen and seen[0] is not threading.current_thread()


@pytest.mark.asyncio
async def test_the_keys_are_computed_strictly_so_a_failure_leaves_the_row_for_the_loop(monkeypatch):
    calls: list[dict] = []
    real = inventory_normalize.normalize_software

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(inventory_normalize, "normalize_software", spy)
    await run_recorded({"software": named(2)})
    assert calls and all(c.get("strict") is True for c in calls)

    def broken(*args, **kwargs):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(inventory_normalize, "_software_cached", broken)
    monkeypatch.setattr(inventory_normalize, "normalize_software", real)
    db = await run_recorded({"software": named(2)})
    # Not stamped with the 'unnamed-software' fallback under the current revision.
    assert all(r["name_key"] is None and r["norm_rev"] is None for r in db.upserts)


@pytest.mark.asyncio
async def test_a_logged_failing_title_is_quoted_not_pasted(monkeypatch, caplog):
    forged = "Evil\nWARNING zenplus.auth: fake entry"

    def boom(*args, **kwargs):
        raise ValueError("no key")

    monkeypatch.setattr(inventory_normalize, "normalize_software", boom)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await run_recorded({"software": [sw(forged)]})
    [record] = [r for r in caplog.records if "keys not computed" in r.getMessage()]
    assert "\n" not in record.getMessage()
    assert repr(forged) in record.getMessage()


# ------------------------------------------------------------ unit: snapshot ordering

SNAPSHOT_AT = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_without_a_snapshot_time_nothing_is_compared():
    db = await run_recorded({"software": named(2), "os": WIN_2022})
    assert db.claims == [] and len(db.upserts) == 2


@pytest.mark.asyncio
async def test_a_snapshot_is_claimed_before_anything_is_written():
    sid = str(uuid.uuid4())
    db = ClaimingDB(accepts=True)
    await metrics._upsert_inventory(sid, {
        "hardware": {"cpu": {"model": "CPU"}}, "os": WIN_2022, "software": named(2),
        "services": [{"service_name": "svc"}],
    }, db, collected_at=SNAPSHOT_AT)
    [(index, params)] = db.claims
    assert index == 0
    assert params == {"sid": sid, "ts": SNAPSHOT_AT, "window": metrics.SNAPSHOT_ORDER_WINDOW_S}
    sql = db.calls[index][0]
    assert "inventory_collected_at <= :ts" in sql          # equal times are accepted: a retry is idempotent
    assert "inventory_collected_at > NOW() + INTERVAL '1 day'" in sql   # a future-dated value never wedges
    assert "RETURNING" in sql
    assert len(db.upserts) == 2 and len(db.stamps) == 1 and db.where("inventory_identity")


@pytest.mark.asyncio
async def test_a_naive_snapshot_time_is_taken_as_utc():
    db = ClaimingDB(accepts=True)
    await metrics._upsert_inventory(str(uuid.uuid4()), {"os": WIN_2022}, db,
                                    collected_at=datetime(2026, 9, 30, 12, 0))
    assert db.claims[0][1]["ts"] == SNAPSHOT_AT


@pytest.mark.asyncio
async def test_a_stale_snapshot_skips_software_os_and_identity_only(caplog):
    db = ClaimingDB(accepts=False)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await metrics._upsert_inventory(str(uuid.uuid4()), {
            "hardware": {"cpu": {"model": "CPU"}}, "os": {**WIN_2022, "boot_time": "2026-09-01T00:00:00Z"},
            "software": named(3), "services": [{"service_name": "svc"}],
            "filesystems": [{"mount": "C:"}], "network_interfaces": [{"if_name": "eth0"}],
        }, db, collected_at=SNAPSHOT_AT)
    assert db.upserts == [] and db.prunes == [] and db.stamps == []
    assert not db.where("os_name = COALESCE") and not db.where("boot_time") and not db.where("inventory_identity")
    assert not db.where("SET software_truncated")
    # The other sections still apply, services and filesystems pruning included.
    assert db.where("cpu_physical_cores") and db.where("INSERT INTO server_service_inventory")
    assert db.where("INSERT INTO server_filesystem_inventory")
    assert db.where("INSERT INTO server_network_interface_inventory")
    assert any(sql.startswith("DELETE FROM server_service_inventory") for sql, _ in db.calls)
    skipped = [r for r in caplog.records if "older than one already accepted" in r.getMessage()]
    assert len(skipped) == 1 and skipped[0].levelno == logging.INFO


@pytest.mark.asyncio
async def test_an_upload_without_software_or_os_is_not_a_snapshot_and_claims_nothing():
    db = ClaimingDB(accepts=False)
    await metrics._upsert_inventory(str(uuid.uuid4()), {
        "hardware": {"cpu": {"model": "CPU"}}, "services": [{"service_name": "svc"}],
    }, db, collected_at=SNAPSHOT_AT)
    assert db.claims == [] and db.where("cpu_physical_cores")


@pytest.mark.asyncio
async def test_an_ordering_check_that_fails_accepts_the_snapshot(caplog):
    class FailingClaimDB(RecordingDB):
        async def execute(self, statement, params=None):
            if "SET inventory_collected_at" in str(statement):
                raise RuntimeError("claim exploded")
            return await super().execute(statement, params)

    db = FailingClaimDB()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await metrics._upsert_inventory(str(uuid.uuid4()), {"software": named(2), "os": WIN_2022}, db,
                                        collected_at=SNAPSHOT_AT)
    assert len(db.upserts) == 2 and len(db.stamps) == 1
    assert len([r for r in caplog.records if "ordering check failed" in r.getMessage()]) == 1


# ------------------------------------------------------------ unit: the change budget

class BudgetDB(RecordingDB):
    """A recording session that reports ``recent`` software events and can fail the upserts."""

    def __init__(self, recent: int, fail_upsert: bool = False):
        super().__init__()
        self.recent = recent
        self.fail_upsert = fail_upsert

    async def execute(self, statement, params=None):
        sql = str(statement)
        if self.fail_upsert and "INSERT INTO server_software_inventory" in sql:
            self.calls.append((sql, params or {}))
            raise RuntimeError("upsert exploded")
        result = await super().execute(statement, params)
        if "SELECT count(*) FROM (SELECT 1 FROM inventory_change_events" in sql:
            result.scalar = lambda: self.recent
        return result

    def settings(self) -> list[str]:
        return [str(p.get("value", "on")) for _, p in self.where("set_config")]


@pytest.mark.asyncio
async def test_the_budget_is_checked_before_the_software_is_written():
    assert metrics.SOFTWARE_EVENT_BUDGET_24H == 2000
    db = BudgetDB(recent=1999)
    await metrics._upsert_inventory(str(uuid.uuid4()), {"software": named(2)}, db)
    [(count_at, params)] = db.where("SELECT count(*) FROM (SELECT 1 FROM inventory_change_events")
    assert params["limit"] == 2000
    assert "category = 'software'" in db.calls[count_at][0]
    assert count_at < min(i for i, _ in db.where("INSERT INTO server_software_inventory"))
    assert db.where("set_config") == [] and db.where("budget_exceeded") == []


@pytest.mark.asyncio
async def test_over_budget_silences_capture_around_the_software_section_only():
    db = BudgetDB(recent=2000)
    await metrics._upsert_inventory(str(uuid.uuid4()), {"software": named(2), "os": WIN_2022}, db)
    on = db.where("set_config(:name, 'on', true)")
    restore = db.where("set_config(:name, :value, true)")
    assert len(on) == 1 and len(restore) == 1
    upserts = [i for i, _ in db.where("INSERT INTO server_software_inventory")]
    assert on[0][0] < min(upserts)
    assert max(upserts) < restore[0][0] < db.stamps[0][0]   # capture is back before the identity events
    assert restore[0][1]["value"] == "off"
    [(_, marker)] = db.where("budget_exceeded")
    assert marker["subject"] == "(change budget exceeded)" and marker["limit"] == 2000


@pytest.mark.asyncio
async def test_capture_is_restored_when_the_software_section_fails():
    db = BudgetDB(recent=2000, fail_upsert=True)
    with pytest.raises(RuntimeError, match="upsert exploded"):   # the existing error behaviour is unchanged
        await metrics._upsert_inventory(str(uuid.uuid4()), {"software": named(2)}, db)
    assert len(db.where("set_config(:name, :value, true)")) == 1


@pytest.mark.asyncio
async def test_a_failing_budget_check_leaves_capture_alone_and_the_upload_goes_on(caplog):
    class Failing(RecordingDB):
        async def execute(self, statement, params=None):
            if "SELECT count(*) FROM (SELECT 1 FROM inventory_change_events" in str(statement):
                raise RuntimeError("budget exploded")
            return await super().execute(statement, params)

    db = Failing()
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await metrics._upsert_inventory(str(uuid.uuid4()), {"software": named(2)}, db)
    assert len(db.upserts) == 2 and db.where("set_config") == []
    assert len([r for r in caplog.records if "budget check failed" in r.getMessage()]) == 1


# ------------------------------------------------------------ unit: SQL contract

def test_upsert_sql_refreshes_evidence_but_never_first_seen_at():
    sql = str(metrics._SOFTWARE_UPSERT_SQL)
    update = sql.split("DO UPDATE SET", 1)[1]
    assert "first_seen_at" not in sql
    # A snapshot without a version is a gap, not a downgrade to NULL.
    assert "version = COALESCE(EXCLUDED.version, server_software_inventory.version)" in update
    assert "package_name" not in update
    for column in ("vendor", "install_date", "architecture", "source", "install_location",
                   "name_key", "publisher_key", "category", "norm_rev"):
        assert f"{column} = " in update


def test_identity_columns_are_the_normaliser_fields_except_lifecycle_slug():
    fields = {f.name for f in dataclasses.fields(inventory_normalize.Identity)}
    assert set(metrics._IDENTITY_COLUMNS) == fields - {"lifecycle_slug"}


# ------------------------------------------------------------ unit: key fallback

@pytest.mark.asyncio
async def test_key_computation_failure_falls_back_to_null_keys_and_logs_once(monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("rule pack exploded")

    monkeypatch.setattr(inventory_normalize, "normalize_software", boom)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        db = await run_recorded({"software": named(50)})
    assert len(db.upserts) == 50
    for row in db.upserts:
        assert (row["name_key"], row["pub_key"], row["category"], row["norm_rev"]) == (None, None, None, None)
    failures = [r for r in caplog.records if "keys not computed" in r.getMessage()]
    assert len(failures) == 1
    assert "50 of 50" in failures[0].getMessage()
    assert failures[0].exc_info and isinstance(failures[0].exc_info[1], RuntimeError)
    assert len(db.stamps) == 1  # the upload carried on


@pytest.mark.asyncio
async def test_one_failing_entry_does_not_unkey_the_others(monkeypatch):
    real = inventory_normalize.normalize_software

    def picky(name, *args, **kwargs):
        if name == "Bad Title":
            raise ValueError("nope")
        return real(name, *args, **kwargs)

    monkeypatch.setattr(inventory_normalize, "normalize_software", picky)
    db = await run_recorded({"software": [sw("Good Title"), sw("Bad Title")]})
    by_name = {r["name"]: r for r in db.upserts}
    assert by_name["Bad Title"]["name_key"] is None and by_name["Bad Title"]["norm_rev"] is None
    assert by_name["Good Title"]["name_key"] and by_name["Good Title"]["norm_rev"] == inventory_normalize.RULEPACK_REV


@pytest.mark.asyncio
async def test_keys_carry_the_current_rule_pack_revision():
    db = await run_recorded({"software": [sw("Google Chrome", "126.0.1", vendor="Google LLC")]})
    [row] = db.upserts
    assert (row["name_key"], row["pub_key"], row["category"]) == ("google-chrome", "google", "browser")
    assert row["norm_rev"] == inventory_normalize.RULEPACK_REV


@pytest.mark.asyncio
async def test_degraded_rule_packs_leave_norm_rev_unset_and_skip_identity(monkeypatch, caplog):
    monkeypatch.setattr(inventory_normalize, "packs_healthy", lambda: False)
    monkeypatch.setattr(inventory_normalize, "PACK_ERRORS", ["software_rules.json: corrupt"])
    monkeypatch.setattr(metrics, "_PACKS_DEGRADED_LOGGED", False)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        first = await run_recorded({"software": named(3), "os": WIN_2022})
        second = await run_recorded({"software": named(3), "os": WIN_2022})
    for db in (first, second):
        assert len(db.upserts) == 3
        assert all(r["norm_rev"] is None and r["name_key"] for r in db.upserts)
        assert not db.where("inventory_identity")  # inline identity skipped entirely
        assert len(db.stamps) == 1
    degraded = [r for r in caplog.records if "rule packs are degraded" in r.getMessage()]
    assert len(degraded) == 1 and "software_rules.json: corrupt" in degraded[0].getMessage()


# ------------------------------------------------------------------ database

EVENT_KEYS = ("category", "action", "subject", "subject_key", "old_value", "new_value",
              "asset_name", "detail")
SOFTWARE_KEYS = ("version", "vendor", "install_date", "architecture", "source", "install_location",
                 "name_key", "publisher_key", "category", "norm_rev", "first_seen_at", "updated_at")
IDENTITY_KEYS = (*metrics._IDENTITY_COLUMNS, "norm_rev", "values_rev", "normalized_at", "source_changed_at")
_names = itertools.count(1)


def add_server(db: PgDatabase, **cols) -> uuid.UUID:
    cols.setdefault("display_name", f"ingest-srv-{next(_names)}")
    names = list(cols)
    marks = ", ".join(f"${i + 1}" for i in range(len(names)))
    return db.scalar(f"INSERT INTO servers ({', '.join(names)}) VALUES ({marks}) RETURNING id",
                     *cols.values())


async def upload(db: PgDatabase, server_id, inventory: dict, *, user: str = "zen", collected_at=None) -> None:
    """One agent upload: _upsert_inventory then commit, as its own transaction."""
    engine = create_async_engine(db.url_for(user), poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await metrics._upsert_inventory(str(server_id), inventory, session, collected_at=collected_at)
            await session.commit()
    finally:
        await engine.dispose()


def age(db: PgDatabase, server_id) -> None:
    """Stand in for the hours between real snapshots (the prune window is 2 minutes)."""
    db.execute("UPDATE server_software_inventory SET updated_at = now() - interval '1 hour' "
               "WHERE server_id = $1", server_id)


def age_events(db: PgDatabase, server_id, by: timedelta = timedelta(hours=25)) -> None:
    """Move a server's history back in time, past the change budget's 24 hour window."""
    db.execute("UPDATE inventory_change_events SET observed_at = observed_at - $2::interval "
               "WHERE asset_id = $1", server_id, by)


def software(db: PgDatabase, server_id) -> dict[str, dict]:
    rows = db.query(f"SELECT package_name, {', '.join(SOFTWARE_KEYS)} FROM server_software_inventory "
                    "WHERE server_id = $1", server_id)
    return {r[0]: dict(zip(SOFTWARE_KEYS, r[1:])) for r in rows}


def events(db: PgDatabase, server_id) -> list[dict]:
    rows = db.query(f"SELECT {', '.join(EVENT_KEYS)} FROM inventory_change_events "
                    "WHERE asset_id = $1 ORDER BY id", server_id)
    return [dict(zip(EVENT_KEYS, r)) for r in rows]


def server_row(db: PgDatabase, server_id) -> dict:
    cols = ("inventory_updated_at", "inventory_collected_at", "inventory_baselined_at", "software_truncated",
            "os_name", "os_version", "kernel_or_build")
    row = db.query(f"SELECT {', '.join(cols)} FROM servers WHERE id = $1", server_id)[0]
    return dict(zip(cols, row))


def identity(db: PgDatabase, server_id) -> dict | None:
    rows = db.query(f"SELECT {', '.join(IDENTITY_KEYS)} FROM inventory_identity "
                    "WHERE asset_kind = 'server' AND asset_id = $1", server_id)
    return dict(zip(IDENTITY_KEYS, rows[0])) if rows else None


# ---- T3: first snapshot of a new server

@pytest.mark.asyncio
async def test_t3_first_snapshot_writes_rows_baselines_and_emits_no_events(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    assert server_row(db, sid)["inventory_baselined_at"] is None

    await upload(db, sid, {
        "os": WIN_2022,
        "software": [
            sw("Google Chrome", "126.0.1", vendor="Google LLC", arch="x64", source="hklm",
               location="C:\\Program Files\\Google\\Chrome", install_date="2024-03-05T00:00:00Z"),
            sw("7-Zip 23.01 (x64)", "23.01", vendor="Igor Pavlov", arch="x64", source="hklm"),
            sw("Per User Tool", "0.9", vendor="Solo Dev", arch="user", source="hkcu"),
        ],
    })

    rows = software(db, sid)
    assert set(rows) == {"Google Chrome", "7-Zip 23.01 (x64)", "Per User Tool"}
    chrome = rows["Google Chrome"]
    assert chrome["version"] == "126.0.1" and chrome["vendor"] == "Google LLC"
    assert chrome["architecture"] == "x64" and chrome["source"] == "hklm"
    assert chrome["install_location"] == "C:\\Program Files\\Google\\Chrome"
    assert chrome["install_date"] == datetime(2024, 3, 5, tzinfo=timezone.utc)
    assert rows["Per User Tool"]["architecture"] == "user" and rows["Per User Tool"]["source"] == "hkcu"
    assert rows["Per User Tool"]["install_location"] is None  # the agent sends "" for none

    server = server_row(db, sid)
    assert server["inventory_baselined_at"] is not None
    assert server["inventory_updated_at"] is not None
    assert server["software_truncated"] is False
    assert events(db, sid) == []


# ---- T4: the second snapshot diffs

@pytest.mark.asyncio
async def test_t4_second_snapshot_emits_exactly_added_removed_and_changed(pg_db_module):
    db = pg_db_module
    sid = add_server(db, display_name="t4-server")
    await upload(db, sid, {"software": [
        sw("Keeper", "1.0"),
        sw("Upgraded Tool", "2.0.0", vendor="Tool Corp", arch="x64", source="hklm"),
        sw("Retired Tool", "5.1", vendor="Old Corp", arch="x86", source="hklm_wow6432"),
    ]})
    assert events(db, sid) == []
    age(db, sid)

    await upload(db, sid, {"software": [
        sw("Keeper", "1.0"),
        sw("Upgraded Tool", "2.1.0", vendor="Tool Corp", arch="x64", source="hklm"),
        sw("Fresh Tool", "3.3", vendor="New Corp", arch="user", source="hkcu"),
    ]})

    got = {e["action"]: e for e in events(db, sid)}
    assert sorted(got) == ["added", "changed", "removed"]
    assert len(events(db, sid)) == 3
    for e in got.values():
        assert e["category"] == "software" and e["asset_name"] == "t4-server"

    added = got["added"]
    assert (added["subject"], added["old_value"], added["new_value"]) == ("Fresh Tool", None, "3.3")
    assert added["subject_key"] == key_of("Fresh Tool", "New Corp", "3.3", "hkcu")
    assert added["detail"] == {"vendor": "New Corp", "architecture": "user", "source": "hkcu"}

    changed = got["changed"]
    assert (changed["subject"], changed["old_value"], changed["new_value"]) == ("Upgraded Tool", "2.0.0", "2.1.0")
    assert changed["subject_key"] == key_of("Upgraded Tool", "Tool Corp", "2.1.0")
    assert changed["subject_key"] == software(db, sid)["Upgraded Tool"]["name_key"]
    assert changed["detail"] == {"vendor": "Tool Corp", "architecture": "x64", "source": "hklm"}

    removed = got["removed"]
    assert (removed["subject"], removed["old_value"], removed["new_value"]) == ("Retired Tool", "5.1", None)
    assert removed["subject_key"] == key_of("Retired Tool", "Old Corp", "5.1", "hklm_wow6432")
    assert removed["detail"] == {"vendor": "Old Corp", "architecture": "x86", "source": "hklm_wow6432"}

    assert set(software(db, sid)) == {"Keeper", "Upgraded Tool", "Fresh Tool"}


@pytest.mark.asyncio
async def test_identical_snapshots_emit_nothing_however_often_they_repeat(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    payload = {"os": WIN_2022, "software": [sw("Steady", "1.0"), sw("Steady Two", "2.0")]}
    await upload(db, sid, payload)
    for _ in range(3):
        age(db, sid)
        await upload(db, sid, payload)
    assert events(db, sid) == []


# ---- T6: a capped list prunes only what the cut cannot hide

def tagged(event) -> bool:
    return event["detail"].get("snapshot_truncated") is True


@pytest.mark.asyncio
async def test_t6_a_capped_host_keeps_its_uninstalls_and_tags_what_the_cut_decides(pg_db_module):
    """A host with 1,100 titles: the agent sends the first 1,000 by lower-cased name."""
    db = pg_db_module
    sid = add_server(db, display_name="t6-server")
    host = named(1100)  # Pkg 0000 .. Pkg 1099 installed

    await upload(db, sid, {"software": agent_list(host)})  # Pkg 0000 .. Pkg 0999
    assert len(software(db, sid)) == 1000 and events(db, sid) == []
    assert server_row(db, sid)["software_truncated"] is True
    age(db, sid)

    # Pkg 0050 is uninstalled; Pkg 1000, installed all along, slides into the visible 1,000.
    host = [e for e in host if e["package_name"] != "Pkg 0050"]
    await upload(db, sid, {"software": agent_list(host)})
    assert set(software(db, sid)) == {f"Pkg {i:04d}" for i in range(1001) if i != 50}
    got = {(e["action"], e["subject"]): e for e in events(db, sid)}
    assert sorted(got) == [("added", "Pkg 1000"), ("removed", "Pkg 0050")]
    # The removal is real, the arrival is an artefact of the cut; both are tagged so a reader can tell.
    assert tagged(got[("added", "Pkg 1000")]) and tagged(got[("removed", "Pkg 0050")])
    assert got[("removed", "Pkg 0050")]["subject_key"] == key_of("Pkg 0050")
    assert server_row(db, sid)["software_truncated"] is True
    age(db, sid)

    # The same list again changes nothing.
    await upload(db, sid, {"software": agent_list(host)})
    assert len(events(db, sid)) == 2 and len(software(db, sid)) == 1000


@pytest.mark.asyncio
async def test_a_title_that_sorts_first_does_not_remove_the_one_pushed_past_the_cut(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    host = named(1100)
    await upload(db, sid, {"software": agent_list(host)})
    age(db, sid)

    host = host + [sw("Aardvark Suite")]  # Pkg 0999 is now the 1,001st title and drops off the list
    await upload(db, sid, {"software": agent_list(host)})

    assert "Pkg 0999" not in {e["package_name"] for e in agent_list(host)}
    assert "Pkg 0999" in software(db, sid)           # still installed: the cut hides it, nothing proves it gone
    assert [(e["action"], e["subject"]) for e in events(db, sid)] == [("added", "Aardvark Suite")]


@pytest.mark.asyncio
async def test_t6_dropping_under_the_cap_trusts_the_list_again_and_clears_the_flag(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    host = named(1100)
    await upload(db, sid, {"software": agent_list(host)})
    age(db, sid)
    await upload(db, sid, {"software": agent_list([e for e in host if e["package_name"] != "Pkg 0050"])})
    age(db, sid)
    before = len(events(db, sid))   # the cut's own artefacts: added Pkg 1000, removed Pkg 0050
    assert server_row(db, sid)["software_truncated"] is True

    # 150 more titles go: 950 are left, a complete list. Everything not in it is gone, including the
    # rows that sat beyond the old cut, and none of the events is tagged: the snapshot was complete.
    keep = [e for e in host if 150 <= int(e["package_name"][4:])]
    assert 900 < len(keep) < 1000
    stored = set(software(db, sid))
    kept = {e["package_name"] for e in keep}
    await upload(db, sid, {"software": agent_list(keep)})

    assert server_row(db, sid)["software_truncated"] is False
    assert set(software(db, sid)) == kept
    later = events(db, sid)[before:]
    assert {e["subject"] for e in later if e["action"] == "removed"} == stored - kept
    assert {e["subject"] for e in later if e["action"] == "added"} == kept - stored
    assert len(later) == len(stored - kept) + len(kept - stored)
    assert not any(tagged(e) for e in later)


@pytest.mark.asyncio
async def test_a_stored_row_that_only_ties_with_the_last_name_is_not_removed(pg_db_module):
    """The agent's sort is stable on the lower-cased name: of two spellings, one may have been cut."""
    db = pg_db_module
    sid = add_server(db)
    filler = [sw(f"Item {i:04d}") for i in range(999)]
    await upload(db, sid, {"software": [*filler, sw("ZED"), sw("Zed")]})  # both spellings are stored
    assert {"ZED", "Zed"} <= set(software(db, sid))
    age(db, sid)

    await upload(db, sid, {"software": [*filler, sw("Zed")]})  # exactly 1,000: the cut fell between the two
    assert {"ZED", "Zed"} <= set(software(db, sid))
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_a_stored_sibling_of_an_earlier_title_is_removed_when_the_list_is_capped(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("ITEM 0500"), sw("Item 0500"), *named(10)]})
    age(db, sid)

    capped = sorted([*named(1000), sw("Item 0500")], key=lambda e: e["package_name"].lower())[:1000]
    await upload(db, sid, {"software": capped})

    # 'ITEM 0500' lower-cases to 'item 0500', long before the last listed name: it would have been listed.
    assert "ITEM 0500" not in software(db, sid)
    assert [e["subject"] for e in events(db, sid) if e["action"] == "removed"] == ["ITEM 0500"]


@pytest.mark.asyncio
async def test_a_capped_list_that_is_not_in_the_agents_order_prunes_nothing(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": named(1100)[:600]})
    age(db, sid)
    stored = set(software(db, sid))

    await upload(db, sid, {"software": list(reversed(named(1000, start=300)))})  # a sender with another order
    assert stored <= set(software(db, sid))
    assert [e for e in events(db, sid) if e["action"] == "removed"] == []
    assert server_row(db, sid)["software_truncated"] is True


@pytest.mark.asyncio
async def test_a_list_cut_at_the_server_cap_flags_truncation_and_bounds_the_table(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": named(50)})
    age(db, sid)
    monkeypatch.setattr(metrics, "SOFTWARE_LIST_SERVER_CAP", 1200)

    await upload(db, sid, {"software": named(1500, start=100)})   # 300 beyond the cap are dropped

    rows = software(db, sid)
    assert len(rows) == 50 + 1200 and "Pkg 1299" in rows and "Pkg 1300" not in rows
    assert server_row(db, sid)["software_truncated"] is True
    assert [e for e in events(db, sid) if e["action"] == "removed"] == []   # nothing can be proven gone


@pytest.mark.asyncio
async def test_exactly_999_entries_still_prunes(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": named(999)})
    age(db, sid)
    await upload(db, sid, {"software": named(998)})
    assert server_row(db, sid)["software_truncated"] is False
    assert len(software(db, sid)) == 998
    assert [e["action"] for e in events(db, sid)] == ["removed"]


@pytest.mark.asyncio
async def test_empty_software_list_never_prunes_and_leaves_markers_alone(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": named(3)})
    db.execute("UPDATE servers SET software_truncated = true WHERE id = $1", sid)
    before = server_row(db, sid)
    age(db, sid)

    await upload(db, sid, {"software": []})
    await upload(db, sid, {"software": [], "applications": []})

    assert len(software(db, sid)) == 3
    assert events(db, sid) == []
    assert server_row(db, sid) == before  # flag, updated_at and baselined_at untouched


# ---- T7: duplicate names do not flap

@pytest.mark.asyncio
async def test_t7_same_name_duplicates_collapse_to_the_highest_version_without_flapping(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    tool = [
        sw("Dual Tool", "2.0.1", vendor="Dual Corp", arch="x64", source="hklm"),
        sw("Dual Tool", "2.0.5", vendor="Dual Corp", arch="x86", source="hklm_wow6432"),
        sw("Dual Tool", "2.0.9", vendor="Dual Corp", arch="user", source="hkcu"),
    ]
    revisions = [  # no portable ordering: the deterministic fallback decides
        sw("Packaged Lib", "1.2.3-1", arch="x64", source="hklm"),
        sw("Packaged Lib", "1.2.3-2", arch="user", source="hkcu"),
    ]
    other = [sw("Bystander", "4.0")]
    uploads = [
        [*tool, *revisions, *other],
        [*reversed(tool), *other, *reversed(revisions)],
        [revisions[1], tool[1], *other, revisions[0], tool[2], tool[0]],
    ]

    for index, entries in enumerate(uploads):
        age(db, sid)
        await upload(db, sid, {"software": entries})
        rows = software(db, sid)
        assert set(rows) == {"Dual Tool", "Packaged Lib", "Bystander"}, index
        assert (rows["Dual Tool"]["version"], rows["Dual Tool"]["source"]) == ("2.0.9", "hkcu"), index
        assert rows["Packaged Lib"]["version"] == "1.2.3-2", index
    assert events(db, sid) == []
    assert db.scalar("SELECT count(*) FROM server_software_inventory WHERE server_id = $1", sid) == 3


@pytest.mark.asyncio
async def test_a_genuine_upgrade_among_duplicates_is_one_changed_event(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("Dual Tool", "2.0.1", source="hklm"), sw("Dual Tool", "2.0.9", source="hkcu")]})
    age(db, sid)
    await upload(db, sid, {"software": [sw("Dual Tool", "2.1.0", source="hkcu"), sw("Dual Tool", "2.0.1", source="hklm")]})
    [event] = events(db, sid)
    assert (event["action"], event["old_value"], event["new_value"]) == ("changed", "2.0.9", "2.1.0")


# ---- row details

@pytest.mark.asyncio
async def test_first_seen_at_survives_reuploads_and_version_changes(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("Long Lived", "1.0", vendor="Old Vendor")]})
    db.execute("UPDATE server_software_inventory SET first_seen_at = now() - interval '30 days' "
               "WHERE server_id = $1", sid)
    first_seen = software(db, sid)["Long Lived"]["first_seen_at"]
    age(db, sid)
    aged = software(db, sid)["Long Lived"]["updated_at"]

    await upload(db, sid, {"software": [sw("Long Lived", "1.0", vendor="Old Vendor")]})
    await upload(db, sid, {"software": [sw("Long Lived", "1.1", vendor="New Vendor", source="hklm_wow6432")]})

    row = software(db, sid)["Long Lived"]
    assert row["first_seen_at"] == first_seen
    assert row["updated_at"] > aged
    assert (row["version"], row["vendor"], row["source"]) == ("1.1", "New Vendor", "hklm_wow6432")


@pytest.mark.asyncio
async def test_install_date_is_kept_when_a_later_snapshot_omits_it(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("Dated", "1.0", install_date="20240131")]})
    await upload(db, sid, {"software": [sw("Dated", "1.0", install_date=None)]})
    assert software(db, sid)["Dated"]["install_date"] == datetime(2024, 1, 31, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_keys_category_and_revision_are_stored(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [
        sw("Google Chrome", "126.0.1", vendor="Google LLC"),
        sw("Security Update for Microsoft Windows (KB4011281)", "1", vendor="Microsoft Corporation"),
        sw("Mystery Utility 3.2", "3.2", vendor="Nobody Ltd"),
    ]})
    rows = software(db, sid)
    chrome = rows["Google Chrome"]
    assert (chrome["name_key"], chrome["publisher_key"], chrome["category"]) == ("google-chrome", "google", "browser")
    update = rows["Security Update for Microsoft Windows (KB4011281)"]
    assert (update["name_key"], update["category"]) == ("kb4011281", "update")
    mystery = rows["Mystery Utility 3.2"]
    assert mystery["name_key"] and mystery["publisher_key"] and mystery["category"] == "other"
    assert {r["norm_rev"] for r in rows.values()} == {inventory_normalize.RULEPACK_REV}


@pytest.mark.asyncio
async def test_key_columns_are_refreshed_when_the_vendor_changes(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("Acme Widget", "1.0", vendor="Acme Inc")]})
    db.execute("UPDATE server_software_inventory SET name_key = 'stale', category = 'stale', norm_rev = 0 "
               "WHERE server_id = $1", sid)
    await upload(db, sid, {"software": [sw("Acme Widget", "1.0", vendor="Acme Inc")]})
    row = software(db, sid)["Acme Widget"]
    assert row["name_key"] == "acme-widget" and row["category"] != "stale"
    assert row["norm_rev"] == inventory_normalize.RULEPACK_REV
    assert events(db, sid) == []  # a key refresh is not a version change


@pytest.mark.asyncio
async def test_stored_package_name_keeps_the_exact_incoming_casing(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("MixedCase APP", "1.0")]})
    await upload(db, sid, {"software": [sw("MixedCase APP", "1.1")]})
    assert list(software(db, sid)) == ["MixedCase APP"]


# ---- stamps

@pytest.mark.asyncio
async def test_os_only_upload_stamps_freshness_only(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022})
    server = server_row(db, sid)
    assert server["inventory_updated_at"] is not None
    assert server["inventory_baselined_at"] is None
    assert server["software_truncated"] is False
    assert (server["os_name"], server["os_version"]) == (WIN_2022["name"], WIN_2022["version"])

    # The first software snapshot after an OS-only upload is still the silent baseline.
    await upload(db, sid, {"software": [sw("First Ever", "1.0")]})
    assert server_row(db, sid)["inventory_baselined_at"] is not None
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_baselined_at_is_set_once_and_updated_at_moves_on_every_snapshot(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("A", "1.0")]})
    db.execute("UPDATE servers SET inventory_baselined_at = now() - interval '2 days', "
               "inventory_updated_at = now() - interval '2 days' WHERE id = $1", sid)
    backdated = server_row(db, sid)

    await upload(db, sid, {"software": [sw("A", "1.0")]})

    after = server_row(db, sid)
    assert after["inventory_baselined_at"] == backdated["inventory_baselined_at"]
    assert after["inventory_updated_at"] > backdated["inventory_updated_at"]


@pytest.mark.asyncio
async def test_uploads_without_software_or_os_leave_inventory_columns_and_identity_alone(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    seeded = identity(db, sid)
    assert seeded["norm_rev"] is None
    await upload(db, sid, {"hardware": {"cpu": {"model": "Example CPU", "logical_count": 8}}})
    server = server_row(db, sid)
    assert server["inventory_updated_at"] is None and server["inventory_baselined_at"] is None
    assert identity(db, sid)["norm_rev"] is None and identity(db, sid)["normalized_at"] is None


# ---- inline identity

@pytest.mark.asyncio
async def test_inline_identity_for_a_windows_server_2022_payload(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    assert identity(db, sid)["norm_rev"] is None  # seeded dirty by the AFTER INSERT trigger

    await upload(db, sid, {"os": WIN_2022, "software": [sw("App", "1.0")]})

    row = identity(db, sid)
    expected = dataclasses.asdict(inventory_normalize.normalize_server(
        WIN_2022["name"], WIN_2022["version"], WIN_2022["kernel_or_build"], WIN_2022["architecture"]))
    assert (row["os_family"], row["os_product"], row["os_release"]) == ("windows", "Windows Server", "2022")
    assert row["os_version"] == expected["os_version"] == "10.0.20348.587"
    assert row["os_build"] == expected["os_build"] == "20348.587"
    assert row["os_edition"] == "Standard Evaluation"
    assert row["os_source"] == "agent"
    assert {c: row[c] for c in metrics._IDENTITY_COLUMNS} == {c: expected[c] for c in metrics._IDENTITY_COLUMNS}
    assert row["norm_rev"] == inventory_normalize.RULEPACK_REV
    assert row["normalized_at"] is not None
    assert events(db, sid) == []  # NULL -> value is discovery


@pytest.mark.asyncio
async def test_os_version_change_between_uploads_writes_one_os_changed_event(pg_db_module):
    db = pg_db_module
    sid = add_server(db, display_name="os-change-server")
    await upload(db, sid, {"os": WIN_2022})
    clean = identity(db, sid)
    assert clean["norm_rev"] == inventory_normalize.RULEPACK_REV

    patched = {**WIN_2022, "kernel_or_build": "10.0.20348.2113 Build 20348.2113"}
    await upload(db, sid, {"os": patched})

    [event] = events(db, sid)
    assert (event["category"], event["action"], event["subject"]) == ("os", "changed", "Windows Server")
    assert (event["old_value"], event["new_value"]) == ("10.0.20348.587", "10.0.20348.2113")
    assert event["asset_name"] == "os-change-server"
    assert event["detail"] == {"os_family": "windows", "os_release": "2022", "os_source": "agent"}
    row = identity(db, sid)
    assert row["os_version"] == "10.0.20348.2113" and row["norm_rev"] == inventory_normalize.RULEPACK_REV
    assert row["source_changed_at"] > clean["source_changed_at"]  # the trigger's stamp, not ours

    await upload(db, sid, {"os": patched})  # repeating the new value is not a change
    assert len(events(db, sid)) == 1


@pytest.mark.asyncio
async def test_unchanged_identity_is_not_rewritten_on_every_upload(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022, "software": [sw("App", "1.0")]})
    first = identity(db, sid)
    age(db, sid)
    await upload(db, sid, {"os": WIN_2022, "software": [sw("App", "1.0")]})
    assert identity(db, sid) == first  # normalized_at included


@pytest.mark.asyncio
async def test_a_software_only_upload_repairs_a_dirty_identity(pg_db_module):
    db = pg_db_module
    sid = add_server(db, os_name=WIN_2022["name"], os_version="21H2",
                     kernel_or_build=WIN_2022["kernel_or_build"])
    assert identity(db, sid)["norm_rev"] is None
    await upload(db, sid, {"software": [sw("App", "1.0")]})
    assert identity(db, sid)["norm_rev"] == inventory_normalize.RULEPACK_REV
    assert identity(db, sid)["os_release"] == "2022"


@pytest.mark.asyncio
async def test_missing_identity_row_is_created_defensively(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    db.execute("DELETE FROM inventory_identity WHERE asset_kind = 'server' AND asset_id = $1", sid)
    await upload(db, sid, {"os": WIN_2022})
    assert identity(db, sid)["os_family"] == "windows"
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_lifecycle_slug_is_never_overwritten_by_ingest(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022})
    db.execute("UPDATE inventory_identity SET lifecycle_slug = 'windows-server' "
               "WHERE asset_kind = 'server' AND asset_id = $1", sid)
    await upload(db, sid, {"os": {**WIN_2022, "kernel_or_build": "10.0.20348.2113 Build 20348.2113"}})
    assert db.scalar("SELECT lifecycle_slug FROM inventory_identity WHERE asset_id = $1", sid) == "windows-server"


# ---- failure isolation

@pytest.mark.asyncio
async def test_normaliser_exceptions_never_fail_the_upload(pg_db_module, monkeypatch, caplog):
    db = pg_db_module
    sid = add_server(db)

    def boom(*args, **kwargs):
        raise RuntimeError("normaliser exploded")

    monkeypatch.setattr(inventory_normalize, "normalize_software", boom)
    monkeypatch.setattr(inventory_normalize, "normalize_server", boom)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await upload(db, sid, {"os": WIN_2022, "software": named(25)})

    rows = software(db, sid)
    assert len(rows) == 25
    for row in rows.values():
        assert (row["name_key"], row["publisher_key"], row["category"], row["norm_rev"]) == (None, None, None, None)
    assert identity(db, sid)["norm_rev"] is None  # still dirty: the loop repairs it
    assert identity(db, sid)["os_family"] is None  # the seeded row was not touched
    server = server_row(db, sid)
    assert server["inventory_baselined_at"] is not None and server["os_name"] == WIN_2022["name"]
    assert events(db, sid) == []
    messages = [r.getMessage() for r in caplog.records]
    assert sum("keys not computed" in m for m in messages) == 1  # once per upload, not per row
    assert sum("Inline inventory identity failed" in m for m in messages) == 1

    # Nothing is stuck: the next healthy upload keys the rows and cleans the identity.
    monkeypatch.undo()
    await upload(db, sid, {"os": WIN_2022, "software": named(25)})
    assert {r["norm_rev"] for r in software(db, sid).values()} == {inventory_normalize.RULEPACK_REV}
    assert identity(db, sid)["norm_rev"] == inventory_normalize.RULEPACK_REV


@pytest.mark.asyncio
async def test_identity_database_error_rolls_back_only_its_savepoint(pg_db_module, monkeypatch, caplog):
    db = pg_db_module
    sid = add_server(db)
    oversized = inventory_normalize.Identity(os_family="windows", os_product="P" * 500)
    monkeypatch.setattr(inventory_normalize, "normalize_server", lambda *a, **k: oversized)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await upload(db, sid, {"os": WIN_2022, "software": [sw("Survivor", "1.0")]})

    assert set(software(db, sid)) == {"Survivor"}
    server = server_row(db, sid)
    assert server["inventory_baselined_at"] is not None and server["inventory_updated_at"] is not None
    assert server["os_name"] == WIN_2022["name"]
    assert identity(db, sid)["norm_rev"] is None and identity(db, sid)["os_product"] is None
    assert sum("Inline inventory identity failed" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_stamp_database_error_rolls_back_only_its_savepoint(pg_db, caplog):
    db = pg_db
    sid = add_server(db)
    # Any stamp of inventory_updated_at now violates a constraint.
    db.execute("ALTER TABLE servers ADD CONSTRAINT zz_no_inventory_stamp CHECK (inventory_updated_at IS NULL)")

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await upload(db, sid, {"os": WIN_2022, "software": [sw("Survivor", "1.0")]})

    assert set(software(db, sid)) == {"Survivor"}
    server = server_row(db, sid)
    assert server["inventory_updated_at"] is None and server["inventory_baselined_at"] is None
    assert server["os_name"] == WIN_2022["name"]
    assert identity(db, sid)["norm_rev"] == inventory_normalize.RULEPACK_REV  # the identity step still ran
    assert sum("freshness stamp failed" in r.getMessage() for r in caplog.records) == 1
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_degraded_packs_leave_database_rows_dirty_for_the_loop(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    monkeypatch.setattr(inventory_normalize, "packs_healthy", lambda: False)
    monkeypatch.setattr(metrics, "_PACKS_DEGRADED_LOGGED", True)  # already reported; the log is unit tested

    await upload(db, sid, {"os": WIN_2022, "software": [sw("Google Chrome", "126.0.1", vendor="Google LLC")]})

    chrome = software(db, sid)["Google Chrome"]
    assert chrome["norm_rev"] is None and chrome["name_key"] == "google-chrome"
    assert identity(db, sid)["norm_rev"] is None
    assert server_row(db, sid)["inventory_baselined_at"] is not None


@pytest.mark.asyncio
async def test_uploads_work_under_the_application_role(pg_db):
    """The appliance connects as zenplus and the capture triggers run with its privileges."""
    db = pg_db
    # What install.sh grants on an appliance.
    db.execute("GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO zenplus; "
               "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO zenplus")
    sid = add_server(db, display_name="app-role-server")

    await upload(db, sid, {"os": WIN_2022, "software": [sw("Steady", "1.0"), sw("Gone", "1.0")]}, user="zenplus")
    assert events(db, sid) == []
    age(db, sid)
    patched = {**WIN_2022, "kernel_or_build": "10.0.20348.2113 Build 20348.2113"}
    await upload(db, sid, {"os": patched, "software": [sw("Steady", "1.1"), sw("New", "2.0")]}, user="zenplus")

    got = sorted((e["category"], e["action"], e["subject"]) for e in events(db, sid))
    assert got == [("os", "changed", "Windows Server"), ("software", "added", "New"),
                   ("software", "changed", "Steady"), ("software", "removed", "Gone")]
    assert identity(db, sid)["os_version"] == "10.0.20348.2113"
    assert server_row(db, sid)["inventory_baselined_at"] is not None


# ---- version gaps are not changes

@pytest.mark.asyncio
async def test_a_snapshot_without_a_version_is_a_gap_not_two_changes(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("Agentx", "4.2"), sw("Steady", "1.0")]})
    for version in ("", "4.2"):  # one snapshot with a half-written uninstall key, then the real one
        age(db, sid)
        await upload(db, sid, {"software": [sw("Agentx", version), sw("Steady", "1.0")]})
        assert software(db, sid)["Agentx"]["version"] == "4.2"
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_an_upgrade_that_happens_across_a_version_gap_is_still_one_event(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("Agentx", "4.2")]})
    for version in ("", "4.3"):
        age(db, sid)
        await upload(db, sid, {"software": [sw("Agentx", version)]})
    [event] = events(db, sid)
    assert (event["action"], event["old_value"], event["new_value"]) == ("changed", "4.2", "4.3")


@pytest.mark.asyncio
async def test_a_version_that_first_appears_later_is_discovery_not_a_change(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("Unversioned", "")]})
    assert software(db, sid)["Unversioned"]["version"] is None
    age(db, sid)
    await upload(db, sid, {"software": [sw("Unversioned", "1.0")]})
    assert software(db, sid)["Unversioned"]["version"] == "1.0"
    assert events(db, sid) == []


# ---- rule-pack revision bumps never read as changes (contract C1)

def rerender_with(monkeypatch, revision, rerender):
    """A rule-pack release: RULEPACK_REV moves and the normaliser renders some value differently."""
    real = inventory_normalize.normalize_server

    def rendered(*args, **kwargs):
        return rerender(real(*args, **kwargs))

    monkeypatch.setattr(inventory_normalize, "RULEPACK_REV", revision)
    monkeypatch.setattr(inventory_normalize, "normalize_server", rendered)


def without_revision_number(identity):
    """Revision 2 renders a Windows version without its update revision: 10.0.20348.587 -> 10.0.20348."""
    version = identity.os_version
    if version and version.count(".") >= 3:
        identity = dataclasses.replace(identity, os_version=version.rsplit(".", 1)[0])
    return identity


@pytest.mark.asyncio
async def test_inline_identity_records_the_revision_that_rendered_its_values(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022, "software": [sw("App", "1.0")]})
    row = identity(db, sid)
    assert row["norm_rev"] == row["values_rev"] == inventory_normalize.RULEPACK_REV


@pytest.mark.asyncio
async def test_a_revision_bump_re_rendering_a_clean_row_writes_no_event(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022})
    assert identity(db, sid)["os_version"] == "10.0.20348.587"

    rerender_with(monkeypatch, 2, without_revision_number)
    await upload(db, sid, {"os": WIN_2022})  # the same data, a new rule pack

    row = identity(db, sid)
    assert (row["os_version"], row["norm_rev"], row["values_rev"]) == ("10.0.20348", 2, 2)
    assert events(db, sid) == []
    await upload(db, sid, {"os": WIN_2022})
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_a_revision_bump_re_rendering_a_dirty_row_writes_no_event(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022})
    rerender_with(monkeypatch, 2, without_revision_number)
    # An unrelated source column changed before the loop reached the row: it is dirty (norm_rev NULL)
    # and no longer says which revision rendered its values, but values_rev still does.
    db.execute("UPDATE servers SET cpu_model = 'Other CPU' WHERE id = $1", sid)
    assert identity(db, sid)["norm_rev"] is None and identity(db, sid)["values_rev"] == 1

    await upload(db, sid, {"os": WIN_2022})

    row = identity(db, sid)
    assert (row["os_version"], row["norm_rev"], row["values_rev"]) == ("10.0.20348", 2, 2)
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_a_revision_bump_re_rendering_a_never_normalised_row_writes_no_event(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db, os_name=WIN_2022["name"], os_version="21H2", kernel_or_build=WIN_2022["kernel_or_build"])
    # Values written by an older writer that did not record a revision: norm_rev and values_rev are NULL.
    db.execute("UPDATE inventory_identity SET os_family = 'windows', os_product = 'Windows Server', "
               "os_version = '10.0.20348.587', norm_rev = NULL, values_rev = NULL "
               "WHERE asset_kind = 'server' AND asset_id = $1", sid)
    rerender_with(monkeypatch, 2, without_revision_number)

    await upload(db, sid, {"os": WIN_2022})

    row = identity(db, sid)
    assert (row["os_version"], row["norm_rev"], row["values_rev"]) == ("10.0.20348", 2, 2)
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_rolling_back_to_an_older_revision_writes_no_event(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022})
    with monkeypatch.context() as newer:
        rerender_with(newer, 2, without_revision_number)
        await upload(db, sid, {"os": WIN_2022})
        assert identity(db, sid)["os_version"] == "10.0.20348"

    await upload(db, sid, {"os": WIN_2022})  # the older release is back; nothing selects the row for the loop

    row = identity(db, sid)
    assert (row["os_version"], row["norm_rev"], row["values_rev"]) == ("10.0.20348.587", 1, 1)
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_hardware_renderings_are_covered_too(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db, hw_manufacturer="Dell Inc.", hw_model="PowerEdge R740", hw_serial="ABC1234")
    await upload(db, sid, {"os": WIN_2022})
    assert identity(db, sid)["hw_model"] == "PowerEdge R740"

    rerender_with(monkeypatch, 2, lambda i: dataclasses.replace(
        i, hw_model=i.hw_model.replace("PowerEdge ", ""), hw_serial=i.hw_serial.lower()))
    await upload(db, sid, {"os": WIN_2022})

    row = identity(db, sid)
    assert (row["hw_model"], row["hw_serial"]) == ("R740", "abc1234")
    assert events(db, sid) == []


@pytest.mark.asyncio
async def test_a_genuine_change_after_the_bump_is_recorded_by_the_next_upload(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022})
    rerender_with(monkeypatch, 2, without_revision_number)
    await upload(db, sid, {"os": WIN_2022})
    assert events(db, sid) == []

    # The change is far enough to show under both renderings.
    await upload(db, sid, {"os": {**WIN_2022, "kernel_or_build": "10.0.26100.1742 Build 26100.1742"}})

    [event] = events(db, sid)
    assert (event["category"], event["old_value"], event["new_value"]) == ("os", "10.0.20348", "10.0.26100")


# ---- the normaliser runs strictly: a failure leaves the row dirty (contract C5)

@pytest.mark.asyncio
async def test_a_normaliser_failure_leaves_the_identity_dirty_instead_of_stamping_a_fallback(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    seen: list[bool] = []
    real = inventory_normalize.normalize_server

    def spy(*args, **kwargs):
        seen.append(kwargs.get("strict"))
        return real(*args, **kwargs)

    monkeypatch.setattr(inventory_normalize, "normalize_server", spy)
    await upload(db, sid, {"os": WIN_2022})
    assert seen == [True]
    monkeypatch.setattr(inventory_normalize, "normalize_server", real)

    # An internal engine failure: the total (default) behaviour would return the 'unknown' identity,
    # which the row would then keep as clean under the current revision.
    def engine_bug(*args, **kwargs):
        raise RuntimeError("engine bug")

    monkeypatch.setattr(inventory_normalize, "_normalize_server", engine_bug)
    await upload(db, sid, {"os": {**WIN_2022, "kernel_or_build": "10.0.20348.2113 Build 20348.2113"}})

    row = identity(db, sid)
    assert row["norm_rev"] is None                       # dirty again: the loop retries it
    assert row["os_family"] == "windows"                 # the last good identity is untouched
    assert events(db, sid) == []


# ---- one server cannot write unbounded history (the change budget)

BUDGET = metrics.SOFTWARE_EVENT_BUDGET_24H
MARKER = metrics.BUDGET_MARKER_SUBJECT


def seed_events(db: PgDatabase, server_id, count: int, category: str = "software") -> None:
    db.execute("INSERT INTO inventory_change_events (asset_kind, asset_id, category, action, subject) "
               "SELECT 'server', $1, $3, 'added', 'seed ' || g FROM generate_series(1, $2) g",
               server_id, count, category)


def software_events(db: PgDatabase, server_id) -> list[dict]:
    return [e for e in events(db, server_id) if e["category"] == "software" and e["subject"] != MARKER]


def markers(db: PgDatabase, server_id) -> list[dict]:
    return [e for e in events(db, server_id) if e["subject"] == MARKER]


async def baselined(db: PgDatabase, titles: int = 20) -> uuid.UUID:
    sid = add_server(db, display_name=f"budget-{next(_names)}")
    await upload(db, sid, {"software": named(titles)})
    age(db, sid)
    return sid


@pytest.mark.asyncio
async def test_a_server_over_budget_keeps_its_state_current_and_records_one_marker(pg_db_module, caplog):
    db = pg_db_module
    sid = await baselined(db)
    seed_events(db, sid, BUDGET)
    before = len(events(db, sid))

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        await upload(db, sid, {"software": [*named(15, version="2.0"), sw("Brand New", "1.0")]})  # 15 upgrades, 1 add, 5 removes
    [warning] = [r for r in caplog.records if "software change events in 24 hours" in r.getMessage()]
    assert warning.levelno == logging.WARNING
    caplog.clear()

    rows = software(db, sid)
    assert set(rows) == {*(f"Pkg {i:04d}" for i in range(15)), "Brand New"}
    assert {rows[f"Pkg {i:04d}"]["version"] for i in range(15)} == {"2.0"}
    assert len(events(db, sid)) == before + 1   # the marker, nothing else
    [marker] = markers(db, sid)
    name = db.scalar("SELECT display_name FROM servers WHERE id = $1", sid)
    assert (marker["category"], marker["action"], marker["asset_name"]) == ("software", "changed", name)
    assert marker["detail"] == {"budget_exceeded": True, "limit": BUDGET}
    assert marker["old_value"] is None and marker["new_value"] is None

    age(db, sid)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        await upload(db, sid, {"software": named(15, version="3.0")})
    assert len(markers(db, sid)) == 1                              # at most one per 24 hours
    assert software(db, sid)["Pkg 0000"]["version"] == "3.0"
    # The warning goes with the marker; the repeat of the same storm is only an info line.
    [again] = [r for r in caplog.records if "software change events in 24 hours" in r.getMessage()]
    assert again.levelno == logging.INFO


@pytest.mark.asyncio
async def test_below_the_budget_everything_is_recorded(pg_db_module):
    db = pg_db_module
    sid = await baselined(db)
    seed_events(db, sid, BUDGET - 1)

    await upload(db, sid, {"software": named(20, version="2.0")})

    assert len(software_events(db, sid)) == BUDGET - 1 + 20
    assert markers(db, sid) == []


@pytest.mark.asyncio
async def test_only_software_events_of_the_last_24_hours_count_against_the_budget(pg_db_module):
    db = pg_db_module
    sid = await baselined(db)
    seed_events(db, sid, BUDGET, category="os")
    seed_events(db, sid, BUDGET, category="hardware")
    await upload(db, sid, {"software": named(20, version="2.0")})
    assert len(software_events(db, sid)) == 20 and markers(db, sid) == []

    seed_events(db, sid, BUDGET)
    age_events(db, sid)
    age(db, sid)
    await upload(db, sid, {"software": named(20, version="3.0")})   # the old events no longer count
    assert markers(db, sid) == []
    assert len([e for e in software_events(db, sid) if e["new_value"] == "3.0"]) == 20


@pytest.mark.asyncio
async def test_capture_resumes_once_the_window_has_passed_and_a_new_marker_follows_a_new_storm(pg_db_module):
    db = pg_db_module
    sid = await baselined(db)
    seed_events(db, sid, BUDGET)
    await upload(db, sid, {"software": named(20, version="2.0")})
    assert len(markers(db, sid)) == 1 and len(software_events(db, sid)) == BUDGET

    age_events(db, sid)
    await upload(db, sid, {"software": named(20, version="3.0")})
    assert len(markers(db, sid)) == 1
    assert len([e for e in software_events(db, sid) if e["new_value"] == "3.0"]) == 20

    seed_events(db, sid, BUDGET)
    await upload(db, sid, {"software": named(20, version="4.0")})
    assert len(markers(db, sid)) == 2


@pytest.mark.asyncio
async def test_a_storm_of_changed_titles_plateaus_instead_of_growing_with_every_upload(pg_db_module):
    db = pg_db_module
    sid = await baselined(db, titles=999)
    bystander = await baselined(db, titles=3)

    counts = []
    for n in range(1, 31):
        await upload(db, sid, {"software": named(999, version=f"2.{n}")})
        counts.append(len(events(db, sid)))

    # 30 uploads of 999 changed titles would write 29,970 events. The budget lets the server reach
    # 2,000 (the upload that crosses it completes), then one marker, then nothing.
    assert counts[0] == 999
    assert counts[-1] == counts[6] <= BUDGET + 999 + 1
    assert counts[-1] > BUDGET
    assert len(markers(db, sid)) == 1
    assert {r["version"] for r in software(db, sid).values()} == {"2.30"}   # state stays current
    await upload(db, bystander, {"software": named(3, version="2.0")})      # others are unaffected
    assert len(software_events(db, bystander)) == 3 and markers(db, bystander) == []


@pytest.mark.asyncio
async def test_identity_events_are_still_recorded_while_software_events_are_silenced(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": WIN_2022, "software": named(5)})
    age(db, sid)
    seed_events(db, sid, BUDGET)
    before = len(events(db, sid))

    await upload(db, sid, {"os": {**WIN_2022, "kernel_or_build": "10.0.20348.2113 Build 20348.2113"},
                           "software": named(5, version="2.0")})

    new = events(db, sid)[before:]
    assert sorted((e["category"], e["subject"]) for e in new) == [("os", "Windows Server"), ("software", MARKER)]


@pytest.mark.asyncio
async def test_the_budget_is_per_server(pg_db_module):
    db = pg_db_module
    noisy, quiet = await baselined(db), await baselined(db)
    seed_events(db, noisy, BUDGET)

    await upload(db, noisy, {"software": named(20, version="2.0")})
    await upload(db, quiet, {"software": named(20, version="2.0")})

    assert len(software_events(db, noisy)) == BUDGET and len(markers(db, noisy)) == 1
    assert len(software_events(db, quiet)) == 20 and markers(db, quiet) == []


async def _suppress_setting(session: AsyncSession) -> str:
    from sqlalchemy import text
    return (await session.execute(text(
        "SELECT COALESCE(current_setting('zenplus.inventory_suppress', true), 'off')"))).scalar()


@pytest.mark.asyncio
async def test_capture_is_back_on_for_the_rest_of_the_transaction(pg_db_module):
    db = pg_db_module
    sid = await baselined(db)
    seed_events(db, sid, BUDGET)
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            await metrics._upsert_inventory(str(sid), {"software": named(20, version="2.0")}, session)
            assert await _suppress_setting(session) != "on"
            # A second snapshot of the same transaction (a batch can carry several) is judged by itself.
            await session.commit()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_capture_is_restored_when_the_software_section_raises_midway(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = await baselined(db)
    seed_events(db, sid, BUDGET)

    async def broken(*args, **kwargs):
        raise RuntimeError("prune exploded")

    monkeypatch.setattr(metrics, "_prune_software", broken)
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            with pytest.raises(RuntimeError, match="prune exploded"):
                async with session.begin_nested():   # the caller's own savepoint around the failure
                    await metrics._upsert_inventory(str(sid), {"software": named(20, version="2.0")}, session)
            assert await _suppress_setting(session) != "on"
    finally:
        await engine.dispose()


# ---- hostile strings never fail an upload

NASTY = "a\x00b\udc80c"
SCRUBBED = "ab?c"


@pytest.mark.parametrize("field, column", [
    ("package_name", "package_name"), ("version", "version"), ("vendor", "vendor"),
    ("architecture", "architecture"), ("source", "source"), ("install_location", "install_location"),
])
@pytest.mark.asyncio
async def test_nul_and_lone_surrogates_in_a_software_column_are_scrubbed(pg_db_module, field, column):
    db = pg_db_module
    sid = add_server(db)
    entry = sw("Plain Title", "1.0")
    entry[field] = NASTY

    await upload(db, sid, {"software": [entry, sw("Neighbour", "1.0")]})

    rows = software(db, sid)
    assert set(rows) == ({SCRUBBED, "Neighbour"} if column == "package_name" else {"Plain Title", "Neighbour"})
    stored = SCRUBBED if column == "package_name" else rows["Plain Title"][column]
    assert stored == SCRUBBED
    assert server_row(db, sid)["inventory_baselined_at"] is not None


@pytest.mark.parametrize("field, column", [
    ("name", "os_name"), ("version", "os_version"), ("kernel_or_build", "kernel_or_build"),
    ("architecture", "architecture"), ("fqdn", "fqdn"),
])
@pytest.mark.asyncio
async def test_nul_and_lone_surrogates_in_an_os_field_are_scrubbed(pg_db_module, field, column):
    db = pg_db_module
    sid = add_server(db)

    await upload(db, sid, {"os": {**WIN_2022, field: NASTY}, "software": [sw("App")]})

    assert db.scalar(f"SELECT {column} FROM servers WHERE id = $1", sid) == SCRUBBED
    assert identity(db, sid)["norm_rev"] == inventory_normalize.RULEPACK_REV   # and the identity still ran


@pytest.mark.asyncio
async def test_nul_and_lone_surrogates_in_the_cpu_model_are_scrubbed(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"hardware": {"cpu": {"model": NASTY, "logical_count": 4}}, "os": WIN_2022})
    assert db.scalar("SELECT cpu_model FROM servers WHERE id = $1", sid) == SCRUBBED


@pytest.mark.asyncio
async def test_oversized_and_non_text_os_values_do_not_fail_the_upload(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"os": {**WIN_2022, "version": "V" * 500, "fqdn": 12345, "architecture": ["x"]},
                           "software": [sw("App")]})
    assert db.scalar("SELECT os_version FROM servers WHERE id = $1", sid) == "V" * 128
    assert db.scalar("SELECT fqdn FROM servers WHERE id = $1", sid) is None
    assert db.scalar("SELECT architecture FROM servers WHERE id = $1", sid) is None

    await upload(db, sid, {"os": ["not", "an", "object"], "software": [sw("App")]})
    assert db.scalar("SELECT os_version FROM servers WHERE id = $1", sid) == "V" * 128


# ---- out-of-order snapshots (contract C2)

_CLOCK_BASE = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=10)


def hours(n: float) -> datetime:
    """An agent clock reading n hours after a base 10 hours ago (so never in the future)."""
    return _CLOCK_BASE + timedelta(hours=n)


@pytest.mark.asyncio
async def test_a_deferred_older_snapshot_neither_rewinds_the_tables_nor_writes_history(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    s1 = {"software": [sw("A", "1.0"), sw("B", "1.0"), sw("C", "1.0")]}
    s2 = {"software": [sw("A", "2.0"), sw("B", "1.0"), sw("D", "1.0")]}

    await upload(db, sid, s1, collected_at=hours(0))
    age(db, sid)
    await upload(db, sid, s2, collected_at=hours(1))
    assert sorted((e["action"], e["subject"]) for e in events(db, sid)) \
        == [("added", "D"), ("changed", "A"), ("removed", "C")]
    age(db, sid)

    await upload(db, sid, s1, collected_at=hours(0))   # the deferred copy of the first snapshot arrives

    assert {n: r["version"] for n, r in software(db, sid).items()} == {"A": "2.0", "B": "1.0", "D": "1.0"}
    assert len(events(db, sid)) == 3
    assert server_row(db, sid)["inventory_collected_at"] == hours(1)
    age(db, sid)

    await upload(db, sid, s2, collected_at=hours(1))   # the retry of the newest one: accepted, idempotent
    assert len(events(db, sid)) == 3
    assert {n: r["version"] for n, r in software(db, sid).items()} == {"A": "2.0", "B": "1.0", "D": "1.0"}


@pytest.mark.asyncio
async def test_a_deferred_older_os_snapshot_writes_no_downgrade(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    older = {**WIN_2022, "kernel_or_build": "10.0.20348.587 Build 20348.587"}
    newer = {**WIN_2022, "kernel_or_build": "10.0.20348.1850 Build 20348.1850"}
    await upload(db, sid, {"os": older}, collected_at=hours(0))
    await upload(db, sid, {"os": newer}, collected_at=hours(1))
    assert len(events(db, sid)) == 1

    await upload(db, sid, {"os": older}, collected_at=hours(0))

    assert len(events(db, sid)) == 1
    assert server_row(db, sid)["kernel_or_build"] == newer["kernel_or_build"]
    assert identity(db, sid)["os_version"] == "10.0.20348.1850"


@pytest.mark.asyncio
async def test_a_stale_snapshot_still_applies_its_other_sections(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("A", "2.0")]}, collected_at=hours(1))

    await upload(db, sid, {"software": [sw("A", "1.0")], "services": [{"service_name": "late-svc", "state": "running"}],
                           "hardware": {"cpu": {"model": "Late CPU"}}}, collected_at=hours(0))

    assert software(db, sid)["A"]["version"] == "2.0"
    assert db.scalar("SELECT state FROM server_service_inventory WHERE server_id = $1", sid) == "running"
    assert db.scalar("SELECT cpu_model FROM servers WHERE id = $1", sid) == "Late CPU"


@pytest.mark.asyncio
async def test_a_future_dated_stored_time_does_not_wedge_the_server(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("A", "1.0")]}, collected_at=hours(0))
    db.execute("UPDATE servers SET inventory_collected_at = now() + interval '30 days' WHERE id = $1", sid)
    age(db, sid)

    await upload(db, sid, {"software": [sw("A", "2.0")]}, collected_at=hours(1))

    assert software(db, sid)["A"]["version"] == "2.0"
    assert server_row(db, sid)["inventory_collected_at"] == hours(1)


@pytest.mark.asyncio
async def test_a_clock_stepped_back_is_accepted_again_after_a_day(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("A", "1.0")]}, collected_at=hours(5))
    age(db, sid)
    await upload(db, sid, {"software": [sw("A", "2.0")]}, collected_at=hours(0))   # within the window: ignored
    assert software(db, sid)["A"]["version"] == "1.0"

    db.execute("UPDATE servers SET inventory_updated_at = now() - interval '25 hours' WHERE id = $1", sid)
    await upload(db, sid, {"software": [sw("A", "2.0")]}, collected_at=hours(0))   # the newer one is a day old
    assert software(db, sid)["A"]["version"] == "2.0"
    assert server_row(db, sid)["inventory_collected_at"] == hours(0)


@pytest.mark.asyncio
async def test_the_first_snapshot_is_always_accepted_and_uploads_without_a_time_are_not_compared(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    assert server_row(db, sid)["inventory_collected_at"] is None
    await upload(db, sid, {"software": [sw("A", "1.0")]})
    assert server_row(db, sid)["inventory_collected_at"] is None
    await upload(db, sid, {"software": [sw("A", "1.0")]}, collected_at=hours(0))
    assert server_row(db, sid)["inventory_collected_at"] == hours(0)
    await upload(db, sid, {"software": [sw("A", "1.1")]})   # no time: applied, the stored time stays
    assert software(db, sid)["A"]["version"] == "1.1"
    assert server_row(db, sid)["inventory_collected_at"] == hours(0)


@pytest.mark.asyncio
async def test_hardware_only_uploads_never_move_the_snapshot_time(pg_db_module):
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"hardware": {"cpu": {"model": "CPU"}}}, collected_at=hours(3))
    assert server_row(db, sid)["inventory_collected_at"] is None


@pytest.mark.asyncio
async def test_overlapping_uploads_for_one_server_take_turns(pg_db_module):
    """The claim holds the servers row until commit, so the second upload sees the first one's baseline."""
    db = pg_db_module
    sid = add_server(db)
    engines = [create_async_engine(db.url, poolclass=NullPool) for _ in range(2)]
    try:
        first = AsyncSession(engines[0], expire_on_commit=False)
        second = AsyncSession(engines[1], expire_on_commit=False)
        await metrics._upsert_inventory(str(sid), {"software": [sw("Late", "1.0")]}, first, collected_at=hours(0))
        racing = asyncio.create_task(
            metrics._upsert_inventory(str(sid), {"software": [sw("Early", "1.0"), sw("Late", "1.0")]},
                                      second, collected_at=hours(1)))
        await asyncio.sleep(0.5)
        assert not racing.done()             # waiting on the servers row the first upload holds
        await first.commit()
        await asyncio.wait_for(racing, 10)
        await second.commit()
    finally:
        await first.close()
        await second.close()
        for engine in engines:
            await engine.dispose()

    # The first snapshot was a baseline and the second saw it committed: its new title is a real install.
    assert [(e["action"], e["subject"]) for e in events(db, sid)] == [("added", "Early")]


@pytest.mark.asyncio
async def test_deleting_a_server_during_a_software_only_upload_cannot_deadlock(pg_db_module, monkeypatch):
    """The claim locks the servers row first, so a delete waits instead of crossing the software locks."""
    db = pg_db_module
    sid = add_server(db)
    await upload(db, sid, {"software": [sw("A", "1.0")]}, collected_at=hours(0))
    age(db, sid)

    reached, proceed = asyncio.Event(), asyncio.Event()
    real_stamp = metrics._stamp_inventory

    async def paused(*args, **kwargs):          # between the software writes and the servers stamp
        reached.set()
        await proceed.wait()
        return await real_stamp(*args, **kwargs)

    monkeypatch.setattr(metrics, "_stamp_inventory", paused)
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            uploading = asyncio.create_task(metrics._upsert_inventory(
                str(sid), {"software": [sw("A", "2.0")]}, session, collected_at=hours(1)))
            await asyncio.wait_for(reached.wait(), 10)
            deleting = asyncio.create_task(asyncio.to_thread(
                db.execute, "DELETE FROM servers WHERE id = $1", sid))
            await asyncio.sleep(1.5)            # longer than deadlock_timeout (1s): a deadlock would have been raised
            assert not deleting.done()
            proceed.set()
            await asyncio.wait_for(uploading, 10)
            await session.commit()
        await asyncio.wait_for(deleting, 10)    # raises DeadlockDetectedError when the lock order is wrong
    finally:
        await engine.dispose()
    assert db.scalar("SELECT count(*) FROM servers WHERE id = $1", sid) == 0


# ---- one server's upload never touches another server

TABLES = ("server_software_inventory", "server_service_inventory",
          "server_filesystem_inventory", "server_network_interface_inventory")
FULL = {
    "os": WIN_2022,
    "software": [sw("Soft One"), sw("Soft Two")],
    "services": [{"service_name": "svc1", "state": "running"}, {"service_name": "svc2", "state": "stopped"}],
    "filesystems": [{"mount": "C:", "total_bytes": 100, "used_bytes": 10},
                    {"mount": "D:", "total_bytes": 200, "used_bytes": 20}],
    "network_interfaces": [{"if_name": "eth0", "mac_address": "00:11"}, {"if_name": "eth1", "mac_address": "00:22"}],
}


def age_everything(db: PgDatabase, server_id) -> None:
    for table in TABLES:
        db.execute(f"UPDATE {table} SET updated_at = now() - interval '1 hour' WHERE server_id = $1", server_id)


def server_state(db: PgDatabase, server_id) -> dict:
    state = {t: sorted(map(tuple, db.query(f"SELECT * FROM {t} WHERE server_id = $1", server_id)), key=repr)
             for t in TABLES}
    state["events"] = events(db, server_id)
    state["server"] = server_row(db, server_id)
    state["identity"] = identity(db, server_id)
    return state


@pytest.mark.asyncio
async def test_an_upload_for_one_server_leaves_every_other_servers_inventory_alone(pg_db_module):
    db = pg_db_module
    a, b, c = add_server(db), add_server(db), add_server(db)
    await upload(db, b, FULL)
    age_everything(db, b)
    before_b, before_c = server_state(db, b), server_state(db, c)
    assert all(len(before_b[t]) == 2 for t in TABLES) and before_c["server"]["inventory_updated_at"] is None

    await upload(db, a, {**FULL, "software": [sw("Only A")], "services": [{"service_name": "svcA"}],
                         "filesystems": [{"mount": "E:"}], "network_interfaces": [{"if_name": "ethA"}]})

    assert server_state(db, b) == before_b       # B's stale-looking rows were not pruned, stamped or rewritten
    assert server_state(db, c) == before_c       # C never uploaded: not baselined, not stamped, identity dirty
    assert set(software(db, a)) == {"Only A"}
    assert server_row(db, a)["inventory_baselined_at"] is not None


@pytest.mark.asyncio
async def test_a_capped_upload_for_one_server_does_not_prune_another(pg_db_module):
    db = pg_db_module
    a, b = add_server(db), add_server(db)
    await upload(db, b, {"software": named(30)})         # the same titles A is about to lose
    age_everything(db, b)
    before = server_state(db, b)
    await upload(db, a, {"software": named(1000)})
    age(db, a)
    await upload(db, a, {"software": named(1000, start=1)})   # Pkg 0000 goes: a boundary prune runs for A
    assert [e["subject"] for e in events(db, a) if e["action"] == "removed"] == ["Pkg 0000"]
    assert server_state(db, b) == before


@pytest.mark.asyncio
async def test_the_ordering_claim_and_the_budget_belong_to_the_uploading_server(pg_db_module):
    db = pg_db_module
    a, b = await baselined(db), await baselined(db)
    seed_events(db, b, BUDGET)
    before = server_state(db, b)

    await upload(db, a, {"software": named(20, version="2.0")}, collected_at=hours(2))

    assert server_state(db, b) == before
    assert server_row(db, a)["inventory_collected_at"] == hours(2)
    assert len(software_events(db, a)) == 20 and markers(db, a) == []   # B's budget is not A's


@pytest.mark.asyncio
async def test_identity_comes_from_the_uploading_servers_own_columns(pg_db_module):
    db = pg_db_module
    other = add_server(db, os_name="Ubuntu 22.04.4 LTS", os_version="22.04", kernel_or_build="5.15.0-105-generic")
    mine = add_server(db)
    await upload(db, mine, {"os": WIN_2022, "software": [sw("X")]})
    assert identity(db, mine)["os_family"] == "windows"
    assert identity(db, other)["norm_rev"] is None and identity(db, other)["os_family"] is None


@pytest.mark.asyncio
async def test_inline_identity_covers_the_hardware_columns_like_the_normaliser(pg_db_module):
    db = pg_db_module
    sid = add_server(db, hw_manufacturer="Dell Inc.", hw_model="PowerEdge R740", hw_serial="ABC1234")
    await upload(db, sid, {"os": WIN_2022, "software": [sw("X")]})
    expected = dataclasses.asdict(inventory_normalize.normalize_server(
        WIN_2022["name"], WIN_2022["version"], WIN_2022["kernel_or_build"], WIN_2022["architecture"],
        hw_manufacturer="Dell Inc.", hw_model="PowerEdge R740", hw_serial="ABC1234"))
    assert expected["hw_serial"] == "ABC1234" and expected["hw_model"] and expected["hw_vendor"]   # not vacuous
    row = identity(db, sid)
    assert {c: row[c] for c in metrics._IDENTITY_COLUMNS} == {c: expected[c] for c in metrics._IDENTITY_COLUMNS}


# ---- the surrounding ingest path

def _batch(inventory=None, samples=(), collected_at=None) -> AgentResultsBatch:
    now = datetime.now(timezone.utc)
    return AgentResultsBatch(
        agent_id=str(uuid.uuid4()), server_id=str(uuid.uuid4()), batch_id=str(uuid.uuid4()),
        sequence_start=1, sequence_end=1, agent_version="test", collected_at=collected_at or now, sent_at=now,
        metrics=list(samples), inventory=inventory or {},
    )


async def _ingest(db: PgDatabase, server_id, batch: AgentResultsBatch, monkeypatch) -> list[str]:
    """ingest_host_metric_batch with ClickHouse stubbed out; returns evaluated server ids."""
    evaluated: list[str] = []
    real = baseline_service.evaluate_server

    async def spy(session, sid, commit=True):
        evaluated.append(str(sid))
        return await real(session, sid, commit=commit)

    monkeypatch.setattr(metrics, "_insert_metric_groups", lambda *args, **kwargs: 0)
    monkeypatch.setattr(baseline_service, "evaluate_server", spy)
    engine = create_async_engine(db.url, poolclass=NullPool)
    try:
        async with AsyncSession(engine, expire_on_commit=False) as session:
            _, rejected, errors, _ = await metrics.ingest_host_metric_batch(
                str(uuid.uuid4()), str(server_id), batch, session)
            await session.commit()
    finally:
        await engine.dispose()
    assert rejected == 0 and errors == [], errors
    return evaluated


@pytest.mark.asyncio
async def test_baseline_evaluation_still_runs_after_a_software_snapshot(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    evaluated = await _ingest(db, sid, _batch({"software": [sw("A", "1.0")], "os": WIN_2022}), monkeypatch)
    assert evaluated == [str(sid)]
    assert set(software(db, sid)) == {"A"} and identity(db, sid)["os_family"] == "windows"

    # The same section arriving as an "inventory" metric sample takes the same path.
    sample = MetricSample(kind="inventory", timestamp=datetime.now(timezone.utc),
                          data={"applications": [sw("A", "1.0"), sw("B", "2.0")]})
    evaluated = await _ingest(db, sid, _batch(samples=[sample]), monkeypatch)
    assert evaluated == [str(sid)]
    assert set(software(db, sid)) == {"A", "B"}
    assert [e["action"] for e in events(db, sid)] == ["added"]


@pytest.mark.asyncio
async def test_baseline_evaluation_is_not_triggered_by_an_os_only_snapshot(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    evaluated = await _ingest(db, sid, _batch({"os": WIN_2022}), monkeypatch)
    assert evaluated == []
    assert server_row(db, sid)["inventory_updated_at"] is not None


@pytest.mark.asyncio
async def test_ingest_gives_the_inventory_writer_the_agents_own_snapshot_time(monkeypatch):
    seen: list[tuple[dict, datetime]] = []

    async def spy(server_id, inv, db, *, collected_at=None):
        seen.append((inv, collected_at))

    monkeypatch.setattr(metrics, "_upsert_inventory", spy)
    monkeypatch.setattr(metrics, "_insert_metric_groups", lambda *args, **kwargs: 0)
    collected, sampled = hours(2), hours(1)
    batch = _batch({"os": WIN_2022}, collected_at=collected,
                   samples=[MetricSample(kind="inventory", timestamp=sampled, data={"os": WIN_2022})])
    # sent_at is refreshed on every upload attempt of a spooled batch, so it cannot order snapshots.
    assert batch.sent_at != collected

    await metrics.ingest_host_metric_batch("agent", "server", batch, None)

    assert [when for _, when in seen] == [collected, sampled]


@pytest.mark.asyncio
async def test_a_deferred_batch_does_not_rewind_the_server_through_the_real_ingest(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)
    old = _batch({"software": [sw("A", "1.0"), sw("B", "1.0")]}, collected_at=hours(0))
    new = _batch({"software": [sw("A", "2.0"), sw("C", "1.0")]}, collected_at=hours(1))

    await _ingest(db, sid, old, monkeypatch)
    age(db, sid)
    await _ingest(db, sid, new, monkeypatch)
    assert len(events(db, sid)) == 3
    age(db, sid)
    await _ingest(db, sid, old, monkeypatch)       # the deferred batch finally gets through

    assert {n: r["version"] for n, r in software(db, sid).items()} == {"A": "2.0", "C": "1.0"}
    assert len(events(db, sid)) == 3
    assert server_row(db, sid)["inventory_collected_at"] == hours(1)


@pytest.mark.asyncio
async def test_an_inventory_sample_is_ordered_by_its_own_timestamp(pg_db_module, monkeypatch):
    db = pg_db_module
    sid = add_server(db)

    def sample(version, when):
        return MetricSample(kind="inventory", timestamp=when, data={"software": [sw("A", version)]})

    await _ingest(db, sid, _batch(samples=[sample("2.0", hours(3))]), monkeypatch)
    await _ingest(db, sid, _batch(samples=[sample("1.0", hours(1))]), monkeypatch)

    assert software(db, sid)["A"]["version"] == "2.0"
    assert server_row(db, sid)["inventory_collected_at"] == hours(3)


@pytest.mark.parametrize("target", ["_plan_software", "_inventory_packs_usable"])
@pytest.mark.asyncio
async def test_a_planning_failure_is_never_a_storage_error_for_the_agent(pg_db_module, monkeypatch, target):
    db = pg_db_module
    sid = add_server(db)

    def boom(*args, **kwargs):
        raise RuntimeError("planning exploded")

    monkeypatch.setattr(metrics, target, boom)
    # _ingest raises HostMetricStorageError (a 503 and an endless retry for the agent) if this regresses.
    await _ingest(db, sid, _batch({"software": named(5), "os": WIN_2022}), monkeypatch)

    rows = software(db, sid)
    assert len(rows) == 5 and {r["norm_rev"] for r in rows.values()} == {None}
    assert server_row(db, sid)["inventory_baselined_at"] is not None
