# 05 — Phase 1 API Contract (Inventory read endpoints)

*Status: implemented in `server/app/api/v1/inventory.py`, `server/app/services/inventory_query.py`, `inventory_software_query.py`, `inventory_trends_settings.py`, `inventory_export.py` and `server/app/schemas/inventory.py` · 2026-10-09 · regenerated from the final code after the Phase 1 review fix round. This is the contract the dashboard is built against. Where it differs from the sketches in `02-ARCHITECTURE-DATA-MODEL-API.md` §8, this file wins (phase 1 scope decisions S1 to S6 and the review-round decisions X1 to X5 below). Every JSON example in this file is a real response, copied from the golden fixtures in `dashboard/src/pages/inventory/fixtures/` (lists are cut short where the text says so; no value is edited).*

**This file is generated.** Do not edit it: edit `scripts/dev/inventory-contract/contract.template.md` (prose) or the code it reads, and run `python scripts/dev/inventory-contract/build_contract.py` (`--check` says whether the committed file is stale; the README next to the generator explains the inputs). The limits, enums, defaults and column lists named below are read from the server code when the file is generated, and the examples are cut from the golden fixtures, so neither can drift from the implementation without a test or the generator noticing.

The golden fixtures are produced by `server/tests/test_inventory_fixtures.py` from a deterministic seeded fleet (@@G:summary.assets.server@@ servers, @@G:summary.assets.device@@ network devices in the asset union plus @@G:index.assets.snmp_twins_not_counted@@ SNMP twins of servers, @@G:software.total@@ visible software products, @@G:changes.total@@ change events in the 7-day window, clock pinned to `@@G:index.pinned_now@@`). The test compares the server's real response with the checked-in files and fails on drift. Regenerate them with `ZENPLUS_UPDATE_FIXTURES=1 python -m pytest server/tests/test_inventory_fixtures.py`, then regenerate this file.

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
| Timestamps | UTC ISO-8601 with a `Z` suffix and whole seconds: `2026-10-08T12:00:00Z`. `generated_at` is the server clock at the time the figures were computed (for `/summary` that can be up to @@F:summary_ttl_s@@ s ago, see caching; for the software catalog it is the age of the table the figures were read from). Ages are whole seconds (`*_age_s`), measured against `generated_at`. |
| Nulls | A field is **always present**; a missing value is `null`, never omitted, so `null` ("unknown") is distinguishable from "this API version does not have the field". Lists are never `null`, except where a field below says so (`hardware.servers.virtualization`, `hardware.matrix`). |
| Unknown is a value | Nothing is dropped for being unknown. `os_family` is the string `"unknown"` when the OS was not identified; the other OS fields are `null`. An unknown value is addressed in a filter as the literal `unknown` (`os_family`, `os_product`, `os_release`, `os_version`, `vendor`, `model`, `cpu_model`, `memory`, `disks`, `virtualization`, `module_model`, and `security_agent`, which has `unknown` as one of its three values), never as a free string. **Endpoint protection** that cannot be judged (no software list yet, a list the agent cut short, rows not yet normalised) is `unknown`, not `missing`; an asset that cannot report an inventory by design is `not_applicable`, not `never` (section 5). |
| `data_status` | `GET /summary`, `/software`, `/software/publishers` and `/software/{product_key}` carry `data_status: {normalising: boolean, pending_software_rows: number, pending_identity_rows: number}`: the rows of the caller's scope that the normaliser has not (re)keyed yet and WILL pick up (a software row with no product key or keyed by an older rule-pack revision; an identity row that is dirty or old). A software row the normalise loop never selects (one with no `source` on a server that is not a Windows machine by `os_type` or by its OS name: it waits for that server's next upload, which carries the source) is not backlog: the count uses the loop's own eligibility rule, so the message cannot stay up for a row nothing will process; such a server is `unknown` for endpoint protection with the reason `no_software_list` (section 5). While `normalising` is `true` the figures that depend on them (software, endpoint protection, OS) are incomplete and every Inventory page says so ("Normalising existing software lists; figures are incomplete"). An empty catalog with `pending_software_rows > 0` means rows are not keyed yet, not an empty estate. The count is cheap (partial indexes; one probe per process for rows of an older revision). |
| Errors | `401` unauthenticated · `403` no `inventory.view` · `404` flyout of an unknown, malformed or out-of-scope asset (one body for all three, `{"detail":"Asset not found"}`) · `404` software product page or installation list of a product that is unknown or has no host in the caller's scope (one body for both, `{"detail":"Product not found"}`) · `422` invalid parameter · `429` an export while @@F:max_exports@@ exports already run in the API process or the caller already has @@F:max_exports_per_user@@ (header `Retry-After: @@F:retry_after_s@@`) · `503 {"detail":"The audit log is unavailable, so nothing was done. Try again; if it persists, contact an administrator."}` when the audit entry of `PUT /settings` or of an export cannot be stored (nothing is saved, nothing is sent). A `422` `detail` is **either a string** (a vocabulary error raised by the service, for example `"product_version needs product"`) **or the standard FastAPI array** of `{loc, msg, type}` objects (a value outside an enum or range, or a text longer than its limit). Show the string, or `detail[0].msg`. No endpoint returns `5xx` for bad input; the only `5xx` the contract names is the audited `503` above. |
| Caching | `/summary` is cached (@@F:summary_ttl_s@@ s, in process, per scope and settings, built once for concurrent callers). The software catalog and its publisher facet read a per-scope table that is cached for @@F:table_ttl_s@@ s; the whole-estate table is also kept fresh in the background by a warmer (section 3.8). Everything else is computed per request. No `Cache-Control` headers are set except `no-store` on the export; the dashboard's React Query `staleTime`/`refetchInterval` is the client cache (doc 03 §8). |
| Settings used | `system_settings['inventory']` (doc 02 §9, section 3.13): `fresh_hours` (server @@F:default_fresh_server@@ h, device @@F:default_fresh_device@@ h by default), `stale_days` (@@F:default_stale_days@@), `hide_categories_by_default` (@@F:default_hidden@@), `trends_enabled`. They are read on every call (the summary cache key includes them); saving them drops the summary and catalog caches of the API process that served the `PUT` (other workers keep theirs for up to @@F:table_ttl_s@@ s). |
| Pagination | `page` (1-based, ≤ @@F:max_page@@) and `page_size` (default @@F:default_page_size@@, **max @@F:max_page_size@@**). A page past the end is `200` with `items: []` and the real `total`. |
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
| X1 | **Endpoint protection.** The software category `security` stays the broad bucket (VPN and SSE clients, scanners, password managers, Sysmon, Wazuh, management agents, ...). A separate category @@F:ep_category@@ ("Endpoint protection") holds only antivirus, endpoint-protection-platform and EDR engines and sensors. The Assets filter `security_agent` (`present` \| `missing` \| `unknown`) and the health key `missing_security_agent` keep their names (vocabulary and URLs are pinned) but are **defined on @@F:ep_category@@ plus, for built-in Microsoft Defender, a running service** from the allowlist @@F:ep_services@@ in `server_service_inventory`. A server whose software list is cut (`software_truncated`), absent or not yet normalised is `unknown`, never `missing`. User-visible copy says "Endpoint protection". |
| X2 | `data_status` on the summary and the software responses (Conventions above). |
| X3 | Freshness has a fifth value, `not_applicable`, for assets that cannot report an inventory by design (section 5). They stay in the list, are counted, and are drillable with `freshness=not_applicable`, but are **excluded** from `coverage.fresh_pct`, from `never`, from `stale_or_never`, from the four identity health predicates and from the all-clear; `summary.health.not_collected_by_design` counts them. |
| X4 | `GET /changes` carries `history_start` (when change capture began on this installation) and `retention_days`; the flyout carries `changes.first_change_at`, so a "View all N" link with `since=first_change_at` lands on exactly N events. |
| X5 | `runtime` joins the default hidden software categories (@@F:default_hidden@@). The toolbar label of the pages is data-driven from `hidden_categories`. |

