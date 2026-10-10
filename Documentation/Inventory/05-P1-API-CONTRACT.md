# 05 — Phase 1 API Contract (Inventory read endpoints)

*Status: implemented in `server/app/api/v1/inventory.py`, `server/app/services/inventory_query.py`, `inventory_software_query.py`, `inventory_trends_settings.py`, `inventory_export.py` and `server/app/schemas/inventory.py` · 2026-10-09 · regenerated from the final code after the Phase 1 review fix round. This is the contract the dashboard is built against. Where it differs from the sketches in `02-ARCHITECTURE-DATA-MODEL-API.md` §8, this file wins (phase 1 scope decisions S1 to S6 and the review-round decisions X1 to X5 below). Every JSON example in this file is a real response, copied from the golden fixtures in `dashboard/src/pages/inventory/fixtures/` (lists are cut short where the text says so; no value is edited).*

**This file is generated.** Do not edit it: edit `scripts/dev/inventory-contract/contract.template.md` (prose) or the code it reads, and run `python scripts/dev/inventory-contract/build_contract.py` (`--check` says whether the committed file is stale; the README next to the generator explains the inputs). The limits, enums, defaults and column lists named below are read from the server code when the file is generated, and the examples are cut from the golden fixtures, so neither can drift from the implementation without a test or the generator noticing.

The golden fixtures are produced by `server/tests/test_inventory_fixtures.py` from a deterministic seeded fleet (60 servers, 85 network devices in the asset union plus 5 SNMP twins of servers, 24 visible software products, 56 change events in the 7-day window, clock pinned to `2026-10-08T12:00:00Z`). The test compares the server's real response with the checked-in files and fails on drift. Regenerate them with `ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py`, then regenerate this file.

Contents: 1 Conventions · 2 The asset filter vocabulary · 3 Endpoints (3.1 to 3.7 the asset read model: summary, assets, facets, flyout, os, hardware, changes; 3.8 to 3.14 software catalog, publishers, product, installations, trends, settings, export; 3.15 the touchpoints outside `/inventory`) · 4 The drill-down map · 5 Health, freshness, software and the other definitions · 6 Fixtures and measured performance · 7 What later waves add

---

## 1. Conventions

| Topic | Rule |
|---|---|
| Base path | `/api/v1/inventory/…`. Every endpoint is a `GET` except `PUT /settings`. |
| Permission | `inventory.view` on every route (router default `policy("inventory")`) except two that declare their own guard: `GET /export` needs `inventory.export` and `PUT /settings` needs `inventory.manage`. Permissions are **not implied at request time** (the role editor offers the implied ones, `PERMISSION_IMPLIES`): a role with only `inventory.export` can export and cannot list; a viewer lists everything and exports nothing. Without the permission: `403 {"detail":"Insufficient permissions (inventory)"}` (the module default) or `403 {"detail":"Insufficient permissions"}` (an explicit guard). Unauthenticated: `401`. |
| Scope | Every response covers the caller's **visible assets** only (the user's own `scope_tags` united with their role's `scope_tags`; `system.admin` is never scoped; an untagged asset is invisible to a scoped user; tags match case-insensitively, any-of). Aggregate responses carry `scope: {restricted, tags}` so the UI can say "Showing assets tagged …" and withhold fleet-wide series. A scope that matches nothing is not an error: every list is empty and every count is zero. `data_status` (below) is scope-aware as well. |
| Asset address | `'<kind>:<uuid>'`, for example `device:6f3127bd-a812-5f82-95d0-259847c912ed`. `kind` is `server` or `device`. The asset's `key` field is exactly this string. |
| One machine, one asset | A device that a server links to through `servers.device_id` (the SNMP side of the same machine) is **not** an asset of its own: it is counted once, as the server, and enriches the server's vendor, model and serial. `summary.assets.linked_devices` says how many servers have one. Only the link `servers.device_id` folds a device into a server: an SNMP device that merely has the same address is an asset of its own, so a machine that is both a server and an unlinked device is counted twice (once of each kind) until it is linked. The link is made by a discovery import that holds both rows or by setting `device_id` on the server through the API (`POST /servers`, `PATCH /servers/{id}`); agent enrolment never sets it, the Servers page has no control for it, and Inventory never guesses it from an address. |
| Timestamps | UTC ISO-8601 with a `Z` suffix and whole seconds: `2026-10-08T12:00:00Z`. `generated_at` is the server clock at the time the figures were computed (for `/summary` that can be up to 60 s ago, see caching; for the software catalog it is the age of the table the figures were read from). Ages are whole seconds (`*_age_s`), measured against `generated_at`. |
| Nulls | A field is **always present**; a missing value is `null`, never omitted, so `null` ("unknown") is distinguishable from "this API version does not have the field". Lists are never `null`, except where a field below says so (`hardware.servers.virtualization`, `hardware.matrix`). |
| Unknown is a value | Nothing is dropped for being unknown. `os_family` is the string `"unknown"` when the OS was not identified; the other OS fields are `null`. An unknown value is addressed in a filter as the literal `unknown` (`os_family`, `os_product`, `os_release`, `os_version`, `vendor`, `model`, `cpu_model`, `memory`, `disks`, `virtualization`, `module_model`, and `security_agent`, which has `unknown` as one of its three values), never as a free string. **Endpoint protection** that cannot be judged (no software list yet, a list the agent cut short, rows not yet normalised) is `unknown`, not `missing`; an asset that cannot report an inventory by design is `not_applicable`, not `never` (section 5). |
| `data_status` | `GET /summary`, `/software`, `/software/publishers` and `/software/{product_key}` carry `data_status: {normalising: boolean, pending_software_rows: number, pending_identity_rows: number}`: the rows of the caller's scope that the normaliser has not (re)keyed yet and WILL pick up (a software row with no product key or keyed by an older rule-pack revision; an identity row that is dirty or old). A software row the normalise loop never selects (one with no `source` on a server that is not a Windows machine by `os_type` or by its OS name: it waits for that server's next upload, which carries the source) is not backlog: the count uses the loop's own eligibility rule, so the message cannot stay up for a row nothing will process; such a server is `unknown` for endpoint protection with the reason `no_software_list` (section 5). While `normalising` is `true` the figures that depend on them (software, endpoint protection, OS) are incomplete and every Inventory page says so ("Normalising existing software lists; figures are incomplete"). An empty catalog with `pending_software_rows > 0` means rows are not keyed yet, not an empty estate. The count is cheap (partial indexes; one probe per process for rows of an older revision). |
| Errors | `401` unauthenticated · `403` no `inventory.view` · `404` flyout of an unknown, malformed or out-of-scope asset (one body for all three, `{"detail":"Asset not found"}`) · `404` software product page or installation list of a product that is unknown or has no host in the caller's scope (one body for both, `{"detail":"Product not found"}`) · `422` invalid parameter · `429` an export while 2 exports already run in the API process or the caller already has 1 (header `Retry-After: 10`) · `503 {"detail":"The audit log is unavailable, so nothing was done. Try again; if it persists, contact an administrator."}` when the audit entry of `PUT /settings` or of an export cannot be stored (nothing is saved, nothing is sent). A `422` `detail` is **either a string** (a vocabulary error raised by the service, for example `"product_version needs product"`) **or the standard FastAPI array** of `{loc, msg, type}` objects (a value outside an enum or range, or a text longer than its limit). Show the string, or `detail[0].msg`. No endpoint returns `5xx` for bad input; the only `5xx` the contract names is the audited `503` above. |
| Caching | `/summary` is cached (60 s, in process, per scope and settings, built once for concurrent callers). The software catalog and its publisher facet read a per-scope table that is cached for 60 s; the whole-estate table is also kept fresh in the background by a warmer (section 3.8). Everything else is computed per request. No `Cache-Control` headers are set except `no-store` on the export; the dashboard's React Query `staleTime`/`refetchInterval` is the client cache (doc 03 §8). |
| Settings used | `system_settings['inventory']` (doc 02 §9, section 3.13): `fresh_hours` (server 24 h, device 2 h by default), `stale_days` (7), `hide_categories_by_default` (`update`, `driver`, `os-component` and `runtime`), `trends_enabled`. They are read on every call (the summary cache key includes them); saving them drops the summary and catalog caches of the API process that served the `PUT` (other workers keep theirs for up to 60 s). |
| Pagination | `page` (1-based, ≤ 100,000) and `page_size` (default 50, **max 500**). A page past the end is `200` with `items: []` and the real `total`. |
| Touchpoints | Phase 1 also adds fields and filters to two existing endpoints outside `/inventory` (`GET /devices`, `GET /servers/{id}/software`); they are listed in section 3.15 because the Inventory pages and the links from the Devices and Servers pages rely on them. |

### Phase 1 scope decisions that shape this contract

| ID | Decision |
|---|---|
| S1 | Routes shipped in P1: `/inventory`, `/assets`, `/os`, `/software`, `/software/:productKey`, `/hardware`, `/changes`, `/settings`. The Changes page moved from P2 to P1. No Lifecycle or Compliance data (`summary.lifecycle` is `null`). |
| S3 | `missing_model`, `missing_serial` and `unknown_vendor` apply to **network devices only** (servers have no hardware identity until P3, so counting them would be noise). `missing_security_agent` and `software_truncated` apply to servers only. `missing_os_version`, `stale` and `never` apply to both. |
| S4 | `GET /trends` (section 3.12) serves the daily snapshots. The Overview shows KPI sparklines only when `days_with_data` is at least 3, and never to a restricted user (`restricted: true`, no series). |
| S6 | The URL of every list page carries its filters as query parameters **identical** to the API's (section 2). A drill link is `/inventory/assets?<params>`; the page forwards them unchanged to `GET /inventory/assets` and `/inventory/assets/facets`. |

### Decisions of the review round that shape this contract

| ID | Decision |
|---|---|
| X1 | **Endpoint protection.** The software category `security` stays the broad bucket (VPN and SSE clients, scanners, password managers, Sysmon, Wazuh, management agents, ...). A separate category `endpoint-protection` ("Endpoint protection") holds only antivirus, endpoint-protection-platform and EDR engines and sensors. The Assets filter `security_agent` (`present` \| `missing` \| `unknown`) and the health key `missing_security_agent` keep their names (vocabulary and URLs are pinned) but are **defined on `endpoint-protection` plus, for built-in Microsoft Defender, a running service** from the allowlist `WinDefend` and `Sense` in `server_service_inventory`. A server whose software list is cut (`software_truncated`), absent or not yet normalised is `unknown`, never `missing`. User-visible copy says "Endpoint protection". |
| X2 | `data_status` on the summary and the software responses (Conventions above). |
| X3 | Freshness has a fifth value, `not_applicable`, for assets that cannot report an inventory by design (section 5). They stay in the list, are counted, and are drillable with `freshness=not_applicable`, but are **excluded** from `coverage.fresh_pct`, from `never`, from `stale_or_never`, from the four identity health predicates and from the all-clear; `summary.health.not_collected_by_design` counts them. |
| X4 | `GET /changes` carries `history_start` (when change capture began on this installation) and `retention_days`; the flyout carries `changes.first_change_at`, so a "View all N" link with `since=first_change_at` lands on exactly N events. |
| X5 | `runtime` joins the default hidden software categories (`update`, `driver`, `os-component` and `runtime`). The toolbar label of the pages is data-driven from `hidden_categories`. |

---

## 2. The asset filter vocabulary

One set of names, used by `GET /inventory/assets`, `GET /inventory/assets/facets`, the export endpoint (`dataset=assets`) and the address bar of `/inventory/assets`. Every filter is optional; an empty string counts as unset; filters combine with AND. Text is trimmed, and a text longer than 200 characters is rejected with `422` (the limit applies to the raw string before it is trimmed; `os_family` allows 64, `product` 160, `source` and `virtualization` 32). The one exception is `tag`, which is repeatable and has no length check: a tag is cut to 200 characters, duplicates are dropped and at most 20 are used.

| Parameter | Type · values | Matches |
|---|---|---|
| `q` | string, at most 200 characters | Free text, case-insensitive contains (`%` and `_` are literal) over name, IP, serial, model, vendor, OS product and OS version. |
| `kind` | `server` \| `device` | The asset kind. |
| `os_family` | string, case-insensitive; `unknown` | Normalised OS family: `windows`, `linux`, `fortios`, `panos`, `ios`, `ios-xe`, `nx-os`, `junos`, `tmos`, `aos-cx`, `other`, `unknown`, … (the values present are listed by the `os_family` facet). |
| `os_product` | string; `unknown` | OS product, case-insensitive (`Windows Server`, `FortiOS`, `IOS XE`). `unknown` = no product. |
| `os_release` | string; `unknown` | Release, case-insensitive exact (`2019`, `7.2`, `17.9`). `unknown` = no release. |
| `os_version` | string; `unknown` | Version, matched by its **equivalence key**: trimmed, lower-cased, trailing `.0` parts dropped, so `7.2` also selects `7.2.0` (and `10.0` is `10`) while `1.10` and `1.100` stay different (`10.0.17763.5696`, `17.9.4a`). `unknown` = no version. |
| `vendor` | string; `unknown` | Vendor label, case-insensitive exact (`Cisco`). `unknown` selects every asset with no vendor, whatever its freshness (the health predicate `unknown_vendor` is the worklist: the same assets minus those that cannot report by design). A server takes its vendor, model and serial from its linked SNMP device when the agent has none. |
| `model` | string; `unknown` | Hardware model, case-insensitive exact; `unknown` selects every asset with no model (`missing_model` is the worklist). |
| `site` | string | Server: the site name. Device: its location text. Case-insensitive exact. There is no `unknown` sentinel: the text `unknown` matches a site of that name. |
| `tag` | string, **repeatable** | The asset must carry **every** tag given (case-insensitive). Up to 20. |
| `source` | `agent`, `snmp`, `wmi`, `winrm`, `ssh`, `controller`, `icmp` | The asset has this collection source (an asset can have several: a server that is also polled over SNMP has `agent` and `snmp`). Not validated against the list: the text is trimmed and lower-cased, and a value no asset has (`bogus`) is `200` with an empty list, not a `422`. |
| `freshness` | `fresh`, `aging`, `stale`, `never`, `not_applicable`, `stale_or_never` | Inventory freshness bucket (section 5). `stale_or_never` is the union "not reporting"; it does not include `not_applicable`. |
| `health` | `missing_os_version`, `missing_model`, `missing_serial`, `unknown_vendor`, `stale`, `never`, `software_truncated`, `missing_security_agent` | One inventory-health predicate (section 5). Single-valued: two predicates cannot be combined. |
| `product` | string (software `product_key`) | Servers that have this product installed. Implies `kind=server`. |
| `product_version` | string, **requires `product`** | Only servers with this version of the product, matched by equivalence key as `os_version` is (`422` without `product`). |
| `cpu_model` | string; `unknown` | Servers by CPU model (exact, case-insensitive). Servers only. |
| `memory` | `le8`, `le16`, `le32`, `le64`, `le128`, `gt128`, `unknown` | Servers by installed memory bucket: ≤ 8, 9–16, 17–32, 33–64, 65–128, > 128 GiB. |
| `disks` | `1`, `2`, `3-4`, `5-8`, `ge9`, `unknown` | Servers by physical disk count. |
| `virtualization` | string; `unknown` | Servers by virtualisation (`vmware`, `hyper-v`, `physical`, …). `unknown` = none reported. |
| `security_agent` | `present`, `missing`, `unknown` | Servers by endpoint-protection state (X1). `present` = a software row of category `endpoint-protection` or a running `WinDefend` or `Sense` service; `missing` = a complete, normalised software list that shows neither; `unknown` = nothing can be said (no software list yet, a list the agent cut at its cap, or rows the normaliser has not keyed yet). The three **partition the servers**: `present` + `missing` + `unknown` = `kind=server`. |
| `module_class` | `chassis`, `backplane`, `module`, `powerSupply`, `fan`, `cpu`, `stack` | Network devices that have an ENTITY-MIB component of this class. Devices only. |
| `module_model` | string; `unknown` | Network devices that have a component of this exact model (case-sensitive; `unknown` = a component with no model). Combine with `module_class` for a class + model group. |

`product`, `cpu_model`, `memory`, `disks`, `virtualization` and `security_agent` select servers only (a device never matches); `module_class` and `module_model` select devices only. The rows from `cpu_model` down are the drill-down targets of the Overview and Hardware pages. `dashboard/src/pages/inventory/filters.ts` is the client's copy of this vocabulary.

List-only parameters: `sort` (section 3.2), `order` (`asc` \| `desc`), `page`, `page_size`.

---

## 3. Endpoints

### 3.1 `GET /api/v1/inventory/summary`

The Overview figures for the caller's visible assets. Permission `inventory.view`. No parameters. Cached for **60 s** per (scope, settings) in the API process; `generated_at` shows when the figures were computed. Cold cost is a handful of aggregates over the asset union and `server_software_inventory` (budget < 300 ms at 5,000 servers × 400 packages and 10,000 devices; measured numbers are in section 6.5).

```ts
interface SummaryResponse {
  generated_at: string
  scope: { restricted: boolean; tags: string[] }      // tags lowercased and sorted; [] when unrestricted
  assets: {
    total: number                                     // = server + device (the asset union, SNMP twins not counted twice)
    server: number
    device: number
    linked_devices: number                            // servers that also have an SNMP device row; informational, not a list total
  }
  coverage: {
    fresh: number; aging: number; stale: number; never: number; not_applicable: number   // sum = assets.total
    fresh_pct: number | null                          // fresh / (total - not_applicable), one decimal, never 100.0 while one asset is not fresh; null when nothing can report
    fresh_hours: { server: number; device: number }   // the thresholds in force
    stale_days: number
  }
  os: {
    families: number                                  // family nodes of GET /os (all kinds), the 'unknown' family included
    releases: number                                  // release nodes of GET /os (all kinds): (family, product, release) triples, a missing release included
    unknown: number                                   // assets whose family is 'unknown'
    top: { family: string; product: string | null; release: string | null; count: number }[]   // up to 10, known families, count desc
  }
  software: {
    products: number                                  // distinct product keys, hidden categories excluded
    installs: number                                  // software rows, hidden categories excluded
    publishers: number                                // distinct publisher keys, plus one when some product has none ('unknown' in the facet)
    hidden_categories: string[]                       // settings.hide_categories_by_default
    top: SummaryProduct[]                             // up to 8, by hosts desc
    sprawl: SummaryProduct[]                          // up to 8, by distinct versions desc, products with more than one version
    sprawl_basis: { scope: 'all_products' | 'top_installed'; candidates: number; products: number }   // what `sprawl` was ranked over
    security_agent_coverage: { covered: number; missing: number; unknown: number; total: number }     // servers by endpoint protection (X1); covered + missing + unknown = total = assets.server
  }
  hardware: {
    devices: number                                   // = assets.device
    vendors: number                                   // distinct non-null vendors among devices
    models: number                                    // distinct (vendor, model) pairs with both known
    top_models: { vendor: string; model: string; count: number }[]   // up to 8, devices only
  }
  changes_7d: {
    since: string                                     // generated_at minus 7 days
    total: number
    software: { added: number; removed: number; changed: number; upgraded: number; downgraded: number }
    os: number; firmware: number; hardware: number
  }
  health: Record<HealthKey, number> & {               // the eight predicates of section 5, plus the assets that cannot report by design
    not_collected_by_design: { count: number }        // = coverage.not_applicable; drills to freshness=not_applicable
  }
  data_status: { normalising: boolean; pending_software_rows: number; pending_identity_rows: number }   // Conventions, X2
  lifecycle: null                                     // P4
}
interface SummaryProduct { product_key: string; name: string; category: string | null; hosts: number; versions: number }
```

