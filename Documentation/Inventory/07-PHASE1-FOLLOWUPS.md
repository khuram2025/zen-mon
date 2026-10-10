# 07 — Phase 1 follow-ups: findings that are still open

*Status: 2026-10-09, written when the owner paused the roadmap after Phase 1. This is the list of every finding of the Phase 1 adversarial review and of its re-check that was **not** fixed, plus the rough edges the fix rounds recorded. It exists because the review tooling lives outside the repository (`04-ROADMAP-PHASES-AND-TASKS.md`, section "Status board and how to resume") and would be lost when that directory is cleaned. Nothing here blocks the release that carries Phase 0 and Phase 1 (planned; migrations 157, 158 and 159). Read it before starting Phase 2.*

**How to read the tables.** The ID is the finding's ID in the review or the re-check (their texts, with the reproduction steps of the verifiers, are in the author's work directory; the summary here is what you need to decide). *Severity* is the rating of the skeptic or verifier where one rated it, otherwise the author's; on the review's scale a cross-scope read is critical, so *medium* here means a real defect with a bounded effect. *Why not in this release* is the reason the finding stayed open. *Phase* is the suggested home; "separate task" means a task already filed outside the module's phases.

The re-check of the review's 110 findings verified 98 as fixed, 7 as partly fixed, 4 as deliberately deferred (BN-02, UI-16, UP-05, UP-10) and 1 as not a defect (UI-05, a flaw of the review's mock). Round 2 then completed six of the seven partly fixed ones (BN-08, UI-08, UI-15, UI-19, UI-22, UP-12) and fixed what the re-check and the review of the new code found and belonged to this release. The items below are what remained: the seventh partly fixed finding (UP-06), the four deferred ones, and the new findings that round 2 did not take.

---

## 1. Security scope outside the module

Both are older than the module, are not changed by this release, and are stated in the release notes (`06-RELEASE-NOTES-DRAFT.md`, section 8). They are filed as separate tasks.

| ID | Title | Area | Severity | Why not in this release | Phase |
|---|---|---|---|---|---|
| NF-EP-01 | `GET /server-baselines/{id}/results` and `GET /agent-fleet` ignored the user's tag scope: a tag-limited user with `servers.view` got the ids, names, hostnames and (for the fleet) agent addresses of servers outside their scope, and the sibling `GET /servers/{id}/…` sub-routes (network, filesystems, services, compliance, agent, commands, captures) answered for those ids | Servers, baselines, agent fleet | **Medium** (skeptic confirmed) | **Fixed for reads, outside the module** (`server/tests/test_server_scope_routes.py`): the sub-routes, the fleet list, counts, search and detail, and the baseline results apply the tag scope and answer `404` outside it. Still open: the fleet-wide counts on the baseline list and detail, the capture detail routes (`/servers/network-captures/{id}/…`) and the scope on `servers.manage` actions | Separate task, for what remains |
| NF-FE-5 | The Services daily bars and the Services reports send the browser's time zone name and answer `422 Invalid reporting timezone` when the host cannot open it (`Asia/Calcutta`, `Europe/Kiev`, `Asia/Saigon`, `America/Buenos_Aires`, `Asia/Katmandu`, `Asia/Rangoon`) because the host lacks `tzdata-legacy` | Services pages, reports | **Medium** (skeptic confirmed; reproduced on `.221` only) | Pre-existing (identical to `main`) and outside the module. The shared fix is to move the module's zone resolver and its alias map into a common module, or to add the `tzdata` package | Separate task (filed) |
| (tzdata) | `tzdata` is not in `server/requirements.txt`; inside the module a legacy browser zone is mapped and any other unknown zone falls back to UTC with a visible notice | Time zones | Low | Deliberately not done: the fallback is honest and labelled. Closing it together with NF-FE-5 removes both | Separate task (with NF-FE-5) |

## 2. Accessibility

