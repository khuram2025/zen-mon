# Inventory knowledge base: article sources

Sources of the customer-facing articles for the **Inventory** module (the Inventory & Compliance group in the
sidebar), in the same shape as `udt-kb/`: Django templates for the zentryc.com website plus the screenshots they show.

**Nothing here is published.** These files are sources under review. Publishing to zentryc.com is a separate, owner-approved
step that follows section 8 of `Documentation/26-GITHUB-MULTI-MACHINE-AND-PUBLISHING-RUNBOOK.md` (backup first, upload to a
staging directory, `manage.py check` and `manage.py test core`, `collectstatic`, restart, external smoke checks, record
the backup path). Do not copy these files onto the web host from this checkout without that procedure.

The website source (`core/urls.py`, `core/views.py`, `core/sitemaps.py`, `core/tests.py`, `templates/llms.txt`,
`core/templates/core/_kb_article_style.html`) is **not** in this repository, so the route and view entries below are listed
as a contract, not provided as code.

## Layout

```
inventory-kb/
  README.md               this file
  templates/              one Django template per article (extends core/base.html)
  web/                    screenshots, WebP, copied to static/img/zenplus/inventory/ on the website
```

No new CSS is needed. The templates use only classes that the UDT articles already use (`kb-shell`, `kb-body`, `kb-table`,
`kb-deflist`, `kb-shot`, `callout tip|warn`, `step-list`, `pill-row`, `kb-foot`, ...), so `_kb_article_style.html` and its
UDT additions are enough.

## Articles

The key is the key of `KB_ARTICLES` in `dashboard/src/components/inventory/InventoryKbLink.tsx`; the path is appended to
`https://zentryc.com/kb/zenplus/inventory`. The first seven are the articles the (i) icon of each screen opens.

| Key | Path | Template | URL name | Opened from |
|---|---|---|---|---|
| `overview` | `/` | `kb_zenplus_inventory.html` | `core:kb_zenplus_inventory` | Overview, and any unknown path |
| `assets` | `/assets/` | `kb_zenplus_inventory_assets.html` | `core:kb_zenplus_inventory_assets` | Assets |
| `operating-systems` | `/operating-systems/` | `kb_zenplus_inventory_operating_systems.html` | `core:kb_zenplus_inventory_operating_systems` | Operating Systems |
| `software` | `/software/` | `kb_zenplus_inventory_software.html` | `core:kb_zenplus_inventory_software` | Software and the product page |
| `hardware` | `/hardware/` | `kb_zenplus_inventory_hardware.html` | `core:kb_zenplus_inventory_hardware` | Hardware |
| `changes` | `/changes/` | `kb_zenplus_inventory_changes.html` | `core:kb_zenplus_inventory_changes` | Changes |
| `settings` | `/settings/` | `kb_zenplus_inventory_settings.html` | `core:kb_zenplus_inventory_settings` | Inventory Settings |
| `getting-started` | `/getting-started/` | `kb_zenplus_inventory_getting_started.html` | `core:kb_zenplus_inventory_getting_started` | linked from the overview article |
| `health-and-freshness` | `/health-and-freshness/` | `kb_zenplus_inventory_health_and_freshness.html` | `core:kb_zenplus_inventory_health_and_freshness` | linked from the overview article |
| `permissions` | `/permissions/` | `kb_zenplus_inventory_permissions.html` | `core:kb_zenplus_inventory_permissions` | linked from the overview article |
| `export` | `/export/` | `kb_zenplus_inventory_export.html` | `core:kb_zenplus_inventory_export` | linked from the overview article |
| `troubleshooting` | `/troubleshooting/` | `kb_zenplus_inventory_troubleshooting.html` | `core:kb_zenplus_inventory_troubleshooting` | linked from the overview article |