Example (`summary.json`; `os.top`, `software.top`, `software.sprawl` and `hardware.top_models` cut to two items):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "scope": {"restricted": false, "tags": []},
  "assets": {"total": 145, "server": 60, "device": 85, "linked_devices": 5},
  "coverage": {
    "fresh": 105,
    "aging": 12,
    "stale": 8,
    "never": 12,
    "not_applicable": 8,
    "fresh_pct": 76.6,
    "fresh_hours": {"server": 24, "device": 2},
    "stale_days": 7
  },
  "os": {
    "families": 12,
    "releases": 33,
    "unknown": 17,
    "top": [
      {"family": "windows", "product": "Windows Server", "release": "2022", "count": 18},
      {"family": "windows", "product": "Windows Server", "release": "2019", "count": 14}
    ]
  },
  "software": {
    "products": 24,
    "installs": 337,
    "publishers": 21,
    "hidden_categories": ["update", "driver", "os-component", "runtime"],
    "top": [
      {"product_key": "google-chrome", "name": "Google Chrome", "category": "browser", "hosts": 41, "versions": 3},
      {"product_key": "microsoft-edge", "name": "Microsoft Edge", "category": "browser", "hosts": 41, "versions": 2}
    ],
    "sprawl": [
      {"product_key": "git", "name": "Git", "category": "development", "hosts": 19, "versions": 5},
      {"product_key": "7-zip", "name": "7-Zip", "category": "productivity", "hosts": 34, "versions": 4}
    ],
    "sprawl_basis": {"scope": "all_products", "candidates": 24, "products": 24},
    "security_agent_coverage": {"covered": 36, "missing": 13, "unknown": 11, "total": 60}
  },
  "hardware": {
    "devices": 85,
    "vendors": 7,
    "models": 19,
    "top_models": [{"vendor": "Cisco", "model": "C9200L-24P-4G", "count": 6}, {"vendor": "Cisco", "model": "WS-C2960X-48TS-L", "count": 6}]
  },
  "changes_7d": {
    "since": "2026-10-01T12:00:00Z",
    "total": 56,
    "software": {"added": 12, "removed": 12, "changed": 15, "upgraded": 12, "downgraded": 1},
    "os": 4,
    "firmware": 11,
    "hardware": 2
  },
  "health": {
    "missing_os_version": 14,
    "missing_model": 13,
    "missing_serial": 15,
    "unknown_vendor": 5,
    "stale": 8,
    "never": 12,
    "software_truncated": 2,
    "missing_security_agent": 13,
    "not_collected_by_design": {"count": 8}
  },
  "data_status": {"normalising": false, "pending_software_rows": 0, "pending_identity_rows": 0},
  "lifecycle": null
}
```

Notes:

- `software.top` and `sprawl` exclude the hidden categories. `hosts` counts distinct servers; `versions` counts distinct non-empty versions, by equivalence key (`7.2` and `7.2.0` are one version; section 5). `top` is ordered by hosts, then installs, then key; `sprawl` by versions, then installs, then key. A missing version is not a version.
- **`sprawl_basis`.** The sprawl ranking is exact (`scope: "all_products"`) up to 400,000 visible software rows. Above that the cold-summary budget does not allow reading every (product, version) pair, so the ranking covers the `candidates` most installed products of the `products` there are, and `scope` is `top_installed`: the panel must say "ranks the N most installed of M products" (the host ranking `top` is exact either way).
- **`security_agent_coverage`** partitions *all* servers into `covered` (`security_agent=present`), `missing` (`security_agent=missing`, equal to `health.missing_security_agent`) and `unknown` (`security_agent=unknown`: no software list, a cut list or rows not yet normalised; the number is shown, never folded into either side). `total` is the number of servers (`assets.server`), not a filter; the UI renders "of N" as text.
- **Freshness and the all-clear.** `fresh + aging + stale + never + not_applicable = assets.total`. `fresh_pct` is over the assets that can report (`total − not_applicable`) and is `null` when there are none. An asset that cannot report by design is neither fresh nor a gap, so it neither lowers the percentage nor blocks an all-clear; `health.not_collected_by_design.count` says how many there are and links to them.
- `health.missing_os_version`, `missing_model`, `missing_serial` and `unknown_vendor` skip the assets that cannot report by design; `stale` and `never` exclude them by definition.
- `changes_7d` counts change events of the visible assets in the last seven days. `software.upgraded`/`downgraded` count `changed` rows whose two versions can be ordered (section 3.7).
- `data_status` is scope-aware: for a restricted caller it counts the rows of the assets the caller can see.
- The doc 02 §8 sketch also listed `health.unmatched_software` and `lifecycle`: the first arrives with the catalog (P2); `lifecycle` is `null` until P4.

Errors: `401`, `403`. Caching: 60 s per `(sorted scope tags | unrestricted, fresh_hours, stale_days, hide_categories_by_default)`, at most 64 entries per process; concurrent cold callers wait for one shared build.

### 3.2 `GET /api/v1/inventory/assets`

One page of the asset union, filtered, sorted and paged on the server. Permission `inventory.view`.

| Parameter | Type · default | Notes |
|---|---|---|
| every filter of section 2 | | |
| `sort` | one of `name`, `kind`, `ip`, `asset_type`, `status`, `vendor`, `model`, `serial`, `os_family`, `os_product`, `os_release`, `os_version`, `os`, `site`, `last_seen`, `inventory_at`, `freshness` · default `name` | `os` = OS product, then version. `os_release`, `os_version` and `os` are numeric-aware (`7.2` before `7.10` before `17.9`, missing values last). `freshness` orders fresh, aging, stale, never, not_applicable. `name`, `vendor`, `model`, `os_product` and `site` sort case-insensitively. A value outside the list is `422`. |
| `order` | `asc` \| `desc` · default `asc` | |
| `page` | integer ≥ 1 · default `1` | |
| `page_size` | integer 1–500 · default `50` | |

The order is **total**: the requested key (nulls always last, in both directions), then `kind`, then `id`. Paging a list whose rows do not change never repeats or skips a row.

```ts
interface AssetList { generated_at: string; items: AssetRow[]; total: number; page: number; page_size: number; sort: string; order: 'asc' | 'desc' }

interface AssetRow {
  key: string                       // '<kind>:<uuid>', the address
  kind: 'server' | 'device'
  id: string
  name: string                      // server display name / device hostname
  ip: string | null
  asset_type: string | null         // server: its os_type (windows, linux, unknown); device: its type (firewall, switch, …)
  status: string | null             // the monitoring status the existing pages show
  vendor: string | null
  model: string | null
  serial: string | null
  os_family: string                 // never null: 'unknown' when not identified
  os_product: string | null
  os_release: string | null
  os_version: string | null
  os_build: string | null
  os_edition: string | null
  os_source: string | null          // where the OS values came from: agent, profile, entity, template, template_stale, sysdescr, managed, ssh
  hw_source: string | null          // where vendor/model/serial came from: agent, device, template
  site: string | null               // server: site name; device: location text
  tags: string[]
  sources: string[]                 // agent, snmp, wmi, winrm, ssh, controller, icmp (may hold several)
  last_seen: string | null          // reachability
  inventory_at: string | null       // when the identity data was last refreshed (server: last accepted software/OS section; device: last successful system-info poll)
  inventory_age_s: number | null
  freshness: 'fresh' | 'aging' | 'stale' | 'never' | 'not_applicable'   // not_applicable: the asset cannot report an inventory by design (section 5)
  software_truncated: boolean       // the agent's list hit its cap on the last upload (servers)
}
```

Example (`assets.json`, `page_size=50`, cut to three of the 50 rows: a device the poller has never read (`never`), a ping-only device that cannot report by design (`not_applicable`), and a server reporting through its agent (`fresh`)):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "items": [
    {
      "key": "device:6f3127bd-a812-5f82-95d0-259847c912ed",
      "kind": "device",
      "id": "6f3127bd-a812-5f82-95d0-259847c912ed",
      "name": "edge-unk-01",
      "ip": "10.20.3.12",
      "asset_type": "switch",
      "status": "up",
      "vendor": null,
      "model": null,
      "serial": null,
      "os_family": "unknown",
      "os_product": null,
      "os_release": null,
      "os_version": null,
      "os_build": null,
      "os_edition": null,
      "os_source": null,
      "hw_source": null,
      "site": null,
      "tags": ["prod", "dc-east", "team-net"],
      "sources": ["snmp"],
      "last_seen": null,
      "inventory_at": null,
      "inventory_age_s": null,
      "freshness": "never",
      "software_truncated": false
    },
    {
      "key": "device:6753bb19-1cd7-5c7e-abec-3627b9aea696",
      "kind": "device",
      "id": "6753bb19-1cd7-5c7e-abec-3627b9aea696",
      "name": "edge-unk-02",
      "ip": "10.20.3.13",
      "asset_type": "switch",
      "status": "unknown",
      "vendor": null,
      "model": null,
      "serial": null,
      "os_family": "unknown",
      "os_product": null,
      "os_release": null,
      "os_version": null,
      "os_build": null,
      "os_edition": null,
      "os_source": null,
      "hw_source": null,
      "site": "HQ / rack 7",
      "tags": ["prod", "dc-west", "team-a"],
      "sources": ["icmp"],
      "last_seen": null,
      "inventory_at": null,
      "inventory_age_s": null,
      "freshness": "not_applicable",
      "software_truncated": false
    },
    {
      "key": "server:dcf69f0f-2cf4-542c-b230-c41f7404a1f7",
      "kind": "server",
      "id": "dcf69f0f-2cf4-542c-b230-c41f7404a1f7",
      "name": "srv-w16-01",
      "ip": "10.10.0.42",
      "asset_type": "windows",
      "status": "healthy",
      "vendor": null,
      "model": null,
      "serial": null,
      "os_family": "windows",
      "os_product": "Windows Server",
      "os_release": "2016",
      "os_version": "10.0.14393.6709",
      "os_build": "14393.6709",
      "os_edition": "Standard",
      "os_source": "agent",
      "hw_source": null,
      "site": "Branch-1",
      "tags": ["dev", "team-a"],
      "sources": ["agent"],
      "last_seen": "2026-10-08T06:04:00Z",
      "inventory_at": "2026-10-08T06:04:00Z",
      "inventory_age_s": 21360,
      "freshness": "fresh",
      "software_truncated": false
    }
  ],
  "total": 145,
  "page": 1,
  "page_size": 50,
  "sort": "name",
  "order": "asc"
}
```

`total` is the number of rows the filters select (not the page size). The `total` of a list equals the number on the page that linked to it (section 4).

Errors: `401`, `403`, `422` (bad enum, a text longer than 200 characters, `page`/`page_size` out of range, `product_version` without `product`).

### 3.3 `GET /api/v1/inventory/assets/facets`

Counts per facet value for the asset filters. Permission `inventory.view`. Parameters: every filter of section 2 (no sort or paging).

**Each facet counts over every active filter except its own**, so a count is exactly the `total` the list would show after choosing that value ("CSAM-style"). The one exception is `tag`: tags narrow cumulatively, so choosing a second tag intersects with the first and the tag facet keeps its own filter. `total` is the list total for all active filters.

```ts
interface FacetsResponse {
  generated_at: string
  total: number
  facets: Record<'kind' | 'os_family' | 'os_product' | 'vendor' | 'model' | 'site' | 'tag' | 'source' | 'freshness' | 'health', FacetGroup>
}
interface FacetGroup { distinct: number; items: { value: string; label: string; count: number }[] }   // items capped (see below); distinct is the uncapped number
```

- `kind`, `freshness` and `health` list their whole vocabulary in a fixed order, **zero counts included**. `kind` always has its 2 entries, `freshness` its 5 (fresh, aging, stale, never, `not_applicable` with the label "Not applicable") and `health` the 8 predicates (`label` equals the key). The `freshness` facet has no `stale_or_never` entry: it is a union, not a bucket.
- `os_family` lists every family including `unknown` (labelled like the OS page: "FortiOS", "IOS XE"); `os_product` lists `unknown` for assets with no product (`label` "Unknown"). `vendor`, `model` and `site` list known values only: assets without a vendor or model are selected with `vendor=unknown` and `model=unknown` (and listed by the `unknown_vendor` and `missing_model` predicates, which skip the assets that cannot report), and an unset site is simply absent.
- **Names are merged case-insensitively** for `os_product`, `vendor`, `model` and `site`: `Cisco` and `CISCO` are one entry whose `value` and `label` are the most common spelling (ties: byte order) and whose `count` is the `total` of the list filtered with that value. A `tag` facet `value` is the lower-cased tag (its `label` is a spelling found in the data).
- Other facets are ordered by count desc, then label. Caps: `os_family` 40, `os_product` 60, `vendor` 60, `model` 100, `site` 100, `tag` 100, `source` 10. Use `distinct` to show "N more".
- `value` is what to put in the URL; `label` is what to show.

Example (`facets.json`, every facet cut to three items):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "total": 145,
  "facets": {
    "kind": {"distinct": 2, "items": [{"value": "server", "label": "server", "count": 60}, {"value": "device", "label": "device", "count": 85}]},
    "os_family": {
      "distinct": 12,
      "items": [
        {"value": "windows", "label": "Windows", "count": 46},
        {"value": "ios-xe", "label": "IOS XE", "count": 18},
        {"value": "unknown", "label": "Unknown", "count": 17}
      ]
    },
    "os_product": {
      "distinct": 14,
      "items": [
        {"value": "Windows Server", "label": "Windows Server", "count": 41},
        {"value": "unknown", "label": "Unknown", "count": 21},
        {"value": "IOS XE", "label": "IOS XE", "count": 18}
      ]
    },
    "vendor": {
      "distinct": 8,
      "items": [
        {"value": "Cisco", "label": "Cisco", "count": 28},
        {"value": "Fortinet", "label": "Fortinet", "count": 14},
        {"value": "HPE Aruba Networking", "label": "HPE Aruba Networking", "count": 10}
      ]
    },
    "model": {
      "distinct": 20,
      "items": [
        {"value": "C9200L-24P-4G", "label": "C9200L-24P-4G", "count": 6},
        {"value": "WS-C2960X-48TS-L", "label": "WS-C2960X-48TS-L", "count": 6},
        {"value": "6300M", "label": "6300M", "count": 5}
      ]
    },
    "site": {
      "distinct": 12,
      "items": [
        {"value": "Branch-1", "label": "Branch-1", "count": 18},
        {"value": "DR", "label": "DR", "count": 18},
        {"value": "HQ", "label": "HQ", "count": 18}
      ]
    },
    "tag": {
      "distinct": 9,
      "items": [
        {"value": "prod", "label": "prod", "count": 98},
        {"value": "team-a", "label": "team-a", "count": 50},
        {"value": "team-net", "label": "team-net", "count": 49}
      ]
    },
    "source": {
      "distinct": 5,
      "items": [
        {"value": "snmp", "label": "snmp", "count": 84},
        {"value": "agent", "label": "agent", "count": 50},
        {"value": "icmp", "label": "icmp", "count": 6}
      ]
    },
    "freshness": {
      "distinct": 5,
      "items": [
        {"value": "fresh", "label": "fresh", "count": 105},
        {"value": "aging", "label": "aging", "count": 12},
        {"value": "stale", "label": "stale", "count": 8}
      ]
    },
    "health": {
      "distinct": 8,
      "items": [
        {"value": "missing_os_version", "label": "missing_os_version", "count": 14},
        {"value": "missing_model", "label": "missing_model", "count": 13},
        {"value": "missing_serial", "label": "missing_serial", "count": 15}
      ]
    }
  }
}
```

Errors: `401`, `403`, `422`.

### 3.4 `GET /api/v1/inventory/assets/{kind}/{id}`

The flyout payload for one asset. Permission `inventory.view`. `kind` is `server` or `device`, `id` the asset's UUID (the `id` of an `AssetRow`).

**`404 {"detail":"Asset not found"}` for an unknown id, a malformed id, a wrong kind, an asset outside the caller's scope, and the SNMP side of a server (it is the server's, not an asset of its own).** The response is byte-identical for all of them: the endpoint is not an existence oracle.

```ts
interface AssetDetail {
  generated_at: string
  asset: AssetRow                                     // the same row the list shows
  identity: IdentityValue[]                           // always the same 9 fields, in this order: vendor, model, serial,
                                                      //   os_family, os_product, os_release, os_version, os_build, os_edition
  raw: Record<string, string | null>                  // the raw evidence the values were normalised from (see below)
  server: ServerBody | null                           // set for kind=server
  device: DeviceBody | null                           // set for kind=device
  changes: {
    items: ChangeRow[]                                 // the 10 newest events of the asset (and of its SNMP twin)
    total: number                                      // all of its recorded events
    first_change_at: string | null                     // the oldest of them; null when there are none (X4)
  }
  links: { server_id: string | null; device_id: string | null }   // ids only: /servers/:id, /devices/:id (permission-gated in the UI)
}
interface IdentityValue {
  field: string; label: string
  value: string | null                                 // null = unknown (os_family 'unknown' is reported as null here)
  source: string | null                                // badge: agent | snmp | template | entity-mib | ssh
  source_detail: string | null                         // the stored source: agent, profile, entity, template, template_stale, device, sysdescr, managed
  observed_at: string | null; age_s: number | null    // when this value was last refreshed; a template-sourced value is as old as its template sample
}
interface ServerBody {
  os_name: string | null; architecture: string | null; hostname: string | null; fqdn: string | null
  cpu_model: string | null; cpu_cores: number | null; cpu_physical_cores: number | null
  memory_total_bytes: number | null
  disks: Record<string, unknown>[]                     // as the agent reported them ({model, size_bytes, …})
  virtualization: string | null; bios_version: string | null; bios_date: string | null; boot_time: string | null
  baselined: boolean                                   // a first software snapshot has been accepted
  software: {
    total: number                                      // all software rows of the server (at most 5,001 are read)
    shown: number                                      // = top.length (10 at most)
    hidden: number                                     // rows in hidden categories (`update`, `driver`, `os-component` and `runtime` by default)
    truncated: boolean                                 // the agent's list was cut at its cap
    top: { package_name: string; product_key: string | null; product: string; version: string | null; publisher: string | null
           category: string | null; architecture: string | null; source: string | null; install_date: string | null; first_seen_at: string | null }[]
    security_agent: {                                  // endpoint protection (X1): the answer of the Assets filter security_agent
      state: 'present' | 'missing' | 'unknown'
      present: boolean | null                          // the same as a nullable bool for older readers: true / false / null (unknown)
      reason: 'no_software_list' | 'list_truncated' | 'normalising' | 'no_service_list' | null   // why it is unknown (section 5); null otherwise
      products: string[]                               // the server's endpoint-protection software titles
      services: string[]                               // the running Defender services that count (`WinDefend` and `Sense`) when no title does
    }
  }
}
interface DeviceBody {
  profile: string | null; group: string | null; device_type: string | null; sys_object_id: string | null
  controller_managed: boolean
  modules: { class: string; class_label: string; model: string | null; count: number
             items: { ent_index: number; name: string | null; serial: string | null; hw_revision: string | null; fw_revision: string | null }[] }[]
  modules_truncated: boolean                           // more than 500 components: the list is cut
  ha: { mode: string | null; role: string | null; peer: string | null; sync: string | null; observed_at: string | null; age_s: number | null } | null
}
```

`server.software.top` lists non-hidden products with the endpoint-protection ones first, then the broader `security` category, then by name. `security_agent` reads *all* of the server's rows, not just the listed ones, and `state` is always the value `GET /assets?security_agent=` would give this server (a test holds the two to the same answer for every server of the fleet). `modules` groups the ENTITY-MIB components of class chassis, backplane, module, powerSupply, fan, cpu and stack by class and model (ports and sensors are not shown), each with its serials. `ha` comes from monitoring-template values (FortiGate, PAN-OS, F5) and is `null` for devices without them. `raw` is `{os_name, os_version, kernel_or_build, hw_manufacturer, hw_model, hw_serial}` for a server and `{vendor, model, os_version, firmware_version, serial_number, sys_descr}` for a device (`sys_descr` cut to 512 characters).

A server's flyout merges its SNMP twin: vendor, model and serial may come from the device (`hw_source: "device"`), `links.device_id` is the twin, and the twin's change events appear in `changes`.

Example, a server with an SNMP twin (`asset-server.json`; `identity` cut to 3 of 9, `disks`, `software.top` and `changes.items` cut):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "asset": {
    "key": "server:faa42e3e-82ca-544d-bc72-34a51c3fdf6b",
    "kind": "server",
    "id": "faa42e3e-82ca-544d-bc72-34a51c3fdf6b",
    "name": "srv-w19-03",
    "ip": "10.10.0.30",
    "asset_type": "windows",
    "status": "healthy",
    "vendor": "Dell",
    "model": "PowerEdge R650",
    "serial": "DELL00003",
    "os_family": "windows",
    "os_product": "Windows Server",
    "os_release": "2019",
    "os_version": "10.0.17763.5696",
    "os_build": "17763.5696",
    "os_edition": "Standard",
    "os_source": "agent",
    "hw_source": "device",
    "site": "Branch-1",
    "tags": ["dev", "pci", "team-edge"],
    "sources": ["agent", "snmp"],
    "last_seen": "2026-10-08T10:40:00Z",
    "inventory_at": "2026-10-08T10:40:00Z",
    "inventory_age_s": 4800,
    "freshness": "fresh",
    "software_truncated": false
  },
  "identity": [
    {
      "field": "vendor",
      "label": "Vendor",
      "value": "Dell",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-10-08T10:40:00Z",
      "age_s": 4800
    },
    {
      "field": "model",
      "label": "Model",
      "value": "PowerEdge R650",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-10-08T10:40:00Z",
      "age_s": 4800
    },
    {
      "field": "serial",
      "label": "Serial number",
      "value": "DELL00003",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-10-08T10:40:00Z",
      "age_s": 4800
    }
  ],
  "raw": {
    "os_name": "Microsoft Windows Server 2019 Standard",
    "os_version": "1809",
    "kernel_or_build": "10.0.17763.5696 Build 17763.5696",
    "hw_manufacturer": null,
    "hw_model": null,
    "hw_serial": null
  },
  "server": {
    "os_name": "Microsoft Windows Server 2019 Standard",
    "architecture": "x86_64",
    "hostname": "srv-w19-03",
    "fqdn": null,
    "cpu_model": "AMD EPYC 7543 32-Core Processor",
    "cpu_cores": 8,
    "cpu_physical_cores": 4,
    "memory_total_bytes": 8589934592,
    "disks": [{"model": "Disk 0", "size_bytes": 480103981056}, {"model": "Disk 1", "size_bytes": 960207962112}],
    "virtualization": "physical",
    "bios_version": null,
    "bios_date": null,
    "boot_time": "2026-09-15T12:00:00Z",
    "baselined": true,
    "software": {
      "total": 14,
      "shown": 8,
      "hidden": 6,
      "truncated": false,
      "top": [
        {
          "package_name": "CrowdStrike Falcon Sensor",
          "product_key": "crowdstrike-falcon-sensor",
          "product": "CrowdStrike Falcon Sensor",
          "version": "7.15.18513",
          "publisher": "CrowdStrike, Inc.",
          "category": "endpoint-protection",
          "architecture": "x64",
          "source": "hklm",
          "install_date": "2026-09-03T12:00:00Z",
          "first_seen_at": "2026-05-12T12:00:00Z"
        },
        {
          "package_name": "Git",
          "product_key": "git",
          "product": "Git",
          "version": "2.46.2",
          "publisher": "The Git Development Community",
          "category": "development",
          "architecture": "x64",
          "source": "hklm",
          "install_date": "2026-01-11T12:00:00Z",
          "first_seen_at": "2026-08-13T12:00:00Z"
        }
      ],
      "security_agent": {"present": true, "state": "present", "reason": null, "products": ["CrowdStrike Falcon Sensor"], "services": []}
    }
  },
  "device": null,
  "changes": {
    "items": [
      {
        "id": 49,
        "observed_at": "2026-10-07T21:50:00Z",
        "asset_key": "device:a8d490b8-97c2-5a52-bc70-e1181b21f217",
        "asset_kind": "device",
        "asset_id": "a8d490b8-97c2-5a52-bc70-e1181b21f217",
        "asset_name": "twin-srv-w19-03",
        "asset_present": true,
        "server_id": "faa42e3e-82ca-544d-bc72-34a51c3fdf6b",
        "category": "firmware",
        "action": "changed",
        "subject": "os_version",
        "subject_key": null,
        "old_value": "1.0.0",
        "new_value": "1.1.0",
        "label": null,
        "pair": null,
        "marker": false,
        "snapshot_truncated": false,
        "detail": {"os_source": "profile"}
      }
    ],
    "total": 3,
    "first_change_at": "2026-10-06T13:38:48Z"
  },
  "links": {"server_id": "faa42e3e-82ca-544d-bc72-34a51c3fdf6b", "device_id": "a8d490b8-97c2-5a52-bc70-e1181b21f217"}
}
```