| ID | Title | Area | Severity | Why not in this release | Phase |
|---|---|---|---|---|---|
| UI-16 | Forced-colors (Windows High Contrast) mode: data bars disappear, the selected product tab is indistinguishable and links and tiles are drawn inside a heavy permanent box | Version, firmware and mini bars, tabs, links | Low (the docs name two of the three symptoms) | Deliberately not done. Needs `forced-color-adjust: none` with explicit borders on the bar segments, a tab indicator that survives forced colours and a focus outline only on `:focus-visible`, which touches shared components | P2 (with the shared component work) |

## 3. Normaliser breadth

All are rule-pack or normaliser changes of low severity: a wrong or coarse value, never a wrong count behind a link. Change them together and bump `RULEPACK_REV` once (the re-render is silent, no change events).

| ID | Title | Area | Severity | Why not in this release | Phase |
|---|---|---|---|---|---|
| NL-04 | The HP vendor refinement is a closed list of HP Inc. words: old Z-series workstations and other HP PCs stay *HPE*, while a hostname such as *envy-db01* or the word *Compaq* can flip an HPE server to *HP Inc.* | Vendor aliases | Low | Breadth, not a regression; a positive-evidence rule (ProLiant, Integrity, enterprise OIDs) needs a corpus review | P2 (catalog and rule packs) |
| NL-06 | A MikroTik model read from `sysDescr` is cut at the first space or caret, so *hAP ac^2*, *hAP ac lite* and *hAP lite* all become *hAP* and *hEX S* becomes *hEX* | OS patterns | Low | The version of RouterOS is not collected either (the poller does not read `mtxrLicVersion`), so MikroTik coverage is already stated as limited | P2, or P3 with the poller work |
| NL-07 | Fallback OS names keep an integer or letter-prefixed version in the product (*Solaris 10*, *Solaris 11*, *AIX 7*, *HP-UX B.11.31*, *Android 13* are separate products), and a less specific `os_version` overrides the version in the name | Normaliser | Low | Only dotted versions are split today; the fix changes the identity of products already stored | P2 |
| NL-08 | Azure Stack HCI and Azure Local: for builds that are also Windows Server builds the release comes from the server build table (*21H2* reads *2022*, *24H2* reads *2025*), for others from the OS itself (*22H2*, *23H2*), so one product shows four release styles | Normaliser | Low | Narrow platform; the choice (OS-reported release over the build table for platform products) needs an owner call | P2 |
| NQ-03 | Hardware page, *Unknown vendor* row: devices that report a model but no vendor form the vendor-less row, which counts their models but has no model rows to open, so those models appear nowhere and the vendor rows' `models_known` add up to more than `network.models_known`. The drills of the row are exact | Hardware | Low | A display and counting choice (emit model rows for the vendor-less group, or count none and reword); no count behind a link is wrong | P2 |

## 4. Export edge cases

