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

A catalog with a very large number of products is the case the sorts that need a figure for every product have to survive. On a **wide catalog of 24,000 products and 519,000 (product, version) pairs** the review round measured `sort=version_count` at 941 ms (3,848 ms before), `sort=latest_version` at 3,333 ms (3,938 ms before) and `sort=hosts&order=asc` at 1,104 ms (2,946 ms before). More important than the time is what it no longer does to the other requests: the event loop is held for 75 ms (was 2,940 ms) by `version_count` and 157 ms (was 3,145 ms) by `latest_version`, because the ranking runs in the worker pool, so a slow sort no longer stalls every other request of the API process. `sort=latest_version` is the one sort whose cost grows with the catalog, and it refuses a catalog of more than @@F:max_ranked_products@@ selected products (`422`, section 3.8). The warmer brings the first unrestricted catalog request after a restart from 460 ms to 15 ms and the publisher facet from 545 ms to 3 ms (one table build of about 0.8 s per worker every 40 s while the catalog is in use). The export of the software catalog measures 1.3 s against 0.2 s in the baseline run, which reused figures that earlier requests had left in the shared table; the export now reads its own and leaves nothing behind. It is far inside its limit (10,000 rows in under 5 s).

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