Example, a switch with modules (`asset-device.json`; `identity` cut to 2 of 9, module groups cut to 3 with two items each, `changes.items` cut to one):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "asset": {
    "key": "device:ce9a854d-ce49-5f31-9e6a-9790ac0bae6b",
    "kind": "device",
    "id": "ce9a854d-ce49-5f31-9e6a-9790ac0bae6b",
    "name": "sw-c9-04",
    "ip": "10.20.1.10",
    "asset_type": "switch",
    "status": "up",
    "vendor": "Cisco",
    "model": "C9300-24T",
    "serial": "FCW2312L004",
    "os_family": "ios-xe",
    "os_product": "IOS XE",
    "os_release": "17.12",
    "os_version": "17.12.3",
    "os_build": null,
    "os_edition": null,
    "os_source": "profile",
    "hw_source": "device",
    "site": "DR / rack 8",
    "tags": ["prod", "dc-east", "team-net"],
    "sources": ["snmp"],
    "last_seen": "2026-10-08T10:41:00Z",
    "inventory_at": "2026-10-08T10:41:00Z",
    "inventory_age_s": 4740,
    "freshness": "fresh",
    "software_truncated": false
  },
  "identity": [
    {
      "field": "vendor",
      "label": "Vendor",
      "value": "Cisco",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-10-08T10:41:00Z",
      "age_s": 4740
    },
    {
      "field": "model",
      "label": "Model",
      "value": "C9300-24T",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-10-08T10:41:00Z",
      "age_s": 4740
    }
  ],
  "raw": {
    "vendor": "Cisco",
    "model": "C9300-24T",
    "os_version": "17.12.03",
    "firmware_version": "17.12.03",
    "serial_number": "FCW2312L004",
    "sys_descr": "Cisco IOS Software [Cupertino], Catalyst L3 Switch Software (CAT9K_IOSXE), Version 17.12.03, RELEASE SOFTWARE (fc3)"
  },
  "server": null,
  "device": {
    "profile": "Cisco IOS-XE",
    "group": "Switching",
    "device_type": "switch",
    "sys_object_id": "1.3.6.1.4.1.9.1.2530",
    "controller_managed": false,
    "modules": [
      {
        "class": "chassis",
        "class_label": "Chassis",
        "model": "C9300-24T",
        "items": [{"ent_index": 1, "name": "Chassis", "serial": "FCW2312L004", "hw_revision": null, "fw_revision": null}],
        "count": 1
      },
      {
        "class": "powerSupply",
        "class_label": "Power supply",
        "model": "PWR-C1-715WAC-P",
        "items": [
          {"ent_index": 2, "name": "Switch 1 - Power Supply A", "serial": "DCA00025A", "hw_revision": null, "fw_revision": null},
          {"ent_index": 3, "name": "Switch 1 - Power Supply B", "serial": "DCA00025B", "hw_revision": null, "fw_revision": null}
        ],
        "count": 2
      }
    ],
    "modules_truncated": false,
    "ha": null
  },
  "changes": {
    "items": [
      {
        "id": 13,
        "observed_at": "2026-10-05T22:27:00Z",
        "asset_key": "device:ce9a854d-ce49-5f31-9e6a-9790ac0bae6b",
        "asset_kind": "device",
        "asset_id": "ce9a854d-ce49-5f31-9e6a-9790ac0bae6b",
        "asset_name": "sw-c9-04",
        "asset_present": true,
        "server_id": null,
        "category": "firmware",
        "action": "changed",
        "subject": "IOS XE",
        "subject_key": null,
        "old_value": "17.6.4",
        "new_value": "17.9.4a",
        "label": null,
        "pair": null,
        "marker": false,
        "snapshot_truncated": false,
        "detail": {"os_family": "ios-xe", "os_source": "profile", "os_release": "17.9"}
      }
    ],
    "total": 1,
    "first_change_at": "2026-10-05T22:27:00Z"
  },
  "links": {"server_id": null, "device_id": "ce9a854d-ce49-5f31-9e6a-9790ac0bae6b"}
}
```

Example, a FortiGate with HA state from its template values (`asset-device-ha.json`; `identity` cut to 3 of 9):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "asset": {
    "key": "device:c8c15245-72b8-5a06-9210-3f47094d22f4",
    "kind": "device",
    "id": "c8c15245-72b8-5a06-9210-3f47094d22f4",
    "name": "fg-04",
    "ip": "10.20.0.13",
    "asset_type": "firewall",
    "status": "down",
    "vendor": "Fortinet",
    "model": "FortiGate-60F",
    "serial": "FG60FTK20000004",
    "os_family": "fortios",
    "os_product": "FortiOS",
    "os_release": "7.4",
    "os_version": "7.4.4",
    "os_build": "2662",
    "os_edition": null,
    "os_source": "template",
    "hw_source": "device",
    "site": "HQ / rack 4",
    "tags": ["prod", "dc-east", "pci", "team-edge"],
    "sources": ["snmp"],
    "last_seen": "2026-09-27T12:00:00Z",
    "inventory_at": "2026-09-27T12:00:00Z",
    "inventory_age_s": 950400,
    "freshness": "stale",
    "software_truncated": false
  },
  "identity": [
    {
      "field": "vendor",
      "label": "Vendor",
      "value": "Fortinet",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-09-27T12:00:00Z",
      "age_s": 950400
    },
    {
      "field": "model",
      "label": "Model",
      "value": "FortiGate-60F",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-09-27T12:00:00Z",
      "age_s": 950400
    },
    {
      "field": "serial",
      "label": "Serial number",
      "value": "FG60FTK20000004",
      "source": "snmp",
      "source_detail": "device",
      "observed_at": "2026-09-27T12:00:00Z",
      "age_s": 950400
    }
  ],
  "raw": {
    "vendor": "Fortinet",
    "model": "FortiGate-60F",
    "os_version": null,
    "firmware_version": null,
    "serial_number": "FG60FTK20000004",
    "sys_descr": "FortiGate-60F v7.4.4,build2662,240614 (GA.M)"
  },
  "server": null,
  "device": {
    "profile": "FortiGate",
    "group": "Firewalls",
    "device_type": "firewall",
    "sys_object_id": "1.3.6.1.4.1.12356.101.1.1000",
    "controller_managed": false,
    "modules": [
      {
        "class": "chassis",
        "class_label": "Chassis",
        "model": "FortiGate-60F",
        "items": [{"ent_index": 1, "name": "Chassis", "serial": "FG60FTK20000004", "hw_revision": null, "fw_revision": null}],
        "count": 1
      }
    ],
    "modules_truncated": false,
    "ha": {"mode": "Active-Passive", "role": "Primary", "peer": null, "sync": null, "observed_at": "2026-09-27T12:00:00Z", "age_s": 950400}
  },
  "changes": {"items": [], "total": 0, "first_change_at": null},
  "links": {"server_id": null, "device_id": "c8c15245-72b8-5a06-9210-3f47094d22f4"}
}
```

Errors: `401`, `403`, `404` (above).

### 3.5 `GET /api/v1/inventory/os`

The OS and firmware tree: family → product → release → version, with asset counts, the latest observed version per release and the build-currency split. Permission `inventory.view`.

| Parameter | Type · default | Notes |
|---|---|---|
| `kind` | `server` \| `device` · all | Limit the tree to servers or to network devices. |

```ts
interface OsResponse {
  generated_at: string
  scope: { restricted: boolean; tags: string[] }
  kind: 'server' | 'device' | null                     // echo of the parameter
  total: number                                        // assets in the tree; = the Assets list total for the same kind
  families: {
    family: string; label: string; count: number       // 'unknown' is a family (always last); label e.g. 'FortiOS'
    products: {
      product: string | null; count: number            // null = known family, no product
      releases: {
        release: string | null; count: number          // null = no release
        latest_version: string | null                  // the highest observed version of the release (comparator-ordered)
        distinct_versions: number                      // non-null versions (equivalence keys: 7.2 and 7.2.0 count once)
        currency: {                                    // null when no observed version parses or the comparator refuses to order them
          latest: number; previous: number; older: number; unknown: number     // assets on the latest observed version (N), on the one before it (N-1), on any earlier one (two or more behind); unknown = no or unparseable version
          latest_version: string; previous_version: string | null
        } | null
        versions: { version: string | null; count: number; raw_example: string | null }[]   // comparable versions newest first, then the unparseable ones, null last
      }[]
    }[]
  }[]
}
```

Rules:

- **Every node's `count` equals the `total` of the Assets list filtered by the path to the node** (`os_family`, then `os_product`, `os_release`, `os_version`, `unknown` for a null level) plus `kind` when the tree is filtered. `currency.latest + previous + older + unknown` equals the release `count`.
- **A node is one value of the key its filter compares.** Products and releases group case-insensitively (`IOS XE` and `ios xe` are one node, labelled with the most common spelling, ties in byte order); versions group by equivalence key (`7.2` and `7.2.0` are one node, labelled with the longer spelling). `currency.latest` and `currency.previous` therefore drill to `os_version=<latest_version>` and `os_version=<previous_version>` and equal the list totals. A missing level is a node of its own (family `unknown`, product, release or version `null`), counted and drillable; `summary.os.families` and `summary.os.releases` are the numbers of family and release nodes of this tree for all kinds.
- **Build currency.** `latest` is the highest observed version of the release, `previous` the one before it, and `older` every earlier comparable version: *two or more versions behind the newest observed*. `unknown` counts the assets with no version or one the scheme cannot parse.
- Order: families by count desc (`unknown` last), products by count desc (null last), releases newest first by natural order (null last), versions newest first by the family's comparator (versions the comparator cannot parse follow in natural order, null last).
- "Latest" is **latest observed** on the visible assets, not the vendor's latest; lifecycle-aware currency arrives in P4. The comparator refuses cross-train comparisons (classic IOS), in which case `currency` is `null` and the versions fall back to natural order; the same holds when no observed version parses.
- `raw_example` is, for network firmware, one raw string that differs from the normalised `version` (for example `v7.2.8,build1639,240614 (GA.M)`), so the UI can show how the value was normalised; `null` when the raw value equals the version (all Windows builds).