| ID | Title | Area | Severity | Why not in this release | Phase |
|---|---|---|---|---|---|
| NF-1 | A client that disconnects while the response is inside a slow `send()` keeps the export slot, its connection and its snapshot until the 120 s stall timer ends (the generator's `finally` waits for garbage collection); the same person then gets `429` for a download they cancelled | Export | Low (reproduced under a test server; not measured under uvicorn) | The effect lasts two minutes and the snapshot cannot be corrupted; a fix subclasses the streaming response so that cancelling ends the producer | P2 (with export and report work) |
| NF-2 | No overall time cap on an export: a reader that takes one chunk every ~100 s keeps its slot, connection and snapshot for hours, and two such sessions can lock every other user out of exports (`429`) | Export | Low (code reading; not run) | Needs `inventory.export`, a sensitive permission, so the actor is a trusted user; add a wall-clock deadline of 10 to 15 minutes | P2 |
| UP-06 | The CSV exports carry `first_seen` (software) and `first_seen_at` (installations) as stored, with no marker; for software that existed at the upgrade the value is the reported install date or the upgrade time, not the day Inventory saw it | Export, software | Low after this round (partly fixed: the screens print *before <date>*) | The code is unchanged on purpose: a marker column would change the pinned column lists. The release notes (section 3) and the export article (*First-seen dates in the files*) now say what the value means | P2 (add a column or an `observed` flag when the export is next changed) |
| (NB-03) | The software dataset is built from the catalog's cached read model, so its figures are not one instant (its rows and order are consistent) | Export | Low | Documented: `06` section 4, the export article and the module docstring state exactly this. A single snapshot would need the catalog read model to run inside the export transaction | P2, if auditors need one instant |

## 5. UX polish and data semantics

| ID | Title | Area | Severity | Why not in this release | Phase |
|---|---|---|---|---|---|
| UP-05 | A server and its same-address ping or SNMP device are two assets unless linked (a discovery import or the API sets the link; enrolling an agent never does and the Servers page has no control for it), so one machine can be counted twice | Assets union | Medium (verifier confirmed; the documents state the precondition) | Deliberately not done: automatic pairing by address can hide a real asset on a wrong guess. Needs a linking control or an explicit pairing rule | P3 (coverage expansion), earlier if the Servers form gains a *Linked device* field |
| UP-10 | Servers that stopped reporting still count in every software figure (hosts, installs, versions, sprawl, endpoint-protection coverage) until they are deleted or report again | Software figures | Low (verifier; the finding's "no qualifier" claim was false because the line under the tabs counts the stale and never-reported assets) | Deliberately not done: whether to ignore servers silent for more than N days is a product choice. Documented in `06` section 8 | P2 (a staleness setting with a *N servers not counted* footnote in the settings work) |
| BN-02 | Above 400,000 visible software rows the Overview's version-sprawl panel ranks only the most installed products (64 to 2,000 of them) and says *N most installed of M products*; below that it is exact | Overview | Medium by the verifiers' reading of the original finding; labelled since the fix | Accepted by the orchestrator: an exact ranking costs 60 to 120 ms at the design size and the panel is honest. The customer overview article now says so | P2 (when the catalog rollup table exists) |
| (catalog) | The software catalog is slightly above its 400 ms budget when process-cold and for a restricted scope (about 460 to 480 ms; warm requests take 15 to 20 ms), and deep pages, `page_size=500` and the per-product sorts also exceed it | Software catalog | Low | A background warmer keeps the whole-estate table warm; a scoped caller's table is built on demand. A rollup table refreshed by the normalise loop needs a migration | P2 (with the catalog tables) |
| DTN-01 | A linked server's vendor, model and serial come from its SNMP twin, but the age shown beside them is the agent's last inventory, not the device's last poll; after a dead agent they look as stale as the agent | Flyout | Low (skeptic; the author said medium). No count, filter, freshness bucket or export is affected | Display only; the fix uses the twin's `identity_updated_at` when the hardware source is the device | P2 (small), or P3 when the agent starts reporting hardware |
| UP-R2-04 | Controller-managed child devices (ping and SNMP disabled) are labelled with the source `icmp`, appear under `source=icmp` and read *not reported by design* in the same row | Assets list, flyout | Low | Label only; nothing pings the device. The fix returns only the controller source for a managed child | P2 |
| NF-FE-6 | A site, vendor, model or version with a tab or a non-ASCII space (for example a no-break space) at its end is counted, but its link opens nothing: the browser trims differently from the backend. The UI now shows the number as text with the reason instead of a wrong list | Facet and table links | Low | The count stays right; the backend should compare with the same trim as the address parser, or normalise on write | P2 |
| NB-05 | The same defect on the Devices page: a vendor, model or OS version ending in a tab, newline or no-break space (SNMP strings often end in CR/LF) is a facet item that cannot be passed back as a filter | Devices facets | Low | Same root cause as NF-FE-6; the page shows such an item as text | P2 (with NF-FE-6) |

## 6. Documentation

| ID | Title | Area | Severity | State |
|---|---|---|---|---|
| (DTN-03 to DTN-09, UP-R2-03, NB-06, NF-4, NF-EP-04, NF-EP-05, NL-02, BN-02 nit) | The documentation findings of the re-check: Settings visibility in the overview article, the ZenPlus Agent category, the hidden categories in the software FAQ, the screenshots to retake, test counts, the Devices CSV and `GET /devices` additions, small wording drift, export limits per API process, the research document's scope gap, the short sidebar label, the Devices column defaults, why Devices vendor counts differ, MikroTik after the upgrade, the sprawl caveat | Documents and knowledge-base sources | Low | **Fixed in this round** in the documents and the knowledge-base sources |
| (comment) | The header comment of `dashboard/src/components/devices/deviceInventory.ts` still says the OS version column is on by default for a user with no saved choice; the page's defaults have it off | Source comment | Low | **Open**: not a document. Edit the comment next time the file is touched |
| (screenshots) | All ten knowledge-base screenshots predate the fix rounds | Knowledge base | Low | **Open and required before publishing**: retake them (`inventory-kb/README.md` lists what differs per file) |

## 7. Test pinning and acceptance gaps

Not findings. They are the checks that were never run, from the *Verification* paragraph and the open-items table of `04-ROADMAP-PHASES-AND-TASKS.md`.

| Item | What is missing | Why | Phase |
|---|---|---|---|
| End-to-end access test | `integration/test_access_control_e2e.py` needs a disposable API; its team-role assertions have never been executed | No disposable API in the build environment | Reference-appliance acceptance (release step) |
| Live viewer check (T14) | A viewer role refused on every mutating Inventory route on a live appliance | Needs the reference appliance | Release step |
| Radix menus against a real backend | The Devices facets and the Inventory selects were checked only in the mock harness (and a headless browser against it), not behind the real proxy and API | Needs the reference appliance | Release step |
| Sidebar label assertion | A grep-style assertion that the documents quote the label of `navigation.ts` would have caught NF-EP-04 | A documentation-drift check, not a product test | P2, if the documentation check script is brought into the repository |

## 8. Rough edges recorded in the round 2 hand-off

Not review findings; the agents of round 2 wrote them down as risks or deviations. Each is real and small.

- The software export is not one instant (NB-03 above), and the mixed-category product (one product key with rows in two categories) is counted per row on the Overview and per product by its highest category in the catalog; it occurs only transiently after a rule-pack change.
- A stale-revision software row that the normalise loop never selects (not a Windows server, no source, revision below the current one) is neither pending nor stuck; under such a row a server could read *without* instead of *unknown* for endpoint protection. Only the first build of the loop, which never shipped, could write one.
- The full-estate benchmark harnesses were not repeated in round 2 (they are not in the repository). Measured only in the window right after an upgrade, while unkeyed software rows exist: the backlog count of `data_status` takes about 96 ms against 50 to 65 ms at 2 million unkeyed rows, and the endpoint-protection joins about 99 ms against 74 ms. In the steady state the partial index is empty and nothing changed, but the endpoint-protection joins are not measured at fleet scale there.
- The Devices page and its pager format numbers with the browser's `toLocaleString`, the Inventory pages with a fixed en-US formatter, so digits can mix in locales that substitute digits.
- The shared disabled style of the read-only settings form (switch and category checkboxes) still dims to 50 percent; the Devices page tag and tab-pill contrast and the shared sidebar contrast were not changed.
- The *unknown* tooltip says that Linux servers report no software today; revisit it when Phase 3 adds Linux packages. The banner's *figures refresh when processing finishes* relies on the layout staying mounted and the summary polling every 15 s.
- Not verified against a real backend or the production build behind the real proxy: the chunk-load recovery, the search-box race fixes and the tab-switch feedback ran on the Vite dev server against the mock harness.

## Closed after the re-check

NB-08 (two wrong sentences: the settings 422 for a body that is not an object, and the module docstring of the software query about the no-version address) was fixed in the contract template and the docstring before the pull request, so it is not an open item.