---

## 2. The asset filter vocabulary

One set of names, used by `GET /inventory/assets`, `GET /inventory/assets/facets`, the export endpoint (`dataset=assets`) and the address bar of `/inventory/assets`. Every filter is optional; an empty string counts as unset; filters combine with AND. Text is trimmed, and a text longer than @@F:max_text@@ characters is rejected with `422` (the limit applies to the raw string before it is trimmed; `os_family` allows 64, `product` 160, `source` and `virtualization` 32). The one exception is `tag`, which is repeatable and has no length check: a tag is cut to @@F:max_text@@ characters, duplicates are dropped and at most 20 are used.

| Parameter | Type · values | Matches |
|---|---|---|
| `q` | string, at most @@F:max_text@@ characters | Free text, case-insensitive contains (`%` and `_` are literal) over name, IP, serial, model, vendor, OS product and OS version. |
| `kind` | `server` \| `device` | The asset kind. |
| `os_family` | string, case-insensitive; `unknown` | Normalised OS family: `windows`, `linux`, `fortios`, `panos`, `ios`, `ios-xe`, `nx-os`, `junos`, `tmos`, `aos-cx`, `other`, `unknown`, … (the values present are listed by the `os_family` facet). |
| `os_product` | string; `unknown` | OS product, case-insensitive (`Windows Server`, `FortiOS`, `IOS XE`). `unknown` = no product. |
| `os_release` | string; `unknown` | Release, case-insensitive exact (`2019`, `7.2`, `17.9`). `unknown` = no release. |
| `os_version` | string; `unknown` | Version, matched by its **equivalence key**: trimmed, lower-cased, trailing `.0` parts dropped, so `7.2` also selects `7.2.0` (and `10.0` is `10`) while `1.10` and `1.100` stay different (`10.0.17763.5696`, `17.9.4a`). `unknown` = no version. |
| `vendor` | string; `unknown` | Vendor label, case-insensitive exact (`Cisco`). `unknown` selects every asset with no vendor, whatever its freshness (the health predicate `unknown_vendor` is the worklist: the same assets minus those that cannot report by design). A server takes its vendor, model and serial from its linked SNMP device when the agent has none. |
| `model` | string; `unknown` | Hardware model, case-insensitive exact; `unknown` selects every asset with no model (`missing_model` is the worklist). |
| `site` | string | Server: the site name. Device: its location text. Case-insensitive exact. There is no `unknown` sentinel: the text `unknown` matches a site of that name. |
| `tag` | string, **repeatable** | The asset must carry **every** tag given (case-insensitive). Up to 20. |
| `source` | @@F:sources@@ | The asset has this collection source (an asset can have several: a server that is also polled over SNMP has `agent` and `snmp`). Not validated against the list: the text is trimmed and lower-cased, and a value no asset has (`bogus`) is `200` with an empty list, not a `422`. |
| `freshness` | @@F:freshness_filters@@ | Inventory freshness bucket (section 5). `stale_or_never` is the union "not reporting"; it does not include `not_applicable`. |
| `health` | @@F:health_keys@@ | One inventory-health predicate (section 5). Single-valued: two predicates cannot be combined. |
| `product` | string (software `product_key`) | Servers that have this product installed. Implies `kind=server`. |
| `product_version` | string, **requires `product`** | Only servers with this version of the product, matched by equivalence key as `os_version` is (`422` without `product`). |
| `cpu_model` | string; `unknown` | Servers by CPU model (exact, case-insensitive). Servers only. |
| `memory` | @@F:memory_buckets@@ | Servers by installed memory bucket: ≤ 8, 9–16, 17–32, 33–64, 65–128, > 128 GiB. |
| `disks` | @@F:disk_buckets@@ | Servers by physical disk count. |
| `virtualization` | string; `unknown` | Servers by virtualisation (`vmware`, `hyper-v`, `physical`, …). `unknown` = none reported. |
| `security_agent` | @@F:security_agent_values@@ | Servers by endpoint-protection state (X1). `present` = a software row of category @@F:ep_category@@ or a running @@F:ep_services_or@@ service; `missing` = a complete, normalised software list that shows neither; `unknown` = nothing can be said (no software list yet, a list the agent cut at its cap, or rows the normaliser has not keyed yet). The three **partition the servers**: `present` + `missing` + `unknown` = `kind=server`. |
| `module_class` | @@F:module_classes@@ | Network devices that have an ENTITY-MIB component of this class. Devices only. |
| `module_model` | string; `unknown` | Network devices that have a component of this exact model (case-sensitive; `unknown` = a component with no model). Combine with `module_class` for a class + model group. |

