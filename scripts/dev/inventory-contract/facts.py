"""Facts the contract states about the code, read from the code.

Every limit, enum, default and column list that the document names is imported here from ``server/app``, so the
document cannot keep describing a value the code has changed. ``collect()`` returns ``{name: text}``; the template
refers to an entry as ``@@F:<name>@@``. Importing the service modules needs the server's Python requirements but no
database, no network and no environment variables (the modules only read the environment when they run a request).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "server"))


def bt(values) -> str:
    """`a`, `b`, `c`"""
    return ", ".join(f"`{v}`" for v in values)


def bt_and(values) -> str:
    """`a`, `b` and `c`"""
    items = [f"`{v}`" for v in values]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def num(n: int) -> str:
    return f"{n:,}"


def collect() -> dict[str, str]:
    from app.services import inventory_export as ex
    from app.services import inventory_normalize as nz
    from app.services import inventory_query as iq
    from app.services import inventory_service as svc
    from app.services import inventory_software_query as isq
    from app.services import inventory_trends_settings as its

    out: dict[str, str] = {}

    # paging and text
    out["max_text"] = str(iq.MAX_TEXT)
    out["max_page"] = num(iq.MAX_PAGE)
    out["max_page_size"] = str(iq.MAX_PAGE_SIZE)
    out["default_page_size"] = str(iq.DEFAULT_PAGE_SIZE)

    # vocabularies
    out["asset_sorts"] = bt(iq.SORT_KEYS)
    out["freshness_values"] = bt(iq.FRESHNESS)
    out["freshness_filters"] = bt(iq.FRESHNESS_FILTERS)
    out["health_keys"] = bt(iq.HEALTH_KEYS)
    out["sources"] = bt(iq.SOURCES)
    out["security_agent_values"] = bt(iq.SECURITY_AGENT_VALUES)
    out["module_classes"] = bt(iq.MODULE_CLASSES)
    out["categories"] = bt(nz.CATEGORIES)
    out["default_hidden"] = bt_and(svc.DEFAULT_SETTINGS["hide_categories_by_default"])
    out["ep_category"] = f"`{iq.ENDPOINT_PROTECTION_CATEGORY}`"
    out["ep_services"] = bt_and(iq.ENDPOINT_PROTECTION_SERVICES)
    out["ep_services_or"] = " or ".join(f"`{n}`" for n in iq.ENDPOINT_PROTECTION_SERVICES)
    out["catalog_sorts"] = bt(isq.CATALOG_SORTS)
    out["install_sorts"] = bt(isq.INSTALL_SORTS)
    out["memory_buckets"] = bt([b[0] for b in iq.MEMORY_BUCKETS] + [iq.UNKNOWN])
    out["disk_buckets"] = bt([b[0] for b in iq.DISK_BUCKETS] + [iq.UNKNOWN])
    out["change_categories"] = bt(iq.CHANGE_CATEGORIES)
    out["export_datasets"] = bt(ex.DATASETS)
    out["trend_metrics"] = bt(svc.SNAPSHOT_METRICS)
    out["snapshot_coverage_keys"] = bt(svc.SNAPSHOT_COVERAGE_KEYS)
    out["snapshot_health_keys"] = bt(svc.SNAPSHOT_HEALTH_KEYS)
    out["facet_names"] = bt(iq.FACET_NAMES)
    out["facet_caps"] = ", ".join(f"`{k}` {v}" for k, v in iq.FACET_LIMITS.items() if k in ("os_family", "os_product", "vendor", "model", "site", "tag", "source"))

    # defaults and limits
    d = svc.DEFAULT_SETTINGS
    out["default_fresh_server"] = str(d["fresh_hours"]["server"])
    out["default_fresh_device"] = str(d["fresh_hours"]["device"])
    out["default_stale_days"] = str(d["stale_days"])
    out["default_retention_days"] = str(d["change_retention_days"])
    out["default_snapshot_hour"] = str(d["snapshot_hour"])
    for name, (lo, hi) in its.SETTINGS_LIMITS.items():
        out[f"limit_{name}"] = f"{lo} to {hi}"
    out["trend_days"] = f"{its.MIN_TREND_DAYS} to {its.MAX_TREND_DAYS}"
    out["trend_default_days"] = str(its.DEFAULT_TREND_DAYS)
    out["trend_max_days"] = str(its.MAX_TREND_DAYS)
    out["trend_max_series"] = str(its.MAX_TREND_SERIES)

    # change feed
    out["window_min_year"] = str(iq.WINDOW_MIN.year)
    out["window_max_year"] = str(iq.WINDOW_MAX.year)
    out["max_series_days"] = str(iq.MAX_SERIES_DAYS)
    out["recent_changes"] = str(iq.RECENT_CHANGES)
    out["wide_scope_pct"] = str(int(round(iq.WIDE_SCOPE_FRACTION * 100)))

    # flyout
    out["max_module_rows"] = str(iq.MAX_MODULE_ROWS)
    out["max_server_software"] = num(iq.MAX_SERVER_SOFTWARE)

    # software
    out["max_ranked_products"] = num(isq.MAX_RANKED_PRODUCTS)
    out["install_sort_cap"] = num(isq.MAX_INSTALL_SORT_ROWS)
    out["evidence_limit"] = str(isq.EVIDENCE_LIMIT)
    out["distribution_top"] = str(isq.DISTRIBUTION_TOP)
    out["publisher_limit_default"] = str(isq.PUBLISHER_LIMIT_DEFAULT)
    out["publisher_limit_max"] = str(isq.PUBLISHER_LIMIT_MAX)
    out["no_version"] = f"`{isq.NO_VERSION}`"
    out["table_ttl_s"] = str(int(isq._TABLE_TTL_S))
    out["table_stale_min"] = str(int(isq._TABLE_STALE_S // 60))
    out["warm_interval_s"] = str(int(isq.WARM_INTERVAL_S))
    out["warm_env"] = f"`{isq.WARM_ENV}=0`"

    # summary
    out["summary_ttl_s"] = str(int(iq._SUMMARY_TTL_S))
    out["summary_cache_max"] = str(iq._CACHE_MAX)
    out["sprawl_exact_rows"] = num(iq._SPRAWL_EXACT_ROWS)
    out["summary_top_os"] = str(iq.SUMMARY_TOP_OS)
    out["summary_top_models"] = str(iq.SUMMARY_TOP_MODELS)

    # export
    out["export_cap"] = num(ex.EXPORT_CAP)
    out["export_batch"] = str(ex.BATCH)
    out["max_exports"] = str(ex.MAX_EXPORTS)
    out["max_exports_per_user"] = str(ex.MAX_EXPORTS_PER_USER)
    out["retry_after_s"] = str(ex.RETRY_AFTER_S)
    out["consumer_stall_s"] = str(int(ex.CONSUMER_STALL_S))
    for dataset, cols in (("assets", ex.ASSET_COLUMNS), ("software", ex.SOFTWARE_COLUMNS), ("installations", ex.INSTALL_COLUMNS),
                          ("os", ex.OS_COLUMNS), ("hardware", ex.HW_COLUMNS), ("changes", ex.CHANGE_COLUMNS)):
        out[f"columns_{dataset}"] = ", ".join(cols)

    # background loops
    out["snapshot_settle_min"] = str(svc.SNAPSHOT_SETTLE_S // 60)
    out["snapshot_hold_max_h"] = str(svc.SNAPSHOT_HOLD_MAX_S // 3600)
    out["snapshot_minute"] = str(svc.SNAPSHOT_MINUTE)
    out["snapshot_retention_years"] = str(svc.SNAPSHOT_RETENTION_YEARS)
    out["normalize_interval_s"] = str(int(svc.NORMALIZE_INTERVAL_S))
    out["keying_rows_per_tick"] = num(svc.SOFTWARE_BATCH * svc.MAX_SOFTWARE_BATCHES_PER_TICK)
    out["keying_minutes_at_2m"] = str(round((2_000_000 / (svc.SOFTWARE_BATCH * svc.MAX_SOFTWARE_BATCHES_PER_TICK)) * svc.NORMALIZE_INTERVAL_S / 60 / 10) * 10)
    out["capture_key"] = f"`{svc.CAPTURE_KEY}`"
    out["rulepack_rev"] = str(nz.RULEPACK_REV)
    return out


if __name__ == "__main__":
    for k, v in collect().items():
        print(f"{k} = {v}")
