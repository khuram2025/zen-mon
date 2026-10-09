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