`product`, `cpu_model`, `memory`, `disks`, `virtualization` and `security_agent` select servers only (a device never matches); `module_class` and `module_model` select devices only. The rows from `cpu_model` down are the drill-down targets of the Overview and Hardware pages. `dashboard/src/pages/inventory/filters.ts` is the client's copy of this vocabulary.

List-only parameters: `sort` (section 3.2), `order` (`asc` \| `desc`), `page`, `page_size`.

---

## 3. Endpoints

### 3.1 `GET /api/v1/inventory/summary`

The Overview figures for the caller's visible assets. Permission `inventory.view`. No parameters. Cached for **@@F:summary_ttl_s@@ s** per (scope, settings) in the API process; `generated_at` shows when the figures were computed. Cold cost is a handful of aggregates over the asset union and `server_software_inventory` (budget < 300 ms at 5,000 servers × 400 packages and 10,000 devices; measured numbers are in section 6.5).

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
    top: { family: string; product: string | null; release: string | null; count: number }[]   // up to @@F:summary_top_os@@, known families, count desc
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
    top_models: { vendor: string; model: string; count: number }[]   // up to @@F:summary_top_models@@, devices only
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
@@EX:summary@@
```

Notes:

- `software.top` and `sprawl` exclude the hidden categories. `hosts` counts distinct servers; `versions` counts distinct non-empty versions, by equivalence key (`7.2` and `7.2.0` are one version; section 5). `top` is ordered by hosts, then installs, then key; `sprawl` by versions, then installs, then key. A missing version is not a version.
- **`sprawl_basis`.** The sprawl ranking is exact (`scope: "all_products"`) up to @@F:sprawl_exact_rows@@ visible software rows. Above that the cold-summary budget does not allow reading every (product, version) pair, so the ranking covers the `candidates` most installed products of the `products` there are, and `scope` is `top_installed`: the panel must say "ranks the N most installed of M products" (the host ranking `top` is exact either way).
- **`security_agent_coverage`** partitions *all* servers into `covered` (`security_agent=present`), `missing` (`security_agent=missing`, equal to `health.missing_security_agent`) and `unknown` (`security_agent=unknown`: no software list, a cut list or rows not yet normalised; the number is shown, never folded into either side). `total` is the number of servers (`assets.server`), not a filter; the UI renders "of N" as text.
- **Freshness and the all-clear.** `fresh + aging + stale + never + not_applicable = assets.total`. `fresh_pct` is over the assets that can report (`total − not_applicable`) and is `null` when there are none. An asset that cannot report by design is neither fresh nor a gap, so it neither lowers the percentage nor blocks an all-clear; `health.not_collected_by_design.count` says how many there are and links to them.
- `health.missing_os_version`, `missing_model`, `missing_serial` and `unknown_vendor` skip the assets that cannot report by design; `stale` and `never` exclude them by definition.
- `changes_7d` counts change events of the visible assets in the last seven days. `software.upgraded`/`downgraded` count `changed` rows whose two versions can be ordered (section 3.7).
- `data_status` is scope-aware: for a restricted caller it counts the rows of the assets the caller can see.
- The doc 02 §8 sketch also listed `health.unmatched_software` and `lifecycle`: the first arrives with the catalog (P2); `lifecycle` is `null` until P4.

Errors: `401`, `403`. Caching: @@F:summary_ttl_s@@ s per `(sorted scope tags | unrestricted, fresh_hours, stale_days, hide_categories_by_default)`, at most @@F:summary_cache_max@@ entries per process; concurrent cold callers wait for one shared build.

### 3.2 `GET /api/v1/inventory/assets`

One page of the asset union, filtered, sorted and paged on the server. Permission `inventory.view`.

| Parameter | Type · default | Notes |
|---|---|---|
| every filter of section 2 | | |
| `sort` | one of @@F:asset_sorts@@ · default `name` | `os` = OS product, then version. `os_release`, `os_version` and `os` are numeric-aware (`7.2` before `7.10` before `17.9`, missing values last). `freshness` orders fresh, aging, stale, never, not_applicable. `name`, `vendor`, `model`, `os_product` and `site` sort case-insensitively. A value outside the list is `422`. |
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
@@EX:assets@@
```

`total` is the number of rows the filters select (not the page size). The `total` of a list equals the number on the page that linked to it (section 4).