Example (`os.json`, families cut to `fortios` and `unknown`, releases cut to two, versions cut to two):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "scope": {"restricted": false, "tags": []},
  "kind": null,
  "total": 145,
  "families": [
    {
      "family": "fortios",
      "label": "FortiOS",
      "count": 14,
      "products": [
        {
          "product": "FortiOS",
          "count": 14,
          "releases": [
            {
              "release": "7.4",
              "count": 4,
              "latest_version": "7.4.4",
              "distinct_versions": 1,
              "currency": {"latest": 4, "previous": 0, "older": 0, "unknown": 0, "latest_version": "7.4.4", "previous_version": null},
              "versions": [{"version": "7.4.4", "count": 4, "raw_example": "v7.4.4,build2662,240614 (GA.M)"}]
            },
            {
              "release": "7.2",
              "count": 7,
              "latest_version": "7.2.8",
              "distinct_versions": 2,
              "currency": {"latest": 5, "previous": 2, "older": 0, "unknown": 0, "latest_version": "7.2.8", "previous_version": "7.2.5"},
              "versions": [
                {"version": "7.2.8", "count": 5, "raw_example": "v7.2.8,build1639,240614 (GA.M)"},
                {"version": "7.2.5", "count": 2, "raw_example": "v7.2.5,build1517,230606 (GA.F)"}
              ]
            }
          ]
        }
      ]
    },
    {
      "family": "unknown",
      "label": "Unknown",
      "count": 17,
      "products": [
        {
          "product": null,
          "count": 17,
          "releases": [
            {
              "release": null,
              "count": 17,
              "latest_version": null,
              "distinct_versions": 0,
              "currency": null,
              "versions": [{"version": null, "count": 17, "raw_example": null}]
            }
          ]
        }
      ]
    }
  ]
}
```

Errors: `401`, `403`, `422` (`kind`).

### 3.6 `GET /api/v1/inventory/hardware`

Network vendors and models, a model × firmware matrix, ENTITY-MIB module groups and server hardware distributions. Permission `inventory.view`.

| Parameter | Type · default | Notes |
|---|---|---|
| `vendor` | string · none | Builds `matrix` for this vendor (case-insensitive; `matrix.vendor` echoes the canonical label). A vendor with no devices gives an empty matrix. Without it `matrix` is `null`. |

```ts
interface HardwareResponse {
  generated_at: string
  scope: { restricted: boolean; tags: string[] }
  network: {
    devices: number                                    // devices in the asset union
    models_known: number                               // distinct known (vendor, model) pairs = summary.hardware.models
    unknown_model_devices: number                      // devices with no model, whatever their vendor
    vendors: {
      vendor: string | null                            // null = devices with no vendor
      count: number
      models: number                                   // = models_known
      models_known: number
      unknown_model_devices: number                    // devices of this vendor that report no model
      firmware_versions: number                        // distinct OS/firmware versions by equivalence key
      serial_missing: number                           // = total of the list health=missing_serial for this vendor
      not_collected: number                            // devices that cannot report by design; in neither serial_missing nor the percentage
      serial_coverage_pct: number | null               // over the devices that can report, one decimal, never 100.0 while a serial is missing; null when none can
      modules: number
    }[]
    models: {
      vendor: string; model: string | null             // model null = vendor known, model not
      unknown: boolean                                 // true for that row (model is null)
      count: number; firmware_versions: number; serial_missing: number
      not_collected: number; serial_coverage_pct: number | null; modules: number
    }[]
  }
  matrix: {
    vendor: string
    releases: string[]                                 // columns: newest first, 'unknown' last
    models: { model: string | null; unknown: boolean; count: number; cells: { release: string; count: number }[] }[]   // only non-empty cells
  } | null
  modules: { class: string; class_label: string; model: string | null; count: number; devices: number; with_serial: number }[]
  servers: {
    total: number
    cpu_models: { value: string; label: string; count: number }[]        // top 25 by count, then 'unknown'
    cpu_models_more: { models: number; servers: number }                  // what the top 25 leave out
    memory: { value: string; label: string; count: number }[]            // the six buckets always (zeros included), then 'unknown' when any
    disks: { value: string; label: string; count: number }[]             // the five buckets always, then 'unknown' when any
    virtualization: { value: string; label: string; count: number }[] | null   // null until some server reports a value
  }
}
```

Rules:

- A vendor row counts every device of that vendor; its model rows add up to it, including an **unknown-model row** (`model: null`, `unknown: true`) for the devices of that vendor that report no model. The devices with no vendor are one `vendor: null` row and have no model rows. `models_known` and `unknown_model_devices` let a page say "5 models plus 4 devices with no model" without counting rows.
- Vendors, models and releases are grouped case-insensitively (`Cisco` and `CISCO` are one vendor, labelled with the most common spelling). `firmware_versions` is the number of distinct normalised OS/firmware versions by equivalence key. `serial_missing` is the `total` of the Assets list `health=missing_serial` for the row's filters, which skips the devices that cannot report by design (`not_collected`); `serial_coverage_pct` is the **serial coverage**, `(reporting − serial_missing) / reporting` in percent with one decimal over the devices that *can* report, and `null` when none can. `modules` counts ENTITY-MIB components (class `module`) of those devices.
- Network hardware describes **devices only** (S3). Server hardware lives under `servers`.
- `modules[]` counts **components**, `devices` the distinct devices that have them, `with_serial` the components that report a serial. Order: by class (chassis, backplane, module, powerSupply, fan, cpu, stack), then models with a name before the unnamed one, then count desc.
- Memory buckets are binary gigabytes (`le8` ≤ 8 GiB … `gt128` > 128 GiB), disk buckets are `1`, `2`, `3-4`, `5-8`, `ge9`.
- Drill targets: the `vendor: null` row opens `kind=device&vendor=unknown`; an unknown-model row opens `kind=device&vendor=<vendor>&model=unknown`; `not_collected` adds `freshness=not_applicable` to the row's filters (section 4).

Example (`hardware.json`, requested with `vendor=Cisco`; vendors, models, matrix, modules, CPUs and buckets cut):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "scope": {"restricted": false, "tags": []},
  "network": {
    "devices": 85,
    "models_known": 19,
    "unknown_model_devices": 17,
    "vendors": [
      {
        "vendor": "Cisco",
        "count": 28,
        "models": 5,
        "models_known": 5,
        "unknown_model_devices": 3,
        "firmware_versions": 10,
        "serial_missing": 5,
        "not_collected": 0,
        "serial_coverage_pct": 82.1,
        "modules": 8
      },
      {
        "vendor": "Fortinet",
        "count": 14,
        "models": 4,
        "models_known": 4,
        "unknown_model_devices": 2,
        "firmware_versions": 4,
        "serial_missing": 2,
        "not_collected": 2,
        "serial_coverage_pct": 83.3,
        "modules": 0
      }
    ],
    "models": [
      {
        "vendor": "Cisco",
        "model": "C9200L-24P-4G",
        "unknown": false,
        "count": 6,
        "firmware_versions": 2,
        "serial_missing": 0,
        "not_collected": 0,
        "serial_coverage_pct": 100.0,
        "modules": 0
      },
      {
        "vendor": "Cisco",
        "model": "WS-C2960X-48TS-L",
        "unknown": false,
        "count": 6,
        "firmware_versions": 3,
        "serial_missing": 1,
        "not_collected": 0,
        "serial_coverage_pct": 83.3,
        "modules": 0
      }
    ]
  },
  "matrix": {
    "vendor": "Cisco",
    "releases": ["17.12", "17.9", "17.6", "17.3"],
    "models": [
      {"model": "C9200L-24P-4G", "unknown": false, "count": 6, "cells": [{"release": "17.9", "count": 3}, {"release": "17.3", "count": 3}]},
      {"model": "WS-C2960X-48TS-L", "unknown": false, "count": 6, "cells": [{"release": "15.2", "count": 6}]}
    ]
  },
  "modules": [
    {"class": "chassis", "class_label": "Chassis", "model": "C9200L-24P-4G", "count": 6, "devices": 6, "with_serial": 6},
    {"class": "chassis", "class_label": "Chassis", "model": "WS-C2960X-48TS-L", "count": 6, "devices": 6, "with_serial": 5}
  ],
  "servers": {
    "total": 60,
    "cpu_models": [
      {"value": "Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz", "label": "Intel(R) Xeon(R) Gold 6338 CPU @ 2.00GHz", "count": 18},
      {"value": "AMD EPYC 7543 32-Core Processor", "label": "AMD EPYC 7543 32-Core Processor", "count": 17}
    ],
    "cpu_models_more": {"models": 0, "servers": 0},
    "memory": [
      {"value": "le8", "label": "Up to 8 GB", "count": 6},
      {"value": "le16", "label": "9 to 16 GB", "count": 11},
      {"value": "le32", "label": "17 to 32 GB", "count": 15}
    ],
    "disks": [
      {"value": "1", "label": "1 disk", "count": 9},
      {"value": "2", "label": "2 disks", "count": 9},
      {"value": "3-4", "label": "3 to 4 disks", "count": 18}
    ],
    "virtualization": [
      {"value": "vmware", "label": "vmware", "count": 18},
      {"value": "hyper-v", "label": "hyper-v", "count": 6},
      {"value": "physical", "label": "physical", "count": 5},
      {"value": "unknown", "label": "Unknown", "count": 31}
    ]
  }
}
```

Errors: `401`, `403`.

### 3.7 `GET /api/v1/inventory/changes`

Change events (installs, removals, version changes, OS and firmware changes, hardware swaps) newest first, with statistics over the window and a per-day series. Permission `inventory.view`.

| Parameter | Type · default | Notes |
|---|---|---|
| `category` | `software` \| `os` \| `firmware` \| `hardware` | |
| `action` | `added` \| `removed` \| `changed` | |
| `kind` | `server` \| `device` | |
| `asset` | `'<kind>:<uuid>'` | One asset. For a server this also matches the events of its SNMP twin. A malformed value is `422`. |
| `subject_key` | string | A software product key (`google-chrome`). |
| `q` | string | Contains, over asset name, subject, product key, old and new value. |
| `label` | `upgrade` \| `downgrade` | Software version changes that are upgrades or downgrades by comparator. Implies `category=software&action=changed` rows only. |
| `since` | ISO-8601 text (at most 64 characters) · `generated_at − 7 days` | Inclusive. Parsed once by the server (`inventory_query.parse_instant`, the parser of every Inventory route that takes a date-time): ISO 8601 with or without an offset, a date, or a Unix timestamp. Anything else, a year the calendar cannot build (`0000-01-01`) and a date outside 1970 to 2100 are `422` (a date-picker typo must not become a calendar overflow or a `500`). |
| `until` | ISO-8601 text · open | **Exclusive.** Read like `since`, between 1970 and 2100, and after `since` (`422` otherwise). |
| `tz` | IANA zone name · the appliance time zone | Zone for the daily `series`. A malformed name is `422`. A well-formed name this host cannot open is not the caller's mistake: a known legacy alias is read as the zone it links to (`Asia/Calcutta` as `Asia/Kolkata`), anything else cuts the series in **UTC** and the response says so (`window.tz` is the zone actually used, `window.tz_fallback` is `true`). |
| `page`, `page_size` | as in 3.2 | |

```ts
interface ChangesResponse {
  generated_at: string
  window: { since: string; until: string | null; tz: string; tz_fallback: boolean }   // tz = the zone the series was cut in; tz_fallback: the requested zone could not be opened and UTC was used
  items: ChangeRow[]                                   // newest first (observed_at, then id, descending)
  total: number                                        // rows matching ALL filters
  page: number; page_size: number
  stats: {                                             // over the window, honouring kind/asset/subject_key/q but NOT category/action/label
    total: number
    software: { added: number; removed: number; changed: number; upgraded: number; downgraded: number
                paired: { upgraded: number; downgraded: number } }
    os: number; firmware: number; hardware: number
  }
  series: { day: string; software: number; os: number; firmware: number; hardware: number; total: number }[]   // one point per local day, zero days included
  history_start: string | null                         // when change capture began on this installation (X4); null only if it was never recorded
  retention_days: number                               // events older than this are removed (settings.change_retention_days)
}

interface ChangeRow {
  id: number
  observed_at: string
  asset_key: string; asset_kind: 'server' | 'device'; asset_id: string
  asset_name: string | null                            // the name at the time of the event
  asset_present: boolean                               // false: the asset has been deleted since (history survives deletion)
  server_id: string | null                             // for a device that is a server's SNMP twin: that server (link to it instead)
  category: 'software' | 'os' | 'firmware' | 'hardware'
  action: 'added' | 'removed' | 'changed'
  subject: string                                      // package name, OS name, 'os_version', 'hw_serial', 'hw_model'
  subject_key: string | null                           // software product key
  old_value: string | null; new_value: string | null
  label: 'upgrade' | 'downgrade' | null                // software 'changed' rows whose versions can be ordered; null otherwise (never a guess)
  pair: { event_id: number; label: 'upgrade' | 'downgrade' | null } | null
  marker: boolean                                      // the "(change budget exceeded)" row, not a real change
  snapshot_truncated: boolean                          // recorded from a list that was cut at the agent's cap: show the "list was capped" note
  detail: Record<string, unknown>                      // vendor, architecture, source, os_family, os_release, os_source, …
}
```

Rules:

- **Why `stats` ignores category, action and label:** they are the chips that offer those filters, so choosing one chip must not zero the others. Every stats number equals the `total` of this endpoint with that chip's filters and the same window (section 4).
- **Upgrade and downgrade pairing.** A product whose display name embeds its version (Java, 7-Zip, Visual C++ redistributables) is stored as a new row, so an upgrade appears as one `removed` plus one `added` event written in the same transaction (same asset, same `subject_key`, same `observed_at`). When there is exactly one of each, both rows carry `pair` (`event_id` of the other half, `label` by comparator of the removed version against the added one). `stats.software.paired` counts those pairs and is **informational**: it is not part of `upgraded` / `downgraded`, which count `changed` rows only, so that `upgraded` stays equal to the `total` of `label=upgrade` (the `label` filter selects `changed` rows only). The UI may add a line such as "plus N upgrades recorded as an installation and a removal" and show it as text.
- **Scoped users.** A restricted caller's feed is built **asset-first**: the visible assets are resolved from their tags, then events are fetched per asset through `idx_inv_events_asset` (doc 02 §14). Events of an asset that has been deleted since are not shown to a restricted user (its tags are gone, so its visibility cannot be proven; failing closed). An unrestricted caller sees them with `asset_present: false`. The SNMP twin of a visible server follows the server's tags, not its own.
- The `(change budget exceeded)` marker is a row like any other for counting (it is a `software`/`changed` row) and is flagged `marker: true`; the UI draws it as a distinct row.
- The daily `series` is bounded to 800 days; days are local days in `window.tz`. It never extends past today, even when `until` is later (events are never in the future).
- **`history_start`** is when change capture began on this installation: the time the capture triggers were armed (migration 158 records it under `inventory.capture_started_at` in `system_settings`). It is install-wide and **not** the date of the first event, because a quiet estate records nothing for days; changes before it were never recorded, and events older than `retention_days` are removed by the daily purge. The UI says "history starts X; changes older than N days are removed" (doc 03 §3.7). The flyout's `changes.first_change_at` is the asset's own oldest event (section 3.4).
- **`stats.software.paired`** counts a pair when *either* half matches `q` (the free text is applied to the pair, not to each half).

Example (`changes.json`, 7-day default window, four of the 50 rows chosen to show an upgrade, a firmware change, an installation and the removal half of a paired upgrade; `series` cut to the last two days):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "window": {"since": "2026-10-01T12:00:00Z", "until": null, "tz": "UTC", "tz_fallback": false},
  "items": [
    {
      "id": 58,
      "observed_at": "2026-10-08T09:00:00Z",
      "asset_key": "server:878db435-7146-56e1-948f-976b4d4e287d",
      "asset_kind": "server",
      "asset_id": "878db435-7146-56e1-948f-976b4d4e287d",
      "asset_name": "srv-w22-01",
      "asset_present": true,
      "server_id": null,
      "category": "software",
      "action": "changed",
      "subject": "Google Chrome",
      "subject_key": "google-chrome",
      "old_value": "151.0.7922.110",
      "new_value": "152.0.8001.12",
      "label": "upgrade",
      "pair": null,
      "marker": false,
      "snapshot_truncated": false,
      "detail": {"source": "hklm", "vendor": "Google LLC", "architecture": "x64"}
    },
    {
      "id": 57,
      "observed_at": "2026-10-08T06:27:00Z",
      "asset_key": "device:03020bd0-7299-58c2-ad29-f6cb5974a08c",
      "asset_kind": "device",
      "asset_id": "03020bd0-7299-58c2-ad29-f6cb5974a08c",
      "asset_name": "fg-01",
      "asset_present": true,
      "server_id": null,
      "category": "firmware",
      "action": "changed",
      "subject": "FortiOS",
      "subject_key": null,
      "old_value": "7.2.5",
      "new_value": "7.2.8",
      "label": null,
      "pair": null,
      "marker": false,
      "snapshot_truncated": false,
      "detail": {"os_family": "fortios", "os_source": "template", "os_release": "7.2"}
    },
    {
      "id": 56,
      "observed_at": "2026-10-08T05:15:00Z",
      "asset_key": "server:6ef72426-fb96-52b5-b55d-a3990a823c8f",
      "asset_kind": "server",
      "asset_id": "6ef72426-fb96-52b5-b55d-a3990a823c8f",
      "asset_name": "srv-w19-13",
      "asset_present": true,
      "server_id": null,
      "category": "software",
      "action": "added",
      "subject": "CrowdStrike Falcon Sensor",
      "subject_key": "crowdstrike-falcon-sensor",
      "old_value": null,
      "new_value": "7.15.18513",
      "label": null,
      "pair": null,
      "marker": false,
      "snapshot_truncated": false,
      "detail": {"source": "hklm", "vendor": "CrowdStrike, Inc.", "architecture": "x64"}
    },
    {
      "id": 52,
      "observed_at": "2026-10-08T01:43:00Z",
      "asset_key": "server:e901e1ab-56ce-5921-b706-2cafd0d511dc",
      "asset_kind": "server",
      "asset_id": "e901e1ab-56ce-5921-b706-2cafd0d511dc",
      "asset_name": "srv-w22-17",
      "asset_present": true,
      "server_id": null,
      "category": "software",
      "action": "removed",
      "subject": "Java 8 Update 391",
      "subject_key": "java-se-runtime",
      "old_value": "8.0.3910.11",
      "new_value": null,
      "label": null,
      "pair": {"event_id": 51, "label": "upgrade"},
      "marker": false,
      "snapshot_truncated": false,
      "detail": {"source": "hklm", "vendor": "Oracle Corporation", "architecture": "x64"}
    }
  ],
  "total": 56,
  "page": 1,
  "page_size": 50,
  "stats": {
    "total": 56,
    "software": {"added": 12, "removed": 12, "changed": 15, "upgraded": 12, "downgraded": 1, "paired": {"upgraded": 6, "downgraded": 1}},
    "os": 4,
    "firmware": 11,
    "hardware": 2
  },
  "series": [
    {"day": "2026-10-07", "software": 12, "os": 1, "firmware": 4, "hardware": 1, "total": 18},
    {"day": "2026-10-08", "software": 6, "os": 0, "firmware": 2, "hardware": 0, "total": 8}
  ],
  "history_start": "2026-08-24T12:00:00Z",
  "retention_days": 400
}
```

Errors: `401`, `403`, `422`.

### 3.8 `GET /api/v1/inventory/software`

The software catalog: one row per **product** (the normaliser's `name_key`), with its version distribution. Permission `inventory.view`. The software inventory is read within the caller's tag scope: a scoped caller's `hosts`, `installs`, versions, footers and rows cover the servers they can see, and a product with no visible host is absent.

| Parameter | Type · default | Notes |
|---|---|---|
| `q` | string | Every word must appear (case-insensitive) in the product key, the product name of a catalog-rule product or the publisher: `google chrome`, `visual c++`, `microsoft`. A query with no letters or digits (`%`) is looked for literally, so punctuation never selects everything. |
| `category` | string, **repeatable** | The product's category (`browser`, `security`, `endpoint-protection`, `remote-access`, `database`, `web-server`, `runtime`, `development`, `productivity`, `monitoring`, `backup`, `virtualization`, `networking`, `driver`, `os-component`, `update`, `other`); `422` for any other value. `endpoint-protection` holds only antivirus, endpoint-protection-platform and EDR engines and sensors (X1); `security` is the broad bucket of everything else security-related. A category named here is never hidden by `hidden=exclude`. |
| `publisher` | string | A `value` of `/software/publishers` (the normalised publisher key; `unknown` for products without one), case-insensitive. |
| `hidden` | `exclude` \| `include` · `exclude` | `exclude` leaves out the products whose category is in `hide_categories_by_default` (`update`, `driver`, `os-component` and `runtime` by default). |
| `unmatched` | boolean · `false` | Only products the normaliser could not place (`norm_status: unmatched`): the queue the P2 catalog editor works through. |
| `sort` | `name`, `publisher`, `category`, `hosts`, `version_count`, `latest_version`, `first_seen` · `hosts` | `name` orders by the product key (the slug of the product name: alphabetical except for punctuation), `publisher` by the alias-table label (else the key), `latest_version` by the natural order of the version text. Rows without a value come last in both directions; ties are broken by the product key, ascending, so the order is **total** and paging never repeats or skips a row. **`sort=latest_version` orders at most 40,000 products at a time**: when the other filters select more, the answer is `422` ("sort=latest_version orders at most 40,000 products at a time: narrow the catalog with q, category or publisher first"). `sort=version_count` has no cap. |
| `order` | `asc` \| `desc` · `desc` for `hosts` and `version_count`, `asc` for every other sort | The effective value is echoed in the response. |
| `page`, `page_size` | as in 3.2 | |

```ts
interface CatalogResponse {
  generated_at: string                                // when the table behind the figures was read (up to 60 s ago; up to five minutes for the whole-estate table, see below)
  scope: { restricted: boolean; tags: string[] }
  items: CatalogRow[]; total: number; page: number; page_size: number; sort: string; order: 'asc' | 'desc'
  hidden_products: number                             // footer: see "Footer counts"
  hidden_installs: number
  unmatched_titles: number
  hidden_categories: string[]                         // the categories hidden by default (settings), sorted
  data_status: { normalising: boolean; pending_software_rows: number; pending_identity_rows: number }   // Conventions, X2
}
interface CatalogRow {
  product_key: string                                 // the address of the product page: /inventory/software/<product_key>
  name: string                                        // the normaliser's product name (a catalog rule's, or the cleaned title); the key when no row is left to read
  publisher: string | null                            // the alias table's label (Microsoft), else the raw publisher text found in the data, else the key; null: no publisher
  publisher_key: string | null
  category: string                                    // never null: 'other' when the rows carry none
  norm_status: 'normalized' | 'publisher_only' | 'unmatched'
  hosts: number                                       // distinct servers with at least one row of the product
  installs: number                                    // rows of the product (a server can hold several)
  version_count: number                               // distinct non-blank versions, by equivalence key (7.2 and 7.2.0 are one)
  latest_version: string | null                       // newest by the comparator; null when no version can be ordered
  distribution: VersionBar
  first_seen: string | null                           // the oldest first_seen_at of its rows
  last_change: string | null                          // the newest software change event of the product on a visible server; null: none recorded
}
interface VersionBar {
  top: { version: string | null; hosts: number; scheme: 'semverish' | 'dpkg' | 'rpm'; rank: number | null }[]   // up to 8, newest first
  other: { versions: number; hosts: number }          // the versions not in top: how many, and the sum of their hosts
}
```

Rules:

- **Row figures.** `hosts`, `installs` and `version_count` are exactly the numbers the links open (section 4). `hosts` counts a server once however many rows it holds (the 32 bit and 64 bit builds of a product share a key; so do the Java and 7-Zip titles that embed their version), so `hosts <= installs`.
- **Versions are counted by equivalence key** (trimmed, lower-cased, trailing `.0` parts dropped: `7.2` and `7.2.0`, `V1` and `v1` are one version; section 5), the key the Assets filter `product_version` compares. A version's label is its longest spelling (ties: byte order); its `hosts` counts a server once across the spellings.
- **Version bar.** `top` holds the up to eight versions with the most hosts (between equally busy versions the newer one), ordered **newest first by the comparator** (the same order as the product page, `rank` is the rank there). Their hosts are per version: a server that runs two versions of the product counts in both. `other.hosts` is the sum of the omitted versions' hosts. The bar never holds the rows without a version (they are `unversioned` on the product page).
- **`latest_version`.** The newest version of the product's main scheme group (section 5): the group with more installs, then semverish, dpkg, rpm. `null` when nothing can be ordered (every version failed to parse, or the product has none).
- **`norm_status`.** `normalized`: a catalog rule placed it (or a noise rule: updates, drivers and language packs). `publisher_only`: no product rule, but the publisher is a known alias. `unmatched`: neither (section 5 says how the status is recovered).
- **Footer counts.** Each respects the other active filters, so the number is exactly the size of the list its link opens. `hidden_products` and `hidden_installs`: the products, and their installs, in the hidden categories among the products the other filters select; `total` with `hidden=include` equals `total` with `hidden=exclude` plus `hidden_products` (a category named in `category` is never counted as hidden). `unmatched_titles`: the unmatched products among those shown by the other filters; `total` with `unmatched=true` equals it. The pages call these "37 titles unmatched" and "312 hidden entries".
- **Cost and freshness.** The catalog reads a table of every product's installs, category, publisher and first-seen date, cached **per scope for 60 s** in the API process (built once for concurrent callers). The whole-estate table is served for up to 5 minutes while a fresh one is built in the background, and every API worker also runs a **warmer** that rebuilds it every 40 s while the catalog is in use (and reads the default catalog and publisher views on it before publishing it), so a request does not wait for the scan of every software row (`ZENPLUS_INVENTORY_CATALOG_WARMER=0` in the environment switches the warmer off). A restricted caller's table is never served stale and never warmed, because it holds which servers the caller may see. `hosts` and the version distribution are exact figures read for the products on the page (and, for `sort=hosts`, for the few more products that provably contain the page: `hosts` can never exceed `installs` or the number of visible servers). Only `sort=hosts&order=asc` reads every product, and `sort=version_count` / `sort=latest_version` read every product's (product, version) pairs once. Measured times are in section 6.5.
- Servers that have not delivered a software inventory contribute no rows. Rows with no `name_key` yet (not normalised) are not counted anywhere until the normaliser keys them. In steady state the agent ingest keys its rows inline; after an upgrade the existing rows are keyed by the background loop at 100,000 rows per 120 s tick, which is about 40 minutes for the 2 million rows of the sizing target. `data_status.pending_software_rows` counts them and the pages show the "normalising" banner meanwhile; an empty catalog with pending rows is not an empty estate.

Example (`software.json`, cut to three of the 24 rows: the CrowdStrike Falcon sensor (category `endpoint-protection`), Chrome with a recorded change, and Git, whose versions come in two schemes; the fixture's `hidden_products` are the products of the hidden categories):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "scope": {"restricted": false, "tags": []},
  "items": [
    {
      "product_key": "crowdstrike-falcon-sensor",
      "name": "CrowdStrike Falcon Sensor",
      "publisher": "CrowdStrike",
      "publisher_key": "crowdstrike",
      "category": "endpoint-protection",
      "norm_status": "normalized",
      "hosts": 28,
      "installs": 28,
      "version_count": 3,
      "latest_version": "7.18.19103",
      "distribution": {
        "top": [
          {"version": "7.18.19103", "hosts": 7, "scheme": "semverish", "rank": 1},
          {"version": "7.15.18513", "hosts": 13, "scheme": "semverish", "rank": 2},
          {"version": "7.04.17605", "hosts": 8, "scheme": "semverish", "rank": 3}
        ],
        "other": {"versions": 0, "hosts": 0}
      },
      "first_seen": "2026-04-17T12:00:00Z",
      "last_change": "2026-10-08T05:15:00Z"
    },
    {
      "product_key": "google-chrome",
      "name": "Google Chrome",
      "publisher": "Google",
      "publisher_key": "google",
      "category": "browser",
      "norm_status": "normalized",
      "hosts": 41,
      "installs": 41,
      "version_count": 3,
      "latest_version": "152.0.8001.12",
      "distribution": {
        "top": [
          {"version": "152.0.8001.12", "hosts": 20, "scheme": "semverish", "rank": 1},
          {"version": "151.0.7922.110", "hosts": 14, "scheme": "semverish", "rank": 2},
          {"version": "150.0.7871.50", "hosts": 7, "scheme": "semverish", "rank": 3}
        ],
        "other": {"versions": 0, "hosts": 0}
      },
      "first_seen": "2026-07-25T12:00:00Z",
      "last_change": "2026-10-08T09:00:00Z"
    },
    {
      "product_key": "git",
      "name": "Git",
      "publisher": "Git",
      "publisher_key": "git",
      "category": "development",
      "norm_status": "normalized",
      "hosts": 19,
      "installs": 19,
      "version_count": 5,
      "latest_version": "2.47.0",
      "distribution": {
        "top": [
          {"version": "2.47.0", "hosts": 5, "scheme": "semverish", "rank": 1},
          {"version": "2.46.2", "hosts": 6, "scheme": "semverish", "rank": 2},
          {"version": "2.43.0", "hosts": 2, "scheme": "semverish", "rank": 3},
          {"version": "1:2.34.1-1ubuntu1.11", "hosts": 3, "scheme": "dpkg", "rank": 1},
          {"version": "1:2.34.1-1ubuntu1.10", "hosts": 3, "scheme": "dpkg", "rank": 2}
        ],
        "other": {"versions": 0, "hosts": 0}
      },
      "first_seen": "2026-07-19T12:00:00Z",
      "last_change": "2026-10-05T07:00:00Z"
    }
  ],
  "total": 24,
  "page": 1,
  "page_size": 50,
  "sort": "hosts",
  "order": "desc",
  "hidden_products": 9,
  "hidden_installs": 274,
  "unmatched_titles": 0,
  "hidden_categories": ["driver", "os-component", "runtime", "update"],
  "data_status": {"normalising": false, "pending_software_rows": 0, "pending_identity_rows": 0}
}
```