Cross-links inside the articles use `{% url 'core:<URL name>' %}` (plus `core:kb`, `core:kb_zenplus` and `core:contact`,
which the UDT articles already use), so the URL names above must exist before any article renders. Anchors used from other
articles (`#history-start`, `#capped`, `#budget`, `#pairs`, `#flyout`, `#scope`, `#custom-roles`, `#health-panel`,
`#security-software`, `#unknown-device`, ...) are `id` attributes of sections in the target article.

## Website changes a publication needs (runbook section 8.2)

1. `core/urls.py`: twelve routes under `kb/zenplus/inventory/` with the URL names above, following the existing
   `kb_zenplus_udt_*` entries.
2. `core/views.py`: the view metadata, following the UDT entries.
3. `core/tests.py`: a route/page test per article.
4. `core/sitemaps.py`: the twelve URLs.
5. The ZenPlus KB index (a link to the Inventory overview) and `templates/llms.txt`.
6. The images under `static/img/zenplus/inventory/` (below), then `collectstatic`.

## Screenshots (`web/`)

Taken from the mock harness (generated example fleet; the Hardware page from the golden fleet of `dashboard/src/pages/inventory/fixtures/`), so they contain invented host names, addresses and numbers
and no customer data. They are WebP, 1600 px wide, named `inventory-<screen>.webp`; every `<img>` has meaningful alt text and
every caption says the data is example data. Retake them when a screen changes visibly.

| File | Shows | Used by |
|---|---|---|
| `inventory-overview.webp` | The Overview (1600 x 1000) | overview (figure); social image of overview, getting started, permissions, health and freshness, troubleshooting |
| `inventory-health-panel.webp` | The Inventory health panel, cropped | health and freshness (figure) |
| `inventory-assets-list.webp` | The Assets list with the filter pane | assets (figure); social image of assets and export |
| `inventory-asset-flyout.webp` | The asset flyout of a firewall | assets (figure) |
| `inventory-os-tree.webp` | Operating Systems | operating systems (figure and social image) |
| `inventory-software-catalog.webp` | The software catalog | software (figure and social image) |
| `inventory-product-versions.webp` | A product page, Versions tab, cropped | software (figure) |
| `inventory-hardware.webp` | The whole Hardware page (1600 x 2187), from the backend's golden example fleet so that module groups are populated | hardware (figure and social image) |
| `inventory-changes.webp` | Changes | changes (figure and social image) |
| `inventory-settings.webp` | Inventory Settings | settings (figure and social image) |

## Before publishing

1. **Release number.** The articles say *Applies to ZenPlus 1.24.0+*, the number proposed for the release that ships Inventory
   (`Documentation/Inventory/04-ROADMAP-PHASES-AND-TASKS.md`, release plan). Replace it everywhere if the release is numbered
   differently (`grep -rl "1.24.0" inventory-kb/templates`), and set `datePublished` / `dateModified` and the *Updated* pill to
   the publication date.
2. **Publish with the release, not before and not long after.** The (i) icon in every Inventory screen links to these
   addresses, so until the articles are live it leads to a not-found page. Publish on the day the release reaches customers.
3. **Re-read the statements that depend on shipped data**, each marked in the source or listed here:
   - `kb_zenplus_inventory_health_and_freshness.html`, the *endpoint-protection* section and the FAQ answer about it: they
     describe the category `endpoint-protection` (antivirus, EPP and EDR engines) plus the running Defender services
     `WinDefend` and `Sense`. Check the product list against `server/app/data/inventory/software_rules.json` (category
     `endpoint-protection`) and the service list against `ENDPOINT_PROTECTION_SERVICES` in
     `server/app/services/inventory_query.py` at release time.
   - The statements about what is *not collected* (server manufacturer, model, serial and BIOS; Linux software; Windows
     updates) in the overview, getting started and hardware articles. They state the limit of this release and promise
     nothing; if a later release ships the data, reword each one.
   - The inventory interval of the agent: the articles say it is `collectors.inventory.interval_seconds` in the agent's own
     `agent.yaml` (default 21600 s) and not a field of Agent Policies. Re-check if the controller ever gains a policy field
     for it.
   - The first-hour figures (about 100,000 software rows every two minutes, first step after 45 seconds): they come from
     `NORMALIZE_START_DELAY_S`, `NORMALIZE_INTERVAL_S`, `SOFTWARE_BATCH` and `MAX_SOFTWARE_BATCHES_PER_TICK` in
     `server/app/services/inventory_service.py`.