Errors: `401`, `403`, `422` (bad enum, a text longer than @@F:max_text@@ characters, `page`/`page_size` out of range, `product_version` without `product`).

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
- Other facets are ordered by count desc, then label. Caps: @@F:facet_caps@@. Use `distinct` to show "N more".
- `value` is what to put in the URL; `label` is what to show.

Example (`facets.json`, every facet cut to three items):

```json
@@EX:facets@@
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
    items: ChangeRow[]                                 // the @@F:recent_changes@@ newest events of the asset (and of its SNMP twin)
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
    total: number                                      // all software rows of the server (at most @@F:max_server_software@@ are read)
    shown: number                                      // = top.length (@@F:recent_changes@@ at most)
    hidden: number                                     // rows in hidden categories (@@F:default_hidden@@ by default)
    truncated: boolean                                 // the agent's list was cut at its cap
    top: { package_name: string; product_key: string | null; product: string; version: string | null; publisher: string | null
           category: string | null; architecture: string | null; source: string | null; install_date: string | null; first_seen_at: string | null }[]
    security_agent: {                                  // endpoint protection (X1): the answer of the Assets filter security_agent
      state: 'present' | 'missing' | 'unknown'
      present: boolean | null                          // the same as a nullable bool for older readers: true / false / null (unknown)
      reason: 'no_software_list' | 'list_truncated' | 'normalising' | 'no_service_list' | null   // why it is unknown (section 5); null otherwise
      products: string[]                               // the server's endpoint-protection software titles
      services: string[]                               // the running Defender services that count (@@F:ep_services@@) when no title does
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
@@EX:asset-server@@
```

Example, a switch with modules (`asset-device.json`; `identity` cut to 2 of 9, module groups cut to 3 with two items each, `changes.items` cut to one):

```json
@@EX:asset-device@@
```

Example, a FortiGate with HA state from its template values (`asset-device-ha.json`; `identity` cut to 3 of 9):

```json
@@EX:asset-device-ha@@
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
@@EX:os@@
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
@@EX:hardware@@
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
| `since` | ISO-8601 text (at most 64 characters) · `generated_at − 7 days` | Inclusive. Parsed once by the server (`inventory_query.parse_instant`, the parser of every Inventory route that takes a date-time): ISO 8601 with or without an offset, a date, or a Unix timestamp. Anything else, a year the calendar cannot build (`0000-01-01`) and a date outside @@F:window_min_year@@ to @@F:window_max_year@@ are `422` (a date-picker typo must not become a calendar overflow or a `500`). |
| `until` | ISO-8601 text · open | **Exclusive.** Read like `since`, between @@F:window_min_year@@ and @@F:window_max_year@@, and after `since` (`422` otherwise). |
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
- The daily `series` is bounded to @@F:max_series_days@@ days; days are local days in `window.tz`. It never extends past today, even when `until` is later (events are never in the future).
- **`history_start`** is when change capture began on this installation: the time the capture triggers were armed (migration 158 records it under @@F:capture_key@@ in `system_settings`). It is install-wide and **not** the date of the first event, because a quiet estate records nothing for days; changes before it were never recorded, and events older than `retention_days` are removed by the daily purge. The UI says "history starts X; changes older than N days are removed" (doc 03 §3.7). The flyout's `changes.first_change_at` is the asset's own oldest event (section 3.4).
- **`stats.software.paired`** counts a pair when *either* half matches `q` (the free text is applied to the pair, not to each half).

Example (`changes.json`, 7-day default window, four of the 50 rows chosen to show an upgrade, a firmware change, an installation and the removal half of a paired upgrade; `series` cut to the last two days):

```json
@@EX:changes@@
```

Errors: `401`, `403`, `422`.

### 3.8 `GET /api/v1/inventory/software`

The software catalog: one row per **product** (the normaliser's `name_key`), with its version distribution. Permission `inventory.view`. The software inventory is read within the caller's tag scope: a scoped caller's `hosts`, `installs`, versions, footers and rows cover the servers they can see, and a product with no visible host is absent.

| Parameter | Type · default | Notes |
|---|---|---|
| `q` | string | Every word must appear (case-insensitive) in the product key, the product name of a catalog-rule product or the publisher: `google chrome`, `visual c++`, `microsoft`. A query with no letters or digits (`%`) is looked for literally, so punctuation never selects everything. |
| `category` | string, **repeatable** | The product's category (@@F:categories@@); `422` for any other value. @@F:ep_category@@ holds only antivirus, endpoint-protection-platform and EDR engines and sensors (X1); `security` is the broad bucket of everything else security-related. A category named here is never hidden by `hidden=exclude`. |
| `publisher` | string | A `value` of `/software/publishers` (the normalised publisher key; `unknown` for products without one), case-insensitive. |
| `hidden` | `exclude` \| `include` · `exclude` | `exclude` leaves out the products whose category is in `hide_categories_by_default` (@@F:default_hidden@@ by default). |
| `unmatched` | boolean · `false` | Only products the normaliser could not place (`norm_status: unmatched`): the queue the P2 catalog editor works through. |
| `sort` | @@F:catalog_sorts@@ · `hosts` | `name` orders by the product key (the slug of the product name: alphabetical except for punctuation), `publisher` by the alias-table label (else the key), `latest_version` by the natural order of the version text. Rows without a value come last in both directions; ties are broken by the product key, ascending, so the order is **total** and paging never repeats or skips a row. **`sort=latest_version` orders at most @@F:max_ranked_products@@ products at a time**: when the other filters select more, the answer is `422` ("sort=latest_version orders at most @@F:max_ranked_products@@ products at a time: narrow the catalog with q, category or publisher first"). `sort=version_count` has no cap. |
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
- **Cost and freshness.** The catalog reads a table of every product's installs, category, publisher and first-seen date, cached **per scope for @@F:table_ttl_s@@ s** in the API process (built once for concurrent callers). The whole-estate table is served for up to @@F:table_stale_min@@ minutes while a fresh one is built in the background, and every API worker also runs a **warmer** that rebuilds it every @@F:warm_interval_s@@ s while the catalog is in use (and reads the default catalog and publisher views on it before publishing it), so a request does not wait for the scan of every software row (@@F:warm_env@@ in the environment switches the warmer off). A restricted caller's table is never served stale and never warmed, because it holds which servers the caller may see. `hosts` and the version distribution are exact figures read for the products on the page (and, for `sort=hosts`, for the few more products that provably contain the page: `hosts` can never exceed `installs` or the number of visible servers). Only `sort=hosts&order=asc` reads every product, and `sort=version_count` / `sort=latest_version` read every product's (product, version) pairs once. Measured times are in section 6.5.
- Servers that have not delivered a software inventory contribute no rows. Rows with no `name_key` yet (not normalised) are not counted anywhere until the normaliser keys them. In steady state the agent ingest keys its rows inline; after an upgrade the existing rows are keyed by the background loop at @@F:keying_rows_per_tick@@ rows per @@F:normalize_interval_s@@ s tick, which is about @@F:keying_minutes_at_2m@@ minutes for the 2 million rows of the sizing target. `data_status.pending_software_rows` counts them and the pages show the "normalising" banner meanwhile; an empty catalog with pending rows is not an empty estate.

Example (`software.json`, cut to three of the @@G:software.total@@ rows: the CrowdStrike Falcon sensor (category @@F:ep_category@@), Chrome with a recorded change, and Git, whose versions come in two schemes; the fixture's `hidden_products` are the products of the hidden categories):

```json
@@EX:software@@
```

Errors: `401`, `403`, `422` (`category`, `hidden`, `sort`, `order`, `page`, `page_size`, a `q` longer than @@F:max_text@@ characters, `sort=latest_version` over a catalog of more than @@F:max_ranked_products@@ products).

### 3.9 `GET /api/v1/inventory/software/publishers`

The publisher facet of the catalog. Permission `inventory.view`. Parameters: `q`, `category` (repeatable), `hidden`, `unmatched` as in 3.8 (every catalog filter **except `publisher`**, so a count is what choosing the value would give), and `limit` (1 to @@F:publisher_limit_max@@, default @@F:publisher_limit_default@@).

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

Example (`software-publishers.json`, three of the @@G:software-publishers.distinct@@ publishers):

```json
@@EX:software-publishers@@
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
- **Unversioned rows** are reported apart: they are not a version. `unversioned.address` (always @@F:no_version@@) is the value of the `version` parameter that lists them in the installation list, so `unversioned.installs` links to `…/installations?version=__none__`; the assets vocabulary has no `product_version` sentinel for "no version", so `unversioned.hosts` is text. (`version=unknown` is no longer an alias: it selects a version spelled "unknown".)
- **Evidence** lists at most @@F:evidence_limit@@ pairs, the most widely installed first; `total` is the number of distinct pairs and `truncated` says more exist. `status` and `rule_id` are re-derived from the raw title (the Evidence tab shows which rule matched).