Errors: `401`, `403`, `422` (`category`, `hidden`, `sort`, `order`, `page`, `page_size`, a `q` longer than 200 characters, `sort=latest_version` over a catalog of more than 40,000 products).

### 3.9 `GET /api/v1/inventory/software/publishers`

The publisher facet of the catalog. Permission `inventory.view`. Parameters: `q`, `category` (repeatable), `hidden`, `unmatched` as in 3.8 (every catalog filter **except `publisher`**, so a count is what choosing the value would give), and `limit` (1 to 200, default 50).

```ts
interface PublishersResponse {
  generated_at: string; scope: { restricted: boolean; tags: string[] }
  total: number                                       // products in this view = the catalog total for the same filters
  distinct: number                                    // publishers, uncapped
  items: { value: string; label: string; products: number; installs: number; hosts: number }[]   // up to `limit`
  data_status: { normalising: boolean; pending_software_rows: number; pending_identity_rows: number }   // Conventions, X2
}
```

`value` goes into the catalog's `publisher` filter (`unknown` for products without a publisher; its `label` is `Unknown`). `products` is the catalog `total` for `publisher=<value>`; `installs` is the sum of those products' installs; `hosts` is the number of servers with at least one row of those products. Ordered by products, then installs, then label. A publisher the alias table does not know is labelled by its raw publisher text (the smallest `vendor` string found among its rows, so it is the same every time).

Example (`software-publishers.json`, three of the 21 publishers):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "scope": {"restricted": false, "tags": []},
  "total": 24,
  "distinct": 21,
  "items": [
    {"value": "microsoft", "label": "Microsoft", "products": 4, "installs": 62, "hosts": 41},
    {"value": "google", "label": "Google", "products": 1, "installs": 41, "hosts": 41},
    {"value": "igor-pavlov", "label": "Igor Pavlov", "products": 1, "installs": 34, "hosts": 34}
  ],
  "data_status": {"normalising": false, "pending_software_rows": 0, "pending_identity_rows": 0}
}
```

Errors: `401`, `403`, `422`.

### 3.10 `GET /api/v1/inventory/software/{product_key}`

The product page: header, every version ranked, and the raw names behind the product. Permission `inventory.view`. **`404 {"detail":"Product not found"}` for an unknown product and for a product with no host in the caller's scope**, one body for both.

```ts
interface ProductResponse {
  generated_at: string; scope: { restricted: boolean; tags: string[] }
  header: {
    product_key: string; name: string; publisher: string | null; publisher_key: string | null; category: string
    norm_status: 'normalized' | 'publisher_only' | 'unmatched'
    rule_id: string | null                            // the pack rule that placed the product's sample title; null when none did
    hosts: number; installs: number; version_count: number; latest_version: string | null
    first_seen: string | null; last_change: string | null                  // as in the catalog row
    mixed_schemes: boolean                            // the versions belong to more than one scheme group (Windows and Linux packages)
  }
  versions: {                                         // ALL distinct non-blank versions, in display order
    version: string; hosts: number; installs: number
    scheme: 'semverish' | 'dpkg' | 'rpm'
    rank: number | null                               // 1 = newest of its scheme group; equal spellings (1.0, 1.0.0) share a rank; null: the scheme cannot parse it
    generation: 'N' | 'N-1' | 'older' | null          // rank 1, rank 2, rank 3 and up (older = two or more versions behind the newest of its group)
  }[]
  unversioned: { hosts: number; installs: number; address: string }    // rows with no or a blank version (zero when none); address is always '__none__'
  evidence: { items: EvidenceItem[]; total: number; truncated: boolean }
  data_status: { normalising: boolean; pending_software_rows: number; pending_identity_rows: number }   // Conventions, X2
}
interface EvidenceItem {                              // a distinct raw (package name, publisher) pair that maps to this product
  package_name: string; vendor: string | null; source: string | null    // source: hklm, dpkg, rpm, ...
  hosts: number; installs: number
  status: string | null; rule_id: string | null       // what the normaliser makes of this raw title today
}
```

Rules:

- **Version order.** Within a scheme group the comparable versions are ordered newest first and ranked; the group with more installs comes first (a product installed on Windows and on Ubuntu has a `semverish` group and a `dpkg` group, each with its own N); versions the scheme cannot parse follow their group in natural order, newest first, with `rank: null`. Versions are listed by equivalence key (`7.2` and `7.2.0` are one entry, labelled with the longest spelling; its `hosts` counts a server once across the spellings), and a version spelled `unknown` is an ordinary version. A version row's `hosts` equals the `total` of `/assets?product=<key>&product_version=<version>` and its `installs` equals the `total` of the installation list filtered by that version. `header.version_count` equals `versions.length` (the number the Overview's "versions" figure links to).
- **N, N-1, older** are observed generations, not vendor releases: rank 1 (`N`) is the newest version on the visible servers, rank 2 (`N-1`) the one before it, and `older` every earlier one, *two or more versions behind the newest* (lifecycle-aware currency arrives with P4).
- **Unversioned rows** are reported apart: they are not a version. `unversioned.address` (always `__none__`) is the value of the `version` parameter that lists them in the installation list, so `unversioned.installs` links to `…/installations?version=__none__`; the assets vocabulary has no `product_version` sentinel for "no version", so `unversioned.hosts` is text. (`version=unknown` is no longer an alias: it selects a version spelled "unknown".)
- **Evidence** lists at most 200 pairs, the most widely installed first; `total` is the number of distinct pairs and `truncated` says more exist. `status` and `rule_id` are re-derived from the raw title (the Evidence tab shows which rule matched).

Example (`software-product.json`: Git has three Windows versions and two Ubuntu package versions):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "scope": {"restricted": false, "tags": []},
  "header": {
    "product_key": "git",
    "name": "Git",
    "publisher": "Git",
    "publisher_key": "git",
    "category": "development",
    "norm_status": "normalized",
    "rule_id": "git",
    "hosts": 19,
    "installs": 19,
    "version_count": 5,
    "latest_version": "2.47.0",
    "first_seen": "2026-07-19T12:00:00Z",
    "last_change": "2026-10-05T07:00:00Z",
    "mixed_schemes": true
  },
  "versions": [
    {"version": "2.47.0", "hosts": 5, "installs": 5, "scheme": "semverish", "rank": 1, "generation": "N"},
    {"version": "2.46.2", "hosts": 6, "installs": 6, "scheme": "semverish", "rank": 2, "generation": "N-1"},
    {"version": "2.43.0", "hosts": 2, "installs": 2, "scheme": "semverish", "rank": 3, "generation": "older"},
    {"version": "1:2.34.1-1ubuntu1.11", "hosts": 3, "installs": 3, "scheme": "dpkg", "rank": 1, "generation": "N"},
    {"version": "1:2.34.1-1ubuntu1.10", "hosts": 3, "installs": 3, "scheme": "dpkg", "rank": 2, "generation": "N-1"}
  ],
  "unversioned": {"hosts": 0, "installs": 0, "address": "__none__"},
  "evidence": {
    "items": [
      {
        "package_name": "Git",
        "vendor": "The Git Development Community",
        "source": "hklm",
        "hosts": 13,
        "installs": 13,
        "status": "normalized",
        "rule_id": "git"
      },
      {"package_name": "git", "vendor": "Ubuntu Developers", "source": "dpkg", "hosts": 6, "installs": 6, "status": "normalized", "rule_id": "git"}
    ],
    "total": 2,
    "truncated": false
  },
  "data_status": {"normalising": false, "pending_software_rows": 0, "pending_identity_rows": 0}
}
```

Errors: `401`, `403`, `404`.

### 3.11 `GET /api/v1/inventory/software/{product_key}/installations`

Where the product is installed: one row per software **row** (a server with two titles of the product appears twice). Permission `inventory.view`; the same `404` as 3.10 for an unknown or out-of-scope product.

| Parameter | Type · default | Notes |
|---|---|---|
| `version` | string | A version, matched by equivalence key like `product_version` (`7.2` lists the `7.2.0` rows too; a version spelled `unknown` is an ordinary version). `__none__` selects the rows that report **no** version (null or blank): it is the `unversioned.address` of the product page. The echoed `version` is the value requested. |
| `q` | string | Contains, over the server name, the package title and the version. |
| `sort` | `asset_name`, `package_name`, `version`, `architecture`, `source`, `install_date`, `first_seen_at`, `updated_at` · `asset_name` | `version` orders by the comparator (the product page's order: ascending is oldest first within a scheme group; rows with no version last in both directions) and is limited to 50,000 rows per product (`422` past it: filter first). Names sort case-insensitively. Ties are broken by server name, server id and package title: a total order. |
| `order` | `asc` \| `desc` · `asc` | |
| `page`, `page_size` | as in 3.2 | |

```ts
interface InstallationsResponse {
  generated_at: string; product_key: string; version: string | null          // the filter, echoed
  items: {
    asset_key: string; asset_id: string; asset_name: string       // 'server:<uuid>' = the Assets address
    package_name: string; version: string | null; architecture: string | null; source: string | null
    install_date: string | null; first_seen_at: string | null; updated_at: string | null   // updated_at: the last report that listed it
  }[]
  total: number; page: number; page_size: number; sort: string; order: 'asc' | 'desc'
}
```

`total` for no filter equals `header.installs`; with `version=<v>` it equals that version row's `installs`; with `version=__none__` it equals `unversioned.installs`.

Example (`software-installations.json`, two of the 19 rows):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "product_key": "git",
  "version": null,
  "items": [
    {
      "asset_key": "server:2a4470ae-aa12-5b1c-a194-e24bfe704454",
      "asset_id": "2a4470ae-aa12-5b1c-a194-e24bfe704454",
      "asset_name": "srv-rh-01",
      "package_name": "git",
      "version": "1:2.34.1-1ubuntu1.10",
      "architecture": null,
      "source": "dpkg",
      "install_date": "2025-03-17T12:00:00Z",
      "first_seen_at": "2026-07-20T12:00:00Z",
      "updated_at": "2026-10-08T00:10:00Z"
    },
    {
      "asset_key": "server:95e1703c-c864-5833-91b8-9926c861b107",
      "asset_id": "95e1703c-c864-5833-91b8-9926c861b107",
      "asset_name": "srv-rh-02",
      "package_name": "git",
      "version": "1:2.34.1-1ubuntu1.11",
      "architecture": null,
      "source": "dpkg",
      "install_date": "2025-03-06T12:00:00Z",
      "first_seen_at": "2026-07-19T12:00:00Z",
      "updated_at": "2026-10-03T15:00:00Z"
    }
  ],
  "total": 19,
  "page": 1,
  "page_size": 50,
  "sort": "asset_name",
  "order": "asc"
}
```

Errors: `401`, `403`, `404`, `422` (a bad `sort` or `order`, `page`/`page_size`, a `version` or `q` longer than 200 characters, `sort=version` over more than 50,000 rows).

### 3.12 `GET /api/v1/inventory/trends`

A daily series from `inventory_daily_snapshots`, which the snapshot loop writes once a day (doc 02 section 6) at `snapshot_hour`:15 appliance time. Permission `inventory.view`. Snapshots are kept 3 years.

| Parameter | Type · default | Notes |
|---|---|---|
| `metric` | **required**: `assets`, `os_release`, `software_products`, `coverage`, `health`, `changes` | The families the loop writes. Dimensions: `assets` = `server`, `device`; `coverage` = `fresh`, `aging`, `stale`, `never`, `not_applicable` (`not_applicable` counts the assets that cannot report by design and is in none of the other four, so `fresh / (fresh + aging + stale + never)` is the share of the assets that could report); `health` = `missing_os_version`, `missing_model`, `missing_serial`, `unknown_vendor`, `stale`, `never`, `software_truncated`, `missing_security_agent`, **by the same definitions as the live Overview** (model, serial and vendor gaps of network devices only; the identity predicates skip the assets that cannot report by design; `missing_security_agent` is the `missing` state of servers, section 5); `software_products` = `products`, `installs` (keyed rows, hidden categories excluded); `os_release` = `family/product/release` (`unknown//` for assets nothing is known about); `changes` = `category/action` (`software/added` ... `hardware/changed`). |
| `dimension` | string | Exactly one dimension; without it every dimension of the metric (at most 60 series, the highest latest value first; `truncated` says more exist). |
| `days` | 1 to 400 · 30 | The window is the last `days` **appliance-local days**, today included (the `company` time zone, the day the snapshot loop uses). |