4. **Retake all ten screenshots.** They were taken from the mock harness before the Phase 1 review fix rounds, and every one of
   them shows something that has changed since. The collapsed sidebar label under the Inventory icon read *ASSETS* and now
   reads *Inventory* (eight files show it; the overview shows the expanded sidebar and the health-panel is a crop), and the line
   under the tabs now carries the *not reported by design* part and the honest percentage (nine files show it). Beyond that,
   per file:

   | File | What differs from the screen now |
   |---|---|
   | `inventory-overview.webp` | The Software card line in the shot uses the old wording (*EDR and AV on 120 of 127 servers, 7 without*); it now reads *Endpoint protection on N of M servers with a complete software list*, with an *unknown* line. Runtimes (the Visual C++ redistributables, Java) are hidden by default, so *Top software* and the sprawl panel change. The health panel has a ninth information line |
   | `inventory-health-panel.webp` | *7 servers without a security agent* now reads *servers without endpoint protection*; a ninth line counts the assets not reported by design |
   | `inventory-assets-list.webp` | Sidebar label and line under the tabs; the Freshness facet, below the part the shot shows, has a *Not reported by design* value |
   | `inventory-asset-flyout.webp` | Sidebar label and line under the tabs |
   | `inventory-os-tree.webp` | The donut folds every family beyond the five largest into one grey *Other (N families)* slice; the *Build currency* panel and the line under the tabs |
   | `inventory-software-catalog.webp` | The hide switch reads from the configured categories and runtimes are hidden by default (the shot lists Visual C++ and Java and the footnote names Driver, OS component and Update only); *CrowdStrike Falcon Sensor* and *Microsoft Defender for Endpoint* are in the category *Endpoint protection*, not *Security* |
   | `inventory-product-versions.webp` | The breadcrumb names the product (the shot reads *Detail*); a first-seen date at or before the capture start reads *First seen before <capture date>*; sidebar label and line under the tabs |
   | `inventory-hardware.webp` | The *not reported by design* notes of the page, the sidebar label and the line under the tabs |
   | `inventory-changes.webp` | Sidebar label and line under the tabs; the footnote that now prints when capture began (*History starts ...*) lies below the part the shot shows, so keep it in view when retaking |
   | `inventory-settings.webp` | The Endpoint protection checkbox is missing, Runtime is unticked and the footnote reads *Hidden by default: Update, Driver, OS component*: the article says Update, Driver, OS component and Runtime are ticked and lists Endpoint protection. Read-only users see the numbers on a muted fill with a note |

   Take the shots from the golden fleet of `dashboard/src/pages/inventory/fixtures/` or the mock harness after it has been
   regenerated, never from a customer or live appliance, and keep the example-data captions.
5. **Run the checks** from the runbook (`manage.py check`, `manage.py test core`, link and static checks) and test every
   article on desktop and mobile width, with the images at full size.
6. **Record** the deployed commit and the backup path (runbook section 10, *KB publication*).

## Style

Plain language for operators and administrators; no emojis; accurate to the shipped screens (names of buttons, tabs and
messages are quoted from the product); numbers that are defaults say so and name the setting that changes them; limits that
are not a bug (the 1,000-title software cap, the change budget of about 2,000 per server and 24 hours, the 200,000-row
export cap) are stated with their reason. Do not promise future features: say what this release does, and keep the roadmap in
`Documentation/Inventory/04-ROADMAP-PHASES-AND-TASKS.md`.