Example (`software-product.json`: Git has three Windows versions and two Ubuntu package versions):

```json
@@EX:software-product@@
```

Errors: `401`, `403`, `404`.

### 3.11 `GET /api/v1/inventory/software/{product_key}/installations`

Where the product is installed: one row per software **row** (a server with two titles of the product appears twice). Permission `inventory.view`; the same `404` as 3.10 for an unknown or out-of-scope product.

| Parameter | Type · default | Notes |
|---|---|---|
| `version` | string | A version, matched by equivalence key like `product_version` (`7.2` lists the `7.2.0` rows too; a version spelled `unknown` is an ordinary version). @@F:no_version@@ selects the rows that report **no** version (null or blank): it is the `unversioned.address` of the product page. The echoed `version` is the value requested. |
| `q` | string | Contains, over the server name, the package title and the version. |
| `sort` | @@F:install_sorts@@ · `asset_name` | `version` orders by the comparator (the product page's order: ascending is oldest first within a scheme group; rows with no version last in both directions) and is limited to @@F:install_sort_cap@@ rows per product (`422` past it: filter first). Names sort case-insensitively. Ties are broken by server name, server id and package title: a total order. |
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

Example (`software-installations.json`, two of the @@G:software-installations.total@@ rows):

```json
@@EX:software-installations@@
```

Errors: `401`, `403`, `404`, `422` (a bad `sort` or `order`, `page`/`page_size`, a `version` or `q` longer than @@F:max_text@@ characters, `sort=version` over more than @@F:install_sort_cap@@ rows).

### 3.12 `GET /api/v1/inventory/trends`

A daily series from `inventory_daily_snapshots`, which the snapshot loop writes once a day (doc 02 section 6) at `snapshot_hour`:@@F:snapshot_minute@@ appliance time. Permission `inventory.view`. Snapshots are kept @@F:snapshot_retention_years@@ years.

| Parameter | Type · default | Notes |
|---|---|---|
| `metric` | **required**: @@F:trend_metrics@@ | The families the loop writes. Dimensions: `assets` = `server`, `device`; `coverage` = @@F:snapshot_coverage_keys@@ (`not_applicable` counts the assets that cannot report by design and is in none of the other four, so `fresh / (fresh + aging + stale + never)` is the share of the assets that could report); `health` = @@F:snapshot_health_keys@@, **by the same definitions as the live Overview** (model, serial and vendor gaps of network devices only; the identity predicates skip the assets that cannot report by design; `missing_security_agent` is the `missing` state of servers, section 5); `software_products` = `products`, `installs` (keyed rows, hidden categories excluded); `os_release` = `family/product/release` (`unknown//` for assets nothing is known about); `changes` = `category/action` (`software/added` ... `hardware/changed`). |
| `dimension` | string | Exactly one dimension; without it every dimension of the metric (at most @@F:trend_max_series@@ series, the highest latest value first; `truncated` says more exist). |
| `days` | @@F:trend_days@@ · @@F:trend_default_days@@ | The window is the last `days` **appliance-local days**, today included (the `company` time zone, the day the snapshot loop uses). |

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
- **The first points are not an upgrade artefact.** The loop holds a day's snapshot until the process has been up for @@F:snapshot_settle_min@@ minutes and the normalise backlog is empty (never longer than @@F:snapshot_hold_max_h@@ hours past the due time), and during the first 24 hours after the capture start (@@F:capture_key@@) it refreshes today's row, at most hourly, while the estate settles. A closed day is never rewritten.
- **A restricted caller** (own `scope_tags` or role `scope_tags`) gets `200` with `restricted: true`, `series: []`, `days_with_data: 0`, `first_day: null`; the database is not read. A fleet-wide series would leak the size of assets the caller may not see (principle 3, T12).
- An unknown `dimension` is `200` with one series with no points.

Example (`trends.json`, each series cut to its last three points; the fixture has @@G:trends.days_with_data@@ days with data):

```json
@@EX:trends@@
```

Errors: `401`, `403`, `422` (`metric` missing or unknown, `days` outside @@F:trend_days@@).

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
  fresh_hours: { server: number; device: number }    // @@F:limit_fresh_hours@@; never above stale_days x 24
  stale_days: number                                  // @@F:limit_stale_days@@
  change_retention_days: number                       // @@F:limit_change_retention_days@@
  snapshot_hour: number                               // @@F:limit_snapshot_hour@@ (appliance time; the loop runs at hh:@@F:snapshot_minute@@)
  hide_categories_by_default: string[]                // category names from `categories` (default @@F:default_hidden@@); [] hides nothing
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
- **Audit**: one entry per change, `action: "inventory.settings.update"`, `resource_type: "inventory_settings"`, `resource_id: "inventory"`, `metadata: {"changed": {"<key>": {"from": ..., "to": ...}}}` with the changed keys only. **The audit entry is part of the write**: when it cannot be stored nothing is saved and the answer is `503 {"detail":"The audit log is unavailable, so nothing was done. Try again; if it persists, contact an administrator."}`. The summary and catalog caches of the serving process are dropped at once; other API workers keep theirs for up to @@F:table_ttl_s@@ s.

Example (`settings.json`, the defaults):

```json
@@EX:settings@@
```

Errors: `401`, `403`, `422` (`PUT`), `503` (`PUT`, audit entry not stored).

### 3.14 `GET /api/v1/inventory/export`

A dataset as CSV. Permission **`inventory.export`**, declared on the route (not the module default): serials and software lists are reconnaissance data, so a viewer lists everything and downloads nothing. The response streams.

| Parameter | Dataset | Notes |
|---|---|---|
| `dataset` | all, **required**: @@F:export_datasets@@ | `422` otherwise. |
| the asset filters of section 2 | `assets` | Including `sort` (the list's `sort` keys, default `name`) and `order`. |
| `q`, `category` (repeatable), `publisher`, `hidden`, `unmatched` | `software` | As 3.8; `sort` one of the catalog sorts, default `name` ascending (not the list's `hosts`: a file is easier to read A to Z). |
| `product` (**required**), `version`, `q` | `installations` | As 3.11 (`version` also takes @@F:no_version@@ for the rows with no version); `sort` as 3.11. An unknown or out-of-scope product is the same `404`. |
| `kind` | `os` | As 3.5. |
| `vendor` | `hardware` | As 3.6. |
| `category` (**one** value, else `422`), `action`, `kind`, `asset`, `subject_key`, `q`, `label`, `since`, `until` | `changes` | As 3.7, same defaults (the last seven days) and the same range check on `since` and `until` (@@F:window_min_year@@ to @@F:window_max_year@@, else `422`). No `tz`: the file has no daily series. |

- **The file is what the list returns to the same user.** Every dataset is produced by the matching list endpoint's own service function with the caller's tag scope, page after page (@@F:export_batch@@ rows a page); the export adds no query of its own except the change feed's keyset paging (same conditions, no statistics). A scoped caller's file holds the visible assets only. The tests compare the file with the list row by row for every dataset, scoped and not.
- **Format**: UTF-8 **with a byte-order mark**, **CRLF** line ends, one header row. Lists are written as `a;b;c` and booleans as `true` / `false`. **Formula injection**: a text cell starting with `=`, `+`, `-`, `@`, a tab or a carriage return gets a leading apostrophe (the rule of `dashboard/src/components/availability/exportCsv.ts` `esc()`, plus the carriage return); a cell containing a quote, comma, line break or apostrophe is quoted. Numbers are written as numbers. The JSON endpoints keep the raw text.
- **Headers**: `Content-Type: text/csv; charset=utf-8`, `Content-Disposition: attachment; filename="zenplus-inventory-<dataset>-<YYYYMMDD>.csv"` (the UTC date), `Cache-Control: no-store`, `X-Export-Rows: <rows in the file>`.
- **Row cap**: at most **@@F:export_cap@@** rows. When the filters select more the file is cut at the cap and the response carries **`X-Export-Truncated: true`** (absent otherwise); the audit entry says so too. The file holds the first @@F:export_cap@@ rows in the list's order (the newest events first, for `changes`).
- **Consistency**: the pages of one file are read in one `REPEATABLE READ` read-only transaction on a session of the export's own (pinned: nothing the dataset reads through opens another connection for it or closes it), and the stream does not depend on the request's session. For `assets`, `installations`, `os`, `hardware` and `changes` every figure comes from that one snapshot. `software` is the catalog, which is built from an in-process read model: the product list, its installs and so its order and total are fixed once when the file starts (that table is cached up to 60 s and shared with other requests of the same scope, so it can be that much older than the file), and per-product figures the catalog has already cached on it are reused; whatever is not cached yet is read inside the transaction. A software file is therefore consistent in its rows and order, but each figure is a value the catalog itself showed within the minutes around the file, not one instant.
- **Audit**: written **before the first byte is sent** (the grant of the data is the event, whether or not the client reads on): `action: "inventory.export"`, `resource_type: "inventory_export"`, `resource_id: <dataset>`, `metadata: {"dataset", "filters", "rows", "truncated"}` where `filters` holds the non-empty filters actually applied (with the effective `sort` and `order`). A refused or invalid request writes nothing. **Fail closed**: when the audit entry cannot be stored the answer is `503 {"detail":"The audit log is unavailable, so nothing was done. Try again; if it persists, contact an administrator."}` and no byte of the file is sent.
- **Admission (`429`)**: an export holds one pooled connection and a snapshot for as long as it runs, so at most **@@F:max_exports@@ exports run at once per API worker process and @@F:max_exports_per_user@@ per user**. Any other request is answered `429 {"detail": ...}` with **`Retry-After: @@F:retry_after_s@@`** before anything is planned or audited: "you already have an export running; wait for it to finish" when the caller's own export is running, "too many exports are running; try again shortly" otherwise. A client should retry after the header's seconds.
- **A client that stops reading** cannot hold the connection and the snapshot for as long as it sleeps: the producer hands the file to the response through a small bounded queue, and when the client has not read for **@@F:consumer_stall_s@@ s** the stream ends with an error (the connection is closed, the database work is rolled back and the slot is given back). A client that goes away costs nothing but the rest of the file; the audit entry already says what was granted.

Columns (the header row, in order):

| Dataset | Columns |
|---|---|
| `assets` | `@@F:columns_assets@@` |
| `software` | `@@F:columns_software@@` (`top_versions`: `version (hosts);...` as in the version bar, then `other N versions (hosts)`) |
| `installations` | `@@F:columns_installations@@` |
| `os` | `@@F:columns_os@@` (one row per version; `build_currency`: `latest`, `previous`, `older` or `unknown` (a missing version, or one the scheme cannot parse, as on the OS page), empty when the release's versions cannot all be ordered) |
| `hardware` | `@@F:columns_hardware@@` (`section`: `network_vendor`, `network_model`, `module`, `server_cpu_models`, `server_memory`, `server_disks`, `server_virtualization`) |
| `changes` | `@@F:columns_changes@@` |

A sample is `export-assets.csv` (the @@G:assets-scoped.total@@ assets of the scoped tag).

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
| `product.unversioned.installs` | page | the installation list filtered with `version=<unversioned.address>` (@@F:no_version@@) |
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
| `fresh` | within `fresh_hours.server` (@@F:default_fresh_server@@ h) | within `fresh_hours.device` (@@F:default_fresh_device@@ h) |
| `aging` | older, within `stale_days` (@@F:default_stale_days@@ d) | older, within `stale_days` |
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
| `present` | it has at least one software row of category @@F:ep_category@@, **or** a running service named @@F:ep_services_or@@ in `server_service_inventory` (built-in Microsoft Defender has no uninstall entry, so a running `WinDefend` (Defender Antivirus) or `Sense` (Defender for Endpoint sensor) service is the evidence) |
| `unknown` | it is not `present` and the question cannot be answered from what the appliance holds. The flyout's `security_agent.reason` names the first of these that applies, in this order: `no_software_list` (no complete software list was ever accepted, or the server holds unkeyed rows that the normalise loop never selects: there is no list to read), `list_truncated` (the agent cut its list at its cap, so a product beyond the cut could be the missing one), `normalising` (rows the loop will still key, or re-key after a rule-pack revision, are waiting), `no_service_list` (the software list is complete and normalised and shows no endpoint-protection title, but the server has **no service rows at all**, as with the APM-only agent profile or the services collector switched off: built-in Defender shows only as a running service, so without a service list it cannot be judged) |
| `missing` | it is not `present` and the question can be answered: baselined, list not cut, every row keyed, **and** a service list exists in which neither `WinDefend` nor `Sense` is running |

The category @@F:ep_category@@ ("Endpoint protection") holds only antivirus, endpoint-protection-platform and EDR engines and sensors (Defender for Endpoint sensor, CrowdStrike Falcon, SentinelOne, Sophos, ESET, Trend Micro, Symantec/Broadcom endpoint, McAfee/Trellix, Bitdefender, Kaspersky, Cortex XDR, Carbon Black, Malwarebytes endpoint, Cylance, Webroot, Avast/AVG business and the like). It does **not** hold the ZenPlus Agent (category `monitoring`), VPN and SSE clients, scanners, password managers, Sysmon, Wazuh and other host-intrusion tools, or management agents (ESET PROTECT, Kaspersky Network Agent, McAfee Agent, Carbon Black App Control): those stay in `security` and never count. The flyout's `security_agent.reason` says why a server is `unknown` (the four values above; `null` otherwise). The Overview's `unknown` line, the `security_agent=unknown` filter, the facet count and the daily snapshot all use the same rule, so a server without a service list is in `unknown` and never in `missing_security_agent`. A device has no state.

**Categories.** The software category vocabulary is @@F:categories@@. The categories hidden by default are @@F:default_hidden@@.

**Equivalence key of a version** (`versioning.equivalence_key`): the text trimmed of blanks, lower-cased, with trailing `.0` parts removed, but never the first character, so the key of a non-blank version is never empty (`.0` stays `.0`, `..0` is `.`). `7.2`, `7.2.0` and `7.2.0.0` are one version; `10.0` is `10`; `1.10` and `1.100` are different; `V1` and `v1` are one. Every filter and every count that names a version (`os_version`, `product_version`, `installations?version=`, the OS tree's version nodes, the catalog's `version_count`, the summary's `versions`, the hardware `firmware_versions`) compares and groups on this key, which is why a number on a row equals the total of the list it links to. A blank version (null or only whitespace) is *no version*.

**Version ordering** uses the scheme of the OS family (`services/versioning.py`): Windows builds numerically, FortiOS / PAN-OS (with hotfix ordering) / Junos / classic IOS and IOS XE (train-aware: a pair from different trains is *not comparable*) and semver-ish for software. "Not comparable" yields `label: null` and `currency: null`, never a guess.

**Hidden software categories** (@@F:default_hidden@@ by default) are excluded from the Overview's software figures, from the flyout's top list and from the catalog (unless `hidden=include` or the category is asked for by name); they are still stored, and `server.software.hidden` says how many a server has.

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
| `software.json` | `GET /software?page_size=50` (the whole default catalog: @@G:software.total@@ products) |
| `software-publishers.json` | `GET /software/publishers` |
| `software-product.json` | `GET /software/git` (five versions in two schemes: Windows and Ubuntu packages) |
| `software-installations.json` | `GET /software/git/installations?page_size=50` |
| `trends.json` | `GET /trends?metric=assets` (@@G:trends.days_with_data@@ days of snapshots over a span of 14 days, one day missing) |
| `settings.json` | `GET /settings` |
| `export-assets.csv` | `GET /export?dataset=assets&tag=team-edge` with `inventory.export` (UTF-8 with a byte-order mark, CRLF; listed under `samples` in `index.json`, not under `fixtures`, because it is not JSON) |
| `summary-scoped.json`, `assets-scoped.json`, `changes-scoped.json`, `software-scoped.json`, `software-product-scoped.json`, `software-installations-scoped.json`, `trends-scoped.json` | the same requests as a user scoped to the tag `team-edge` |
| `index.json` | the manifest: for each file the request, parameters and caller; the pinned clock; the asset counts |

The fleet: @@G:index.assets.total@@ assets (@@G:summary.assets.server@@ servers, @@G:summary.assets.device@@ devices; @@G:index.assets.snmp_twins_not_counted@@ more device rows are the SNMP twins of servers and are not counted), `now = @@G:index.pinned_now@@`, change capture started 45 days earlier. Servers: Windows Server 2016 / 2019 / 2022, Windows 10 / 11, Ubuntu and RHEL, four servers with no OS, four registered but never reported, two registered in a collection mode that cannot upload an inventory (`not_applicable`), two with truncated software lists (`security_agent=unknown`), and endpoint protection on most Windows servers but not all: a CrowdStrike Falcon sensor on most, two servers protected only by a running Defender service, three that run only the ZenPlus Agent (which does not count as protection) and one whose Defender for Endpoint `Sense` service is stopped. Devices: FortiGate (two managed through a controller: `not_applicable`), Palo Alto, Cisco IOS XE / IOS / NX-OS, Aruba AOS-CX, Juniper, F5 and Ubiquiti gear, eight devices that tell us nothing (four of them ping-only: `not_applicable`) and some with gaps (no model, no serial, never polled, stale). The scoped user sees @@G:summary-scoped.assets.total@@ assets and @@G:software-scoped.total@@ software products (@@G:software-scoped.hidden_products@@ more in the hidden categories).

---

## 6.5 Measured performance

@@PERF@@

@@PERF_SW@@

---

## 7. What later waves add

The router, the service modules and the schemas are structured to be extended without touching the endpoints above: the asset union and the filters are in `inventory_query.py` (`asset_union_sql`, `_conditions`), the software catalog is in `inventory_software_query.py`, the handlers are thin, and a new list endpoint reuses `AssetFilters` through the `asset_filters` dependency.

| Endpoint (later phase) | Notes |
|---|---|
| `PATCH /inventory/catalog/products/{product_key}`, rules, `GET/POST/DELETE /inventory/views` (P2) | Catalog rules, product attributes and saved views need `inventory.manage` and a forward migration (the number is taken when P2 starts; 155 is intentionally unused, see `00-INDEX.md`). The catalog's `norm_status` then comes from the stored product row instead of the derivation of section 5, and a rollup table refreshed by the normalise loop (doc 02 section 13) replaces the per-scope table cache if the catalog budget requires it. |
| `GET /inventory/lifecycle` (P4) | EOL stages; `summary.lifecycle` and the lifecycle-aware N / N-1 of the OS and Software pages. |
| `product_version=__none__` | The assets vocabulary has no sentinel for "no version" (the installation list has one, `unversioned.address`); the software pages show `unversioned.hosts` as text. A sentinel would make the number a link. |

`test_inventory_route_guards.py` asserts that every non-GET route declares more than `inventory.view` (today only `PUT /settings`, which declares `inventory.manage`) and that any `…/export` route declares `inventory.export`.