```ts
interface TrendsResponse {
  generated_at: string
  restricted: boolean          // true: the caller has a tag scope. The snapshots are fleet-wide and carry no tags, so there is NO series (below)
  enabled: boolean             // settings.trends_enabled; false: no series (recording continues, only the display is off)
  metric: string; dimension: string | null; days: number
  window: { since: string; until: string }       // local dates, both included
  first_day: string | null     // the oldest snapshot day of the metric, even outside the window: "history starts ..."
  days_with_data: number       // distinct days that have a point in `series`; draw a sparkline from 3
  dimensions_total: number     // dimensions found in the window (before the 60-series cap)
  truncated: boolean
  series: { dimension: string; points: { day: string; value: number }[] }[]      // points in day order
}
```

- **A day without a snapshot is a gap, never a zero**: the points hold only the days that exist (the loop never backfills a missed day). Draw the gap.
- **The first points are not an upgrade artefact.** The loop holds a day's snapshot until the process has been up for 30 minutes and the normalise backlog is empty (never longer than 12 hours past the due time), and during the first 24 hours after the capture start (`inventory.capture_started_at`) it refreshes today's row, at most hourly, while the estate settles. A closed day is never rewritten.
- **A restricted caller** (own `scope_tags` or role `scope_tags`) gets `200` with `restricted: true`, `series: []`, `days_with_data: 0`, `first_day: null`; the database is not read. A fleet-wide series would leak the size of assets the caller may not see (principle 3, T12).
- An unknown `dimension` is `200` with one series with no points.

Example (`trends.json`, each series cut to its last three points; the fixture has 13 days with data):

```json
{
  "generated_at": "2026-10-08T12:00:00Z",
  "restricted": false,
  "enabled": true,
  "metric": "assets",
  "dimension": null,
  "days": 30,
  "window": {"since": "2026-09-09", "until": "2026-10-08"},
  "first_day": "2026-09-25",
  "days_with_data": 13,
  "dimensions_total": 2,
  "truncated": false,
  "series": [
    {"dimension": "device", "points": [{"day": "2026-10-06", "value": 81}, {"day": "2026-10-07", "value": 83}, {"day": "2026-10-08", "value": 85}]},
    {"dimension": "server", "points": [{"day": "2026-10-06", "value": 58}, {"day": "2026-10-07", "value": 59}, {"day": "2026-10-08", "value": 60}]}
  ]
}
```

Errors: `401`, `403`, `422` (`metric` missing or unknown, `days` outside 1 to 400).

### 3.13 `GET /api/v1/inventory/settings` and `PUT /api/v1/inventory/settings`

The module settings (`system_settings['inventory']`). `GET` needs `inventory.view`, `PUT` needs **`inventory.manage`** (declared on the route). Settings are module configuration, not tagged data: a scoped caller reads and, with the permission, changes the same values.

```ts
interface SettingsResponse {
  settings: InventorySettings                         // what is in force now
  defaults: InventorySettings
  limits: Record<'fresh_hours' | 'stale_days' | 'change_retention_days' | 'snapshot_hour', { min: number; max: number }>
  categories: string[]                                // the vocabulary of hide_categories_by_default
  updated_at: string | null                           // null: never saved, the defaults apply
  changed: string[]                                   // PUT: the top-level keys whose value changed; GET: []
  adjusted: string[]                                  // PUT: numbers the validator changed (clamped, rounded or capped), e.g. "stale_days", "fresh_hours.server" -- including numbers nobody asked to change that a lowered stale_days capped; GET: []
}
interface InventorySettings {
  fresh_hours: { server: number; device: number }    // 1 to 720; never above stale_days x 24
  stale_days: number                                  // 1 to 365
  change_retention_days: number                       // 30 to 3650
  snapshot_hour: number                               // 0 to 23 (appliance time; the loop runs at hh:15)
  hide_categories_by_default: string[]                // category names from `categories` (default `update`, `driver`, `os-component` and `runtime`); [] hides nothing
  trends_enabled: boolean
}
```

`PUT` takes a JSON object with any subset of the settings; **keys left out keep their current value** (and `fresh_hours` may name one kind only):

```http
PUT /api/v1/inventory/settings
{"stale_days": 14, "fresh_hours": {"device": 4}, "hide_categories_by_default": ["update", "driver"]}
```

- **`422` with a string `detail` naming every problem**: unknown keys (`"unknown settings: apple, zebra"`), a value of the wrong type (`"stale_days must be a number"`, `"fresh_hours must be an object with server and device"`, `"unknown categories: boots, shoes"`, `"trends_enabled must be true or false"`). A body that is not a JSON object (an array, a string, a number, `null`) is rejected by the framework before the route runs: `422` with the standard array-shaped `detail` (`type` `dict_type`, or `missing` for `null`). Nothing is stored in either case.
- **Out-of-range numbers are not errors**: they are clamped by the validator the loops use (`merge_inventory_settings`) and the response shows what was stored (`stale_days: 9999` is stored as `365` and listed in `adjusted`). Fractions are rounded.
- **Stored** as the whole merged blob. When the merged result equals the current settings nothing is written and nothing is audited (`changed: []`). Writers are serialised by an advisory lock taken before the current settings are read, so two administrators saving *different* keys at the same moment both keep their change.
- **Audit**: one entry per change, `action: "inventory.settings.update"`, `resource_type: "inventory_settings"`, `resource_id: "inventory"`, `metadata: {"changed": {"<key>": {"from": ..., "to": ...}}}` with the changed keys only. **The audit entry is part of the write**: when it cannot be stored nothing is saved and the answer is `503 {"detail":"The audit log is unavailable, so nothing was done. Try again; if it persists, contact an administrator."}`. The summary and catalog caches of the serving process are dropped at once; other API workers keep theirs for up to 60 s.

Example (`settings.json`, the defaults):

```json
{
  "settings": {
    "fresh_hours": {"server": 24, "device": 2},
    "stale_days": 7,
    "change_retention_days": 400,
    "snapshot_hour": 0,
    "hide_categories_by_default": ["update", "driver", "os-component", "runtime"],
    "trends_enabled": true
  },
  "defaults": {
    "fresh_hours": {"server": 24, "device": 2},
    "stale_days": 7,
    "change_retention_days": 400,
    "snapshot_hour": 0,
    "hide_categories_by_default": ["update", "driver", "os-component", "runtime"],
    "trends_enabled": true
  },
  "limits": {
    "fresh_hours": {"min": 1, "max": 720},
    "stale_days": {"min": 1, "max": 365},
    "change_retention_days": {"min": 30, "max": 3650},
    "snapshot_hour": {"min": 0, "max": 23}
  },
  "categories": [
    "browser",
    "security",
    "endpoint-protection",
    "remote-access",
    "database",
    "web-server",
    "runtime",
    "development",
    "productivity",
    "monitoring",
    "backup",
    "virtualization",
    "networking",
    "driver",
    "os-component",
    "update",
    "other"
  ],
  "updated_at": null,
  "changed": [],
  "adjusted": []
}
```

Errors: `401`, `403`, `422` (`PUT`), `503` (`PUT`, audit entry not stored).

### 3.14 `GET /api/v1/inventory/export`

A dataset as CSV. Permission **`inventory.export`**, declared on the route (not the module default): serials and software lists are reconnaissance data, so a viewer lists everything and downloads nothing. The response streams.

| Parameter | Dataset | Notes |
|---|---|---|
| `dataset` | all, **required**: `assets`, `software`, `installations`, `os`, `hardware`, `changes` | `422` otherwise. |
| the asset filters of section 2 | `assets` | Including `sort` (the list's `sort` keys, default `name`) and `order`. |
| `q`, `category` (repeatable), `publisher`, `hidden`, `unmatched` | `software` | As 3.8; `sort` one of the catalog sorts, default `name` ascending (not the list's `hosts`: a file is easier to read A to Z). |
| `product` (**required**), `version`, `q` | `installations` | As 3.11 (`version` also takes `__none__` for the rows with no version); `sort` as 3.11. An unknown or out-of-scope product is the same `404`. |
| `kind` | `os` | As 3.5. |
| `vendor` | `hardware` | As 3.6. |
| `category` (**one** value, else `422`), `action`, `kind`, `asset`, `subject_key`, `q`, `label`, `since`, `until` | `changes` | As 3.7, same defaults (the last seven days) and the same range check on `since` and `until` (1970 to 2100, else `422`). No `tz`: the file has no daily series. |

- **The file is what the list returns to the same user.** Every dataset is produced by the matching list endpoint's own service function with the caller's tag scope, page after page (500 rows a page); the export adds no query of its own except the change feed's keyset paging (same conditions, no statistics). A scoped caller's file holds the visible assets only. The tests compare the file with the list row by row for every dataset, scoped and not.
- **Format**: UTF-8 **with a byte-order mark**, **CRLF** line ends, one header row. Lists are written as `a;b;c` and booleans as `true` / `false`. **Formula injection**: a text cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe (the rule of `dashboard/src/components/availability/exportCsv.ts` `esc()`, plus the carriage return); a cell containing a quote, comma, line break or apostrophe is quoted. Numbers are written as numbers. The JSON endpoints keep the raw text.
- **Headers**: `Content-Type: text/csv; charset=utf-8`, `Content-Disposition: attachment; filename="zenplus-inventory-<dataset>-<YYYYMMDD>.csv"` (the UTC date), `Cache-Control: no-store`, `X-Export-Rows: <rows in the file>`.
- **Row cap**: at most **200,000** rows. When the filters select more the file is cut at the cap and the response carries **`X-Export-Truncated: true`** (absent otherwise); the audit entry says so too. The file holds the first 200,000 rows in the list's order (the newest events first, for `changes`).
- **Consistency**: the pages of one file are read in one `REPEATABLE READ` read-only transaction on a session of the export's own (pinned: nothing the dataset reads through opens another connection for it or closes it), and the stream does not depend on the request's session. For `assets`, `installations`, `os`, `hardware` and `changes` every figure comes from that one snapshot. `software` is the catalog, which is built from an in-process read model: the product list, its installs and so its order and total are fixed once when the file starts (that table is cached up to 60 s and shared with other requests of the same scope, so it can be that much older than the file), and per-product figures the catalog has already cached on it are reused; whatever is not cached yet is read inside the transaction. A software file is therefore consistent in its rows and order, but each figure is a value the catalog itself showed within the minutes around the file, not one instant.
- **Audit**: written **before the first byte is sent** (the grant of the data is the event, whether or not the client reads on): `action: "inventory.export"`, `resource_type: "inventory_export"`, `resource_id: <dataset>`, `metadata: {"dataset", "filters", "rows", "truncated"}` where `filters` holds the non-empty filters actually applied (with the effective `sort` and `order`). A refused or invalid request writes nothing. **Fail closed**: when the audit entry cannot be stored the answer is `503 {"detail":"The audit log is unavailable, so nothing was done. Try again; if it persists, contact an administrator."}` and no byte of the file is sent.
- **Admission (`429`)**: an export holds one pooled connection and a snapshot for as long as it runs, so at most **2 exports run at once per API worker process and 1 per user**. Any other request is answered `429 {"detail": ...}` with **`Retry-After: 10`** before anything is planned or audited: "you already have an export running; wait for it to finish" when the caller's own export is running, "too many exports are running; try again shortly" otherwise. A client should retry after the header's seconds.
- **A client that stops reading** cannot hold the connection and the snapshot for as long as it sleeps: the producer hands the file to the response through a small bounded queue, and when the client has not read for **120 s** the stream ends with an error (the connection is closed, the database work is rolled back and the slot is given back). A client that goes away costs nothing but the rest of the file; the audit entry already says what was granted.

Columns (the header row, in order):

| Dataset | Columns |
|---|---|
| `assets` | `key, kind, name, ip, asset_type, status, vendor, model, serial, os_family, os_product, os_release, os_version, os_build, os_edition, os_source, hw_source, site, tags, sources, last_seen, inventory_at, inventory_age_s, freshness, software_truncated` |
| `software` | `product_key, name, publisher, category, norm_status, hosts, installs, version_count, latest_version, top_versions, first_seen, last_change` (`top_versions`: `version (hosts);...` as in the version bar, then `other N versions (hosts)`) |
| `installations` | `product_key, asset_key, asset_name, package_name, version, architecture, source, install_date, first_seen_at, updated_at` |
| `os` | `family, family_label, product, release, version, assets, release_assets, release_latest_version, build_currency, raw_example` (one row per version; `build_currency`: `latest`, `previous`, `older` or `unknown` (a missing version, or one the scheme cannot parse, as on the OS page), empty when the release's versions cannot all be ordered) |
| `hardware` | `section, vendor, model, class, value, label, count, devices, firmware_versions, serial_missing, serial_coverage_pct, modules` (`section`: `network_vendor`, `network_model`, `module`, `server_cpu_models`, `server_memory`, `server_disks`, `server_virtualization`) |
| `changes` | `id, observed_at, asset_key, asset_name, asset_present, category, action, subject, subject_key, old_value, new_value, label, paired_event_id, paired_label, marker, snapshot_truncated` |

A sample is `export-assets.csv` (the 15 assets of the scoped tag).

Errors: `401`, `403` (no `inventory.export`), `404` (installations of an unknown or out-of-scope product), `422` (unknown dataset, `product` missing for `installations`, more than one `category` for `changes`, any invalid filter of the matching list), `429` (too many exports, above), `503` (audit entry not stored, above).

### 3.15 Touchpoints outside `/inventory`

Phase 1 adds fields and filters to two existing endpoints. They are part of this contract because the Inventory pages and the links from the Devices and Servers pages rely on them; their permission, tag scope and every other behaviour are unchanged. They are pinned by `server/tests/test_inventory_touchpoints.py`.

**`GET /api/v1/devices`** (the Devices list):

| Addition | Rule |
|---|---|
| `vendor`, `model`, `os_version` (query, at most 200 characters) | Exact, case-insensitive, trimmed (`%` and `_` are literal); they narrow what the tag scope already allows. A blank value filters nothing. `unknown` (any case) selects the devices with **no** value (null, blank or the text "unknown"), the meaning `vendor=unknown` and `model=unknown` have in section 2. `vendor` and `model` match the device's own columns (`vendor` and `model` of each item); `os_version` matches each item's `os_version_display`. |
| `facets=true` (query, default `false`) | The response gains `facets: {vendor, model, os_version}`, each `{items: [{value: string, label: string, count: number}], distinct: number}`, computed over **all** the devices the caller may see, not the page. Each facet counts over every active filter except its own, so a `count` equals the `meta.total` this list gives when its `value` is passed as that filter. Items are sorted by count descending, at most 200 per facet (`distinct` is uncapped); the devices with no value form the item `value: "unknown"`, `label: "Unknown"`. Without the flag nothing is computed and there is no `facets` key. |
| `os_version_display` (every item of `data`) | `string \| null`: the normalised version of the Inventory identity where it has one (for example `17.9.4a`), else the device's own `os_version`, trimmed, else `null`. The legacy `os_version` field is unchanged. |

**`GET /api/v1/servers/{id}/software`** (the Software tab of a server):

| Addition | Rule |
|---|---|
| `product_key`, `category` (every item) | `product_key` is the stored product key, the address of `/inventory/software/<product_key>` (`null` while the row has not been normalised); `category` is the normaliser's category. |
| `agent_truncated` (response) | `true` when the agent itself stopped listing at its cap (`servers.software_truncated`): the stored list is incomplete however large `limit` is. `truncated` keeps its meaning: this response was cut at `limit`. |
| `404 {"detail":"Server not found"}` | The server does not exist or is outside the caller's tag scope, with the same body as `GET /servers/{id}` (an unknown id used to answer `200` with an empty list, and a scoped user could read the package list of any server whose id they knew). |

---

## 4. The drill-down map

Principle 1: every number is a link to the list behind it, and the drilled list's `total` equals the number clicked. The server half of the test is `server/tests/test_inventory_api.py` (T-UI2); it implements exactly this table and runs it against the seeded fleet, for every number of `/summary`, `/os` (all three `kind` values), `/hardware` (several vendors) and `/changes`. The software rows are executed by `server/tests/test_inventory_software.py` (every product's hosts, every version's hosts and installs, every category, the footers).

An **assets drill** opens `/inventory/assets?<params>`; a **changes drill** opens `/inventory/changes?<params>` (with `since` = the page's window).

| Number | Drill | Parameters |
|---|---|---|
| `summary.assets.total` | assets | none |
| `summary.assets.server` / `.device` | assets | `kind=server` / `kind=device` |
| `summary.coverage.fresh` / `aging` / `stale` / `never` / `not_applicable` | assets | `freshness=<bucket>` |
| `summary.coverage.fresh_pct` | assets | `freshness=fresh` (derived figure: the numerator's list) |
| `summary.os.top[i].count` | assets | `os_family=<family>&os_product=<product or unknown>&os_release=<release or unknown>` |
| `summary.os.unknown` | assets | `os_family=unknown` |
| `summary.os.families` / `.releases` | page | `/inventory/os` (the number of family / release nodes of its tree) |
| `summary.software.top[i].hosts`, `sprawl[i].hosts` | assets | `product=<product_key>` |
| `summary.software.top[i].versions`, `sprawl[i].versions` | page | `/inventory/software/<product_key>` (its version distribution has that many rows) |
| `summary.software.security_agent_coverage.covered` | assets | `security_agent=present` |
| `summary.software.security_agent_coverage.missing` | assets | `security_agent=missing` (= `health.missing_security_agent`) |
| `summary.software.security_agent_coverage.unknown` | assets | `security_agent=unknown` |
| `summary.software.security_agent_coverage.total` ("of N") | assets | `kind=server` (the three states above partition it) |
| `summary.software.products` / `installs` / `publishers` | page | `/inventory/software` (at the default settings its `total` is `products`, the sum of its rows' `installs` is `installs`, and `/software/publishers` has `publishers` entries in `distinct`) |
| `summary.hardware.devices` | assets | `kind=device` |
| `summary.hardware.top_models[i].count` | assets | `kind=device&vendor=<vendor>&model=<model>` |
| `summary.hardware.vendors` / `.models` | page | `/inventory/hardware` |
| `summary.health.<key>` (all eight) | assets | `health=<key>` |
| `summary.health.not_collected_by_design.count` | assets | `freshness=not_applicable` |
| `summary.changes_7d.total` | changes | `since=<changes_7d.since>` |
| `summary.changes_7d.software.added` / `removed` / `changed` | changes | `category=software&action=added` / `removed` / `changed` |
| `summary.changes_7d.software.upgraded` / `downgraded` | changes | `category=software&action=changed&label=upgrade` / `downgrade` |
| `summary.changes_7d.os` / `firmware` / `hardware` | changes | `category=os` / `firmware` / `hardware` |
| `os.families[i].count` | assets | `os_family=<family>[&kind=<kind>]` |
| `…products[j].count` | assets | the above plus `os_product=<product or unknown>` |
| `…releases[k].count` | assets | the above plus `os_release=<release or unknown>` |
| `…versions[m].count` | assets | the above plus `os_version=<version or unknown>` |
| `os…currency.latest` / `previous` | assets | the release's filters plus `os_version=<latest_version>` / `<previous_version>` |
| `os…currency.older` / `unknown` | none | the rest of the release; render as text beside the version rows, each of which links (the four buckets add up to the release count) |
| `hardware.network.devices` | assets | `kind=device` |
| `hardware.network.unknown_model_devices` | assets | `kind=device&model=unknown` |
| `hardware.network.vendors[i].count` | assets | `kind=device&vendor=<vendor>`; for `vendor: null` use `vendor=unknown` |
| `…vendors[i].unknown_model_devices` | assets | `kind=device&vendor=<vendor>&model=unknown` |
| `…vendors[i].serial_missing`, `models[j].serial_missing` | assets | the row's filters (`model=unknown` for a row with `model: null`) plus `health=missing_serial` |
| `…vendors[i].not_collected`, `models[j].not_collected` | assets | the row's filters plus `freshness=not_applicable` |
| `hardware.network.models[j].count` | assets | `kind=device&vendor=<vendor>&model=<model>`; for `model: null` use `model=unknown` |
| `hardware.matrix.models[j].cells[k].count` | assets | `kind=device&vendor=<vendor>&model=<model or unknown>&os_release=<release>` (`release: "unknown"` is the literal `unknown`) |
| `hardware.modules[g].devices` | assets | `module_class=<class>&module_model=<model or unknown>` |
| `hardware.modules[g].count`, `with_serial` | none | component counts; the device list is the `devices` link, the serials are in each device's flyout |
| `hardware.servers.total` | assets | `kind=server` |
| `hardware.servers.cpu_models[i].count` | assets | `cpu_model=<value>` (`unknown` included) |
| `hardware.servers.memory[i]`, `disks[i]`, `virtualization[i]` `.count` | assets | `memory=<value>`, `disks=<value>`, `virtualization=<value>` |
| `software.items[i].hosts` | assets | `product=<product_key>` |
| `software.items[i].installs` | page | `/inventory/software/<product_key>`, Installed on tab (the installation list: its `total` is `installs`) |
| `software.items[i].version_count` | page | `/inventory/software/<product_key>` (its Versions tab has that many rows) |
| `software.items[i].distribution.top[j].hosts`, `product.versions[j].hosts` | assets | `product=<product_key>&product_version=<version>` |
| `product.versions[j].installs` | page | the installation list filtered with `version=<version>` |
| `product.unversioned.installs` | page | the installation list filtered with `version=<unversioned.address>` (`__none__`) |
| `product.header.hosts` / `.installs` / `.version_count` | assets / page / page | as the catalog row's: `product=<product_key>`; the installation list; the Versions tab |
| `software.total` with a `category` filter (category chips) | page | `/inventory/software?category=<c>&hidden=include` (its `total` is the products in the category) |
| `software.hidden_products` | page | `/inventory/software?hidden=include` (its `total` is `total + hidden_products`) |
| `software.unmatched_titles` | page | `/inventory/software?unmatched=true` (its `total` is `unmatched_titles`) |
| `publishers.items[i].products` | page | `/inventory/software?publisher=<value>` (the other filters kept) |
| `publishers.total` | page | `/inventory/software` with the same filters |
| `changes.stats.*` | changes | the chip's `category` / `action` / `label` with the same window and filters |
| `asset.changes.total` (flyout, "View all N") | changes | `asset=<asset key>&since=<changes.first_change_at>` (the landed total is exactly `changes.total`; X4) |
| `facets.*.items[i].count` | assets | the current filters plus the facet's own parameter set to `value` (`health` facet: `health=<value>`) |

Numbers that are **not** list totals and carry no drill link: the software figures listed in the last rows above (`distribution.other`, `unversioned.hosts`, the publisher `installs` and `hosts`, `hidden_installs`, the evidence counts and the trend values), `summary.assets.linked_devices` (servers that also have an SNMP row: informational), `os…currency.older` and `.unknown`, `changes.stats.software.paired`, and the counts of components in `hardware.modules` (`count`, `with_serial`) and `hardware.network.*.modules`. The UI renders them as plain text.

---

## 5. Health, freshness, software and the other definitions

**Freshness** compares the asset's `inventory_at` (server: `servers.inventory_updated_at`; device: `devices.identity_updated_at`) with the clock:

| Bucket | Server | Device |
|---|---|---|
| `fresh` | within `fresh_hours.server` (24 h) | within `fresh_hours.device` (2 h) |
| `aging` | older, within `stale_days` (7 d) | older, within `stale_days` |
| `stale` | older than `stale_days` | older than `stale_days` |
| `never` | `inventory_at` is null and the server can report | `inventory_at` is null and the device can report |
| `not_applicable` | `inventory_at` is null and the collection mode is not `agent` (agentless WMI or WinRM, SNMP-only, SSH, none) | `inventory_at` is null and SNMP polling is off (a ping-only device), or the device is a child managed through a controller (`managed_by_device_id`, `poll_mode = via_controller`) |

The boundary instants belong to the fresher bucket (`inventory_at >= now − threshold`). `fresh_hours` is clamped to `stale_days` so the buckets stay in order.

**`not_applicable` (X3).** The agent upload is the only writer of `servers.inventory_updated_at` (the other collection modes have no inventory collector in this release), and a device's identity is stamped by its own SNMP poll (a ping-only device has none, and a controller child's identity arrives from the controller sync, which does not stamp it). An asset in that position never reported and cannot: it is not "missing a report". It stays in the list and is counted (`coverage.not_applicable`, `health.not_collected_by_design`, the `freshness` facet, `freshness=not_applicable`), but it is **excluded** from `fresh_pct`, from `never`, from `stale_or_never`, from the four identity predicates below and from the all-clear. The rule is about an asset that has *never* reported: once an asset has an `inventory_at` it is classified by that age whatever its mode is now.

**Health predicates** (`health=<key>`; `summary.health.<key>` is the count):

| Key | An asset is counted when | Kinds |
|---|---|---|
| `missing_os_version` | no normalised OS version, and the asset can report (not `not_applicable`) | server, device |
| `missing_model` | no hardware model, and the device can report | device |
| `missing_serial` | no serial number, and the device can report | device |
| `unknown_vendor` | no vendor, and the device can report | device |
| `stale` | freshness is `stale` | server, device |
| `never` | freshness is `never` | server, device |
| `software_truncated` | the agent's software list was cut at its cap on the last upload | server |
| `missing_security_agent` | `security_agent=missing`: a complete, normalised software list that shows no endpoint protection (below) | server |

Each count is the `total` of `/assets?health=<key>`. (`vendor=unknown` and `model=unknown` select the same assets *plus* those that cannot report; the predicates are the worklists.)

**Endpoint protection (X1).** `security_agent` is a three-way answer per server, and the three states partition the servers:

| State | A server is in it when |
|---|---|
| `present` | it has at least one software row of category `endpoint-protection`, **or** a running service named `WinDefend` or `Sense` in `server_service_inventory` (built-in Microsoft Defender has no uninstall entry, so a running `WinDefend` (Defender Antivirus) or `Sense` (Defender for Endpoint sensor) service is the evidence) |
| `unknown` | it is not `present` and the question cannot be answered from what the appliance holds. The flyout's `security_agent.reason` names the first of these that applies, in this order: `no_software_list` (no complete software list was ever accepted, or the server holds unkeyed rows that the normalise loop never selects: there is no list to read), `list_truncated` (the agent cut its list at its cap, so a product beyond the cut could be the missing one), `normalising` (rows the loop will still key, or re-key after a rule-pack revision, are waiting), `no_service_list` (the software list is complete and normalised and shows no endpoint-protection title, but the server has **no service rows at all**, as with the APM-only agent profile or the services collector switched off: built-in Defender shows only as a running service, so without a service list it cannot be judged) |
| `missing` | it is not `present` and the question can be answered: baselined, list not cut, every row keyed, **and** a service list exists in which neither `WinDefend` nor `Sense` is running |

The category `endpoint-protection` ("Endpoint protection") holds only antivirus, endpoint-protection-platform and EDR engines and sensors (Defender for Endpoint sensor, CrowdStrike Falcon, SentinelOne, Sophos, ESET, Trend Micro, Symantec/Broadcom endpoint, McAfee/Trellix, Bitdefender, Kaspersky, Cortex XDR, Carbon Black, Malwarebytes endpoint, Cylance, Webroot, Avast/AVG business and the like). It does **not** hold the ZenPlus Agent (category `monitoring`), VPN and SSE clients, scanners, password managers, Sysmon, Wazuh and other host-intrusion tools, or management agents (ESET PROTECT, Kaspersky Network Agent, McAfee Agent, Carbon Black App Control): those stay in `security` and never count. The flyout's `security_agent.reason` says why a server is `unknown` (the four values above; `null` otherwise). The Overview's `unknown` line, the `security_agent=unknown` filter, the facet count and the daily snapshot all use the same rule, so a server without a service list is in `unknown` and never in `missing_security_agent`. A device has no state.

**Categories.** The software category vocabulary is `browser`, `security`, `endpoint-protection`, `remote-access`, `database`, `web-server`, `runtime`, `development`, `productivity`, `monitoring`, `backup`, `virtualization`, `networking`, `driver`, `os-component`, `update`, `other`. The categories hidden by default are `update`, `driver`, `os-component` and `runtime`.

**Equivalence key of a version** (`versioning.equivalence_key`): the text trimmed of blanks, lower-cased, with trailing `.0` parts removed, but never the first character, so the key of a non-blank version is never empty (`.0` stays `.0`, `..0` is `.`). `7.2`, `7.2.0` and `7.2.0.0` are one version; `10.0` is `10`; `1.10` and `1.100` are different; `V1` and `v1` are one. Every filter and every count that names a version (`os_version`, `product_version`, `installations?version=`, the OS tree's version nodes, the catalog's `version_count`, the summary's `versions`, the hardware `firmware_versions`) compares and groups on this key, which is why a number on a row equals the total of the list it links to. A blank version (null or only whitespace) is *no version*.

**Version ordering** uses the scheme of the OS family (`services/versioning.py`): Windows builds numerically, FortiOS / PAN-OS (with hotfix ordering) / Junos / classic IOS and IOS XE (train-aware: a pair from different trains is *not comparable*) and semver-ish for software. "Not comparable" yields `label: null` and `currency: null`, never a guess.

**Hidden software categories** (`update`, `driver`, `os-component` and `runtime` by default) are excluded from the Overview's software figures, from the flyout's top list and from the catalog (unless `hidden=include` or the category is asked for by name); they are still stored, and `server.software.hidden` says how many a server has.

**Software figures.** A product is the normaliser's `name_key` (`google-chrome`); the software table keeps one row per (server, raw title). `hosts` is the number of distinct servers with a row of the product, `installs` the number of rows, `version_count` the number of distinct non-blank versions (by equivalence key). A **missing version is not a version**: rows with no or a blank version are reported as `unversioned` on the product page and addressed in the installation list as `version=__none__` (a version spelled "unknown" is an ordinary version). The category of a product is the one its rows carry (`max(category)`; the packs give every row of a key the same one), and the hidden-category rule works on whole products, so at the default settings the catalog `total` equals `summary.software.products` and the sum of the rows' `installs` equals `summary.software.installs` (the Overview works per row; the two agree for every key the packs produce).

**Version schemes and ranks.** The comparators of `versioning.py` compare versions of one scheme. A software row's scheme follows its source: `dpkg` and `rpm` packages use their own, everything else (`hklm`, `hkcu`, Windows installers) is `semverish`. A product with rows of several schemes has one **group per scheme**; ranks restart in every group (`rank` 1 is the newest version of that group), so an Ubuntu package version is never ranked against a Windows one. The group with more installs comes first (ties: semverish, dpkg, rpm) and its newest version is the product's `latest_version`. Inside a group two spellings of one version (`1.0` and `1.0.0`) share a rank, the longer spelling first; a version the scheme cannot parse (`nightly-build`, `2.0-SNAPSHOT`) has `rank: null` and follows the ordered ones. **N, N-1 and older** are the generations of the observed versions (`rank` 1, 2, 3 and up; *older* is two or more versions behind the newest), "latest observed" in the sense of section 3.5, not the vendor's latest. The OS tree's build currency uses the same three words with the same meaning.

**Normalisation status** (`norm_status`). The software rows keep the product key, the publisher key and the category but not how the key was reached, so the status is recovered from them, following the pipeline of `inventory_normalize.normalize_software`: the key is one of the keys of the catalog rules, or the category is one only noise rules assign (`update`, `driver`, `os-component`): `normalized`; otherwise the generic fallback made the key, and it is `publisher_only` when the publisher key is a known alias and `unmatched` when it is not. `server/tests/test_inventory_software.py` checks the derivation against the real normaliser for every product of the fleet and a corpus of titles, so a pipeline change that breaks it fails there. The product page's `evidence[].status` re-runs the normaliser on each raw title.

**Settings and what reads them.** `fresh_hours`, `stale_days` and `hide_categories_by_default` change the Overview, the Assets freshness buckets and the catalog at once (the `PUT` drops the caches of the serving process); `change_retention_days` and `snapshot_hour` are read by the loops on their next pass; `trends_enabled` switches the display of `/trends` only, recording continues.

---

## 6. Fixtures and measured performance

`dashboard/src/pages/inventory/fixtures/`:

| File | Request |
|---|---|
| `summary.json` | `GET /summary` |
| `assets.json` | `GET /assets?page_size=50` |
| `facets.json` | `GET /assets/facets` |
| `asset-server.json` | `GET /assets/server/<id of srv-w19-03>` (linked SNMP twin, in the scoped team's tag) |
| `asset-device.json` | `GET /assets/device/<id of sw-c9-04>` (IOS XE switch, modules, a firmware change) |
| `asset-device-ha.json` | `GET /assets/device/<id of fg-04>` (FortiGate, HA state) |
| `os.json` | `GET /os` |
| `hardware.json` | `GET /hardware?vendor=Cisco` |
| `changes.json` | `GET /changes?page_size=50` |
| `software.json` | `GET /software?page_size=50` (the whole default catalog: 24 products) |
| `software-publishers.json` | `GET /software/publishers` |
| `software-product.json` | `GET /software/git` (five versions in two schemes: Windows and Ubuntu packages) |
| `software-installations.json` | `GET /software/git/installations?page_size=50` |
| `trends.json` | `GET /trends?metric=assets` (13 days of snapshots over a span of 14 days, one day missing) |
| `settings.json` | `GET /settings` |
| `export-assets.csv` | `GET /export?dataset=assets&tag=team-edge` with `inventory.export` (UTF-8 with a byte-order mark, CRLF; listed under `samples` in `index.json`, not under `fixtures`, because it is not JSON) |
| `summary-scoped.json`, `assets-scoped.json`, `changes-scoped.json`, `software-scoped.json`, `software-product-scoped.json`, `software-installations-scoped.json`, `trends-scoped.json` | the same requests as a user scoped to the tag `team-edge` |
| `index.json` | the manifest: for each file the request, parameters and caller; the pinned clock; the asset counts |

The fleet: 145 assets (60 servers, 85 devices; 5 more device rows are the SNMP twins of servers and are not counted), `now = 2026-10-08T12:00:00Z`, change capture started 45 days earlier. Servers: Windows Server 2016 / 2019 / 2022, Windows 10 / 11, Ubuntu and RHEL, four servers with no OS, four registered but never reported, two registered in a collection mode that cannot upload an inventory (`not_applicable`), two with truncated software lists (`security_agent=unknown`), and endpoint protection on most Windows servers but not all: a CrowdStrike Falcon sensor on most, two servers protected only by a running Defender service, three that run only the ZenPlus Agent (which does not count as protection) and one whose Defender for Endpoint `Sense` service is stopped. Devices: FortiGate (two managed through a controller: `not_applicable`), Palo Alto, Cisco IOS XE / IOS / NX-OS, Aruba AOS-CX, Juniper, F5 and Ubiquiti gear, eight devices that tell us nothing (four of them ping-only: `not_applicable`) and some with gaps (no model, no serial, never polled, stale). The scoped user sees 15 assets and 17 software products (9 more in the hidden categories).

---

## 6.5 Measured performance

**How to read this section.** Phase 1 was measured twice. The tables headed *baseline* were measured when the endpoints were built; the plans and the reasoning under them still describe the code. The review round then changed the read model (endpoint protection and the `not_applicable` freshness in the asset union, equivalence keys for versions, the summary's sprawl basis and `data_status`, the software catalog's cache and warmer, the export's admission control); the tables headed *after the review round* were measured on that code, on the same kind of fixture, and where the two differ the later numbers are the ones to read. Every figure is a measurement with the service functions called directly on a scratch PostgreSQL 16 on a shared 4-CPU development host, not a promise: the harnesses are not part of the repository, and the contract does not assert wall-clock times.

### After the review round: the asset read model

Fleet: **5,000 servers x about 400 software rows (1,995,000 rows), 10,000 network devices, 1,000,000 change events and 1,250,000 service rows** (the Defender evidence of endpoint protection reads `server_service_inventory`). Scopes `team-a` + `prod` (about 40 percent of the assets) and `site-3` (a single site).

| Request | p50 ms | p95 ms |
|---|---:|---:|
| `/summary`, cold, unrestricted | 230 to 265 (repeated runs) | |
| `/summary`, cold, scope `team-a` + `prod` | about 255 | |
| `/summary`, cold, scope `site-3` | about 137 | |
| `/summary`, cached | 0.3 | |
| `data_status` (the normalisation backlog count) | 1.7 | 3.3 |
| `/assets` default page | 34 | 37 |
| `/assets/facets` | 93 | 101 |
| `/assets/{kind}/{id}`, a server | 6.5 | 10 |
| `/os` | 29 | 31 |
| `/hardware` | 48 | 53 |
| `/changes`, 7 days (the default) | 15 | 61 |
| `/changes`, 90 days | 89 | 100 |
| `/changes`, 400 days (the whole retention, 1 M events) | 199 | 248 |
| `/changes`, 90 days, a single-site scope | 31 | 39 |
| `/changes`, `kind=device`, a wide scope (about 40 percent) | 175 | 217 |

The cold summary stays under its 300 ms budget with the version-sprawl pass in it only because the pass is bounded: up to 400,000 visible software rows it reads every (product, version) pair (exact), above that it ranks the most installed products that hold a quarter of the rows (at least 64, at most 2,000 products) and says so in `software.sprawl_basis`; reading all 2 million rows costs 0.3 s on this host, which alone is the whole cold budget. Endpoint protection (`sec_state`) is three joins over small sets (the rows of category `endpoint-protection`, the running Defender services, the rows not yet keyed; each has a partial index); the union joins them only for a request that needs them (the `security_agent` filter, the health predicate, the facets and the summary).

### Baseline: measured when the endpoints were built (before the review round)

Fleet: **5,000 servers x 400 software rows (1,998,500 rows), 10,000 network devices (50,798 ENTITY-MIB rows), 1,000,200 change events spread over the 400-day retention**, built with set-based SQL on a scratch PostgreSQL 16.15 (default settings: `shared_buffers` 128 MB, `work_mem` 4 MB, JIT available) on the 4-CPU development host while other jobs were running (load average about 2.8). The service functions were called directly with a pooled connection, 6 to 10 runs per row; scopes `team-a`, `team-d` and `team-quiet` match 40 percent, 10 percent and 0.7 percent of the assets. Budgets are doc 02 section 13: list endpoints p95 under 400 ms, summary under 300 ms cold and under 20 ms cached.

| Request | p50 ms | p95 ms |
|---|---:|---:|
| `/summary`, cold, unrestricted | 186 | 212 |
| `/summary`, cold, scope 40 percent of assets | 221 | 279 |
| `/summary`, cold, scope 10 percent | 200 | 209 |
| `/summary`, cold, scope 0.7 percent | 75 | 94 |
| `/summary`, cached | 0 | 1 |
| `/assets`, 18 filter and sort variants (default, page 100, sorts, `q`, kind/OS, vendor/model, freshness, health, `product`, `product_version`, memory, tags, `page_size=500`, module) and 3 scopes, 210 requests | | 66 (worst) |
| `/assets` default, scope 40 percent | 30 | 31 |
| `/assets?health=missing_security_agent` (the then definition, category `security`: scanned all 2 M software rows, the slowest list; X1 replaced it, see above) | 62 | 66 |
| `/assets/facets`, no filter | 140 | 148 |
| `/assets/facets`, kind and OS family chosen | 114 | 117 |
| `/assets/facets`, scope 10 percent | 79 | 83 |
| `/assets/{kind}/{id}` server (400 software rows) | 5 | 10 |
| `/assets/{kind}/{id}` device | 7 | 8 |
| `/os`, all assets | 22 | 24 |
| `/os?kind=device` | 16 | 19 |
| `/hardware?vendor=Cisco` | 101 | 111 |
| `/changes`, 7 days (the default), unrestricted | 17 | 20 |
| `/changes`, 30 days | 56 | 58 |
| `/changes`, 90 days (the widest window the page offers) | 144 | 151 |
| `/changes`, 400 days (the whole retention, 1 M events scanned) | 433 | 441 |
| `/changes`, 90 days, scope 40 percent | 216 | 260 |
| `/changes`, 400 days, scope 40 percent | 378 | 414 |
| `/changes`, 400 days, scope 0.7 percent | 26 | 26 |
| 8 clients at once, each a list plus a facets request | 241 | 287 |

Every asset list is under 70 ms, facets under 150 ms, the OS tree under 25 ms, hardware 111 ms and the summary under 300 ms cold for every scope tried (the 40-percent scope is the slowest at 221 ms median, 279 ms p95). The only requests over 400 ms are the two full-retention `/changes` windows (400 days, 1 M events scanned by three aggregates: 441 ms unrestricted, 414 ms for a scope of 40 percent); no page offers a window wider than 90 days, where `/changes` takes 151 ms (260 ms scoped).

**The scoped change feed (doc 02 section 14), 3 M events.** The natural form (an `EXISTS` on the asset row, newest first) against the asset-driven form this API uses, for a quiet team (100 assets, 200 events, all in the last 4 days) and a dormant team (40 assets, 120 events, all 250 to 390 days old). "naive" is `ORDER BY observed_at DESC LIMIT 50` and a `count(*)` with the same `EXISTS`; "asset-driven" is the whole `GET /changes` (page, statistics, daily series, pairing) for the same scope and window.

| Scope | Window | Events in the table | Naive page ms | Naive count ms | Asset-driven `changes()` ms |
|---|---|---:|---:|---:|---:|
| team-quiet | 7 d | 3 M | 20 | 17 | 29 |
| team-quiet | 400 d | 3 M | 23 | 315 | 26 |
| team-dormant | 7 d | 3 M | 29 | 19 | 15 |
| team-dormant | 400 d | 3 M | 722 | 303 | 21 |
| team-quiet | 7 d | 1 M | 14 | 14 | 31 |
| team-quiet | 400 d | 1 M | 16 | 113 | 27 |
| team-dormant | 7 d | 1 M | 18 | 14 | 15 |
| team-dormant | 400 d | 1 M | 221 | 104 | 22 |

The naive page for the dormant team walks the time index back 250 days before it has 50 matches (the asset-driven form needs 40 index probes); on this host that is 0.72 s at 3 M events (doc 02 section 14 recorded about 4 s on another dataset), the count scans the whole window either way (0.3 s at 3 M), while the asset-driven form answers the whole page, statistics and series in about 25 ms regardless of how many events the table holds. A scope that reaches more than 15 percent of the estate switches to a hash semi-join over the window, which is faster for wide scopes (40 percent, 400 days, 1 M events: 1.15 s per-asset against 0.36 s) and would be slower for narrow ones (0.7 percent: 2.1 s against 25 ms).

At 3 M events (three times the sizing target) the same requests give: `/changes` 7 days 39 ms, 30 days 148 ms, 90 days 462 ms, 400 days 1,057 ms unrestricted; scoped to 40 percent, 7 days 110 ms, 90 days 497 ms, 400 days 779 ms; the summary is unaffected (195 ms cold unrestricted, 228 ms for the 40-percent scope).

**Plans** (`EXPLAIN (ANALYZE, BUFFERS)` on the same fleet):

- The asset union is a sequential scan of `servers` (5,000 rows) and `devices` (10,000 rows) hash-joined to `inventory_identity`; it costs about 10 ms and every filter, sort and facet is evaluated over it, so the list endpoints do not depend on the size of `server_software_inventory`.
- `product` / `product_version` are one semi-join: a bitmap scan of `idx_ssi_name_key` (1,215 rows for a common product, 6 ms for the whole list); the security-agent predicate (`security_agent`, `health=missing_security_agent`, the summary's coverage) was, in the baseline, the only union variant that read the 2 M software rows (a parallel sequential scan, about 50 ms) and was joined only when asked for; since X1 it reads the small sets described above instead.
- Facets are one statement over one materialised union (`WITH a AS MATERIALIZED`), ten counted groupings in a single pass.
- Change events: the time index (`idx_inv_events_time`, bitmap heap scan) for the default window; for a restricted scope narrower than 15 percent of the estate one index probe per visible asset on `idx_inv_events_asset` in a lateral subquery (no sequential scan of the events table); a hash join against the window for a wider scope. The three statistics statements are hash aggregates (the previous single grouping-sets statement sorted the window twice and spilled to disk).

**What the measurements changed in the code** (each has a regression test):

- `jit` is turned off and `plan_cache_mode` is `force_custom_plan` for the transaction of every read (`_tune`). The union and its lookups give the planner cost estimates that start JIT compilation (the OS tree for devices took 488 ms with JIT and 15 ms without), and after five executions the driver's prepared statements fall back to a generic plan: with generic plans forced, the product-name lookup of the summary took 580 ms for a scope of 2,000 servers against 10 ms with a custom plan, so whether a request was fast could depend on which scope had been served before it.
- Change statistics are three small aggregates on separate connections instead of one grouping-sets statement (the statistics statement alone: 601 ms to about 190 ms over 1 M events); at most two requests fan out at once, so the shared pool of 30 connections cannot be exhausted.
- A scope that is a large share of the estate reads events as a semi-join (above).
- Text filters drop NUL bytes (PostgreSQL text cannot hold them, so one character of query string was a 500) and a non-array `tags` value is treated as no tags.

### Software catalog, trends, settings and export

#### After the review round

The review round changed the software read model: versions are counted by equivalence key, hosts are merged across spellings, the CPU-bound work (building a table's rows, ranking a page's or every product's versions) runs in a two-thread pool instead of on the event loop, a request that waits for a shared build gives its connection back first, cached tables are evicted when they expire, the export no longer leaves version lists behind, and every API worker runs a **warmer** that builds the whole-estate table before it can expire (every 40 s while the catalog is in use). Fleet of the re-measurement: **5,000 servers, 1,840,000 software rows, 1,500 products, 36,000 (product, version) pairs**, 10,000 devices; "cold" means nothing is cached in the process (the first request after a restart, which the warmer now prevents from being a user's request), "table warm, page cold" means the per-scope table is cached and the page's own exact figures are read again; scopes of 63, 31 and 0.8 percent of the servers.

| Request | p50 ms | p95 ms |
|---|---:|---:|
| `/software` default (hosts desc, 50), **warm** (any scope) | 15 to 20 | 16 to 62 |
| `/software` default, process-cold, unrestricted | 477 | 556 |
| `/software` default, process-cold, scope 63 / 31 / 0.8 percent | 458 / 371 / 87 | 530 / 379 / 105 |
| `/software` page 2, table warm, page cold (unrestricted / 63 / 31 / 0.8 percent) | 266 / 220 / 189 / 32 | 346 / 335 / 255 / 52 |
| `/software` page 5 by hosts / `page_size=500` by hosts | 558 / 802 | 594 / 963 |
| `/software?sort=name` / `publisher` / `category` / `first_seen` | 67 / 28 / 33 / 23 | 119 / 31 / 34 / 24 |
| `/software?sort=version_count` / `sort=latest_version` | 217 / 383 | 272 / 428 |
| `/software?sort=hosts&order=asc` (reads every product) | 657 | 671 |
| `/software?hidden=include` / `unmatched=true` | 243 / 232 | 244 / 243 |
| `/software?q=...` / `category=security` / `publisher=microsoft` | 6 / 15 / 4 | 6 / 18 / 4 |
| `/software/publishers`, process-cold, unrestricted | 573 | 617 |
| `/software/publishers`, table warm | 3 to 4 | 3 to 5 |
| `/software/{product_key}`, the most installed product (5,000 hosts, 301 versions) | 15 | 16 |
| `/software/{product_key}`, the rarest product | 9 | 10 |
| `/software/{product_key}`, the most installed product, scope 63 percent | 32 | 73 |
| `/software/{product_key}/installations`, by server name / by version (comparator) | 5 / 18 | 6 / 80 |
| `/trends`, 400 days / `/settings` | 3 / 1 | 4 / 1 |
| export `assets`, all 14,750 rows (3.8 MB) | 1,709 | 1,741 |
| export `software`, all 1,500 products | 1,309 | 1,314 |
| export `installations` of the most installed product | 118 | 131 |
| export `changes`, **50,000 rows** (7.9 MB) of the 1,000,200 events | 1,350 | 1,483 |
| export `os` / `hardware` | 30 / 84 | 31 / 85 |

A catalog with a very large number of products is the case the sorts that need a figure for every product have to survive. On a **wide catalog of 24,000 products and 519,000 (product, version) pairs** the review round measured `sort=version_count` at 941 ms (3,848 ms before), `sort=latest_version` at 3,333 ms (3,938 ms before) and `sort=hosts&order=asc` at 1,104 ms (2,946 ms before). More important than the time is what it no longer does to the other requests: the event loop is held for 75 ms (was 2,940 ms) by `version_count` and 157 ms (was 3,145 ms) by `latest_version`, because the ranking runs in the worker pool, so a slow sort no longer stalls every other request of the API process. `sort=latest_version` is the one sort whose cost grows with the catalog, and it refuses a catalog of more than 40,000 selected products (`422`, section 3.8). The warmer brings the first unrestricted catalog request after a restart from 460 ms to 15 ms and the publisher facet from 545 ms to 3 ms (one table build of about 0.8 s per worker every 40 s while the catalog is in use). The export of the software catalog measures 1.3 s against 0.2 s in the baseline run, which reused figures that earlier requests had left in the shared table; the export now reads its own and leaves nothing behind. It is far inside its limit (10,000 rows in under 5 s).

#### Baseline: measured when the endpoints were built (before the review round)

The text and tables that follow describe the same endpoints before the review round; the plans still apply, and the cold figures are superseded by the table above.

Fleet: **5,000 servers x 400 software rows (1,998,500 rows, 2,600 products from 420 publishers, three of which own 30 percent of the rows, up to 301 versions per product)**, 10,000 network devices and 1,000,200 change events, on the scratch PostgreSQL 16.15 described above (default settings, JIT available), the 4-CPU shared development host (load average 2.7 to 3.4 from other jobs). The service functions were called directly with a pooled connection, 6 to 8 runs per row (3 for the exports). "Cold" means nothing is cached in the process; "table warm, page cold" means the per-scope table is cached and the page's own exact figures are read again; "warm" is the same request repeated. Scopes `team-a`, `team-d` and `team-quiet` match 40 percent, 10 percent and 0.7 percent of the servers. The budget is 400 ms (p95) for a list endpoint.

| Request | p50 ms | p95 ms |
|---|---:|---:|
| `/software` default (hosts desc, 50), **cold**, unrestricted | 505 | 526 |
| `/software` default, cold, scope 40 percent | 260 | 315 |
| `/software` default, cold, scope 10 percent | 186 | 242 |
| `/software` default, cold, scope 0.7 percent | 71 | 107 |
| `/software` default, warm (any scope) | 21 | 65 |
| `/software` page 2, table warm, page cold, unrestricted | 194 | 244 |
| `/software` page 2, table warm, page cold, scope 40 percent | 101 | 195 |
| `/software` page 5 by hosts (table warm) | 356 | 374 |
| `/software?page_size=500` by hosts (table warm) | 530 | 666 |
| `/software?sort=name` | 270 | 298 |
| `/software?sort=publisher` / `category` / `first_seen` | 26 | 28 |
| `/software?hidden=include` | 238 | 273 |
| `/software?unmatched=true` | 247 | 271 |
| `/software?q=...` / `category=security` / `publisher=microsoft` | 37 | 38 |
| `/software?sort=hosts&order=asc` (reads every product) | 466 | 506 |
| `/software?sort=latest_version` (reads every (product, version) pair) | 746 | 767 |
| `/software?sort=version_count` | 860 | 905 |
| `/software/publishers`, cold, unrestricted | 552 | 600 |
| `/software/publishers`, cold, scope 40 percent | 322 | 372 |
| `/software/publishers`, warm | 2 | 2 |
| `/software/{product_key}`, the most installed product (5,000 hosts, 301 versions) | 14 | 16 |
| `/software/{product_key}`, scope 10 percent | 21 | 24 |
| `/software/{product_key}`, the rarest product | 3 | 6 |
| `/software/{product_key}/installations`, 5,000 rows, by server name | 5 | 7 |
| `/software/{product_key}/installations?sort=version` (comparator, 5,000 rows) | 22 | 63 |
| `/software/{product_key}/installations`, scope 10 percent | 9 | 10 |
| `/trends`, 400 days | 2 | 4 |
| `/settings` | 1 | 1 |
| export `assets`, all 15,000 rows (3.7 MB) | 1592 | 1644 |
| export `software`, all 2,600 products | 259 | 909 |
| export `installations` of the most installed product (5,000 rows) | 132 | 144 |
| export `changes`, **50,000 rows** (7.7 MB) of the 1,000,200 events | 1604 | 1690 |
| export `os` / `hardware` | 22 | 23 |

**Under the budget** at 5,000 servers x 400 packages: every warm request (16 ms), every restricted caller's cold request (the 40-percent scope is the slowest at 260 ms median, 315 ms p95), the product page (14 ms), the installation lists (5 to 22 ms), trends and settings (2 ms) and all the exports (50,000 change events in 1.6 s, the 15,000 assets in 1.6 s; the exit criterion is 10,000 rows in under 5 s).

**Over the budget at this extreme size** (honest list): the first catalog request of an unrestricted caller after the whole-estate table has gone more than five minutes without being used (cold 505 ms median, 526 ms p95: a 150 ms scan of the 2 M rows plus the exact host counts of the 100 or so products that provably hold the first page, a further 150 to 200 ms; within the five minutes after a table's first minute a page in use is served the old table at once while a new one is built, so its callers do not wait); the same cold request of the publisher facet (552 ms: it adds a 360 ms count of hosts per publisher); and the sorts that need a figure for every product, which are rare clicks and are kept for a minute afterwards: `hosts` ascending 466 ms, `latest_version` 746 ms and `version_count` 860 ms (the comparator over 50,589 (product, version) pairs), and `page_size=500` by hosts 530 ms. On a fleet a tenth of this size every one of them is under 150 ms. The documented escape hatch (doc 02 section 13: a rollup table refreshed by the normalise loop, which needs a migration) is not built.


**Plans** (`EXPLAIN (ANALYZE)` on the same fleet):

- The light table (`installs`, `category`, `publisher_key`, first-seen per product) is a **parallel sequential scan with a partial hash aggregate** (154 ms at 2 M rows, 91 ms for a 10-percent scope, where a bitmap scan of the primary key narrows the rows first). Left to itself the planner walks `idx_ssi_name_key` in order, which reads the heap at random and takes 518 ms, so the statement turns index scans off for its own transaction and back on.
- The exact host counts and version distribution of a set of products are one `GROUPING SETS ((name_key, version), (name_key))` statement with `count(DISTINCT server_id)` over a **bitmap scan of `idx_ssi_name_key`**: 308,615 rows for the 64 most installed products in 233 ms, split over two connections when there are 24 products or more. `count(DISTINCT server_id)` for every product at once costs about one second at this size (a sort or a hash of 1.9 M (product, server) pairs), which is why only the products that matter are counted: `hosts <= installs` and `hosts <=` the visible servers bound every product outside the candidate set (a regression test starts from one candidate and must give the same pages as reading all 31 products).
- The (product, version) pairs behind `sort=latest_version` and `sort=version_count`: the same parallel scan with a partial hash aggregate (190 ms, 50,589 groups), ranked in Python.
- The sample row and the last change of 50 products: nested loops of index probes (1.7 ms and 0.06 ms). The product page's evidence is one grouped statement over the product's own rows.
- Publisher hosts: `(publisher_key, server_id)` pairs are few (publishers x servers), so the distinct is a small hash aggregate over a parallel scan with the product keys as a hashed array filter (360 ms); it is kept with the table per filter signature (warm: 2 ms). Joining the product keys to the table with a nested loop of index probes took 6.5 s and was replaced.
- Installation lists: the product's rows by `idx_ssi_name_key`, a hash join to `servers`, a window count and a top-N sort (5.7 ms for 5,000 rows).
- Export: the lists' own functions, 500 rows per page. The change feed pages by keyset `(observed_at, id)` through the time index (2 ms per page); the list's presence check of each page (a subquery on `servers.device_id`, which has no index, 60 ms per 500 rows at 5,000 servers) is replaced by one aggregate (2 ms) and the pairing statement is the list's own (1.5 ms).

**What the measurements changed in the code** (the results are pinned by `test_inventory_software.py`, `test_inventory_export.py` and `test_inventory_trends_settings.py`; the plan choices themselves are measured here, not asserted):

- The light table and the (product, version) pairs turn index scans off for their transaction (518 to 154 ms).
- `hosts` is read for candidates and page products only, with the proof of section 3.8 (about 200 ms instead of one second for the default page); a first round of twice the page's end plus 14 products avoids a second round trip when hidden products and ties make a tight one fall short.
- The exact read is split over two connections when it covers 24 products or more (safe under `MAX_FANOUT`).
- The publisher facet counts hosts per (publisher, server) (6.5 s to 0.36 s) and caches the result with the table; unknown publishers are labelled by one aggregate of the smallest raw `vendor` text per publisher.
- The change-feed export uses its own presence check and keyset paging (50,000 rows: 7.9 s to 1.7 s).
- The whole-estate table is served stale for up to five minutes while a fresh one is built on a session of its own; a restricted caller's table never is.

---

## 7. What later waves add

The router, the service modules and the schemas are structured to be extended without touching the endpoints above: the asset union and the filters are in `inventory_query.py` (`asset_union_sql`, `_conditions`), the software catalog is in `inventory_software_query.py`, the handlers are thin, and a new list endpoint reuses `AssetFilters` through the `asset_filters` dependency.

| Endpoint (later phase) | Notes |
|---|---|
| `PATCH /inventory/catalog/products/{product_key}`, rules, `GET/POST/DELETE /inventory/views` (P2) | Catalog rules, product attributes and saved views need `inventory.manage` and a forward migration (the number is taken when P2 starts; 155 is intentionally unused, see `00-INDEX.md`). The catalog's `norm_status` then comes from the stored product row instead of the derivation of section 5, and a rollup table refreshed by the normalise loop (doc 02 section 13) replaces the per-scope table cache if the catalog budget requires it. |
| `GET /inventory/lifecycle` (P4) | EOL stages; `summary.lifecycle` and the lifecycle-aware N / N-1 of the OS and Software pages. |
| `product_version=__none__` | The assets vocabulary has no sentinel for "no version" (the installation list has one, `unversioned.address`); the software pages show `unversioned.hosts` as text. A sentinel would make the number a link. |

`test_inventory_route_guards.py` asserts that every non-GET route declares more than `inventory.view` (today only `PUT /settings`, which declares `inventory.manage`) and that any `…/export` route declares `inventory.export`.
