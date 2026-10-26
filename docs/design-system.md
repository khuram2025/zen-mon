# Dashboard design system

The ZenPlus UI is used in two ways: engineers at a desk (laptop or desktop monitor, day-to-day work) and NOC wall
screens read from across a room. One set of tokens serves both; the **display density** scales everything together.

## Typography

- **Fonts ship with the dashboard.** Inter Variable (UI) and JetBrains Mono Variable (IPs, IDs, code) come from
  `@fontsource-variable/*`, imported in `src/main.tsx`; `src/styles/fonts.css` maps the plain `Inter` name used by
  saved map annotations onto the same files. Many appliances have no internet access: never load a font, stylesheet,
  script or data file from a CDN. `scripts/test-offline-assets.mjs` (CI) enforces this.
- **One type scale, in rem** (`tailwind.config.js`). At the default 15px root:

  | Class | Size | Use |
  |---|---|---|
  | `text-2xs` | 11.25px | the floor: uppercase labels, table headers, axis ticks |
  | `text-xs` | 12.2px | secondary lines, table cells, captions |
  | `text-sm` | 13.1px | body text, panel titles, controls |
  | `text-base` | 14.1px | page body, section titles |
  | `text-lg` – `text-xl` | 15.9 – 18.8px | sub-headings |
  | `text-2xl` | 22.5px | page titles |
  | `text-3xl` – `text-4xl` | 28 – 34px | stat-tile and hero figures |

  Do not write pixel font sizes (`text-[11px]`); they do not follow the density. Sub-11px text is reserved for
  miniature previews (report thumbnails) and map canvases, which scale themselves.
- Figures that line up use tabular numbers (`table`, `.font-mono`, `.tabular` do this by default).

## Display density

`src/stores/display.ts`, set in the header's Display menu or Settings → Appearance (per browser):

| Density | Root | For |
|---|---|---|
| Compact | 14px | laptops, more rows on screen |
| Comfortable | 15px | default, desktop monitors |
| Large | 17px | 1440p and 4K monitors |
| NOC wall | 20px | video walls read from a distance |

**NOC wall mode** (kiosk) hides the sidebar and header and requests full screen. A wall browser can be set up by URL
alone: `https://<appliance>/?density=wall&kiosk=1` (not saved, so it never changes an engineer's own browser). Sizes
that must scale with the density are written in rem (the sidebar widths too). Breakpoints stay in viewport pixels, so
`globals.css` reflows the 6- and 7-column stat strips to 3–4 columns at Wall (and Large under 2200px).

## Colour

Tokens live in `src/styles/globals.css` (`:root` light, `.dark` dark — the NOC default). Dark panels separate by
surface step and a 1px border, not shadows. Status colours (`success`, `warning`, `danger`, `info`) mean state only;
text on status tints uses the `*-fg` tokens, which keep 4.5:1 (tests in `test-netflow-foundation.mjs` and
`test-netflow-detection-health-ux-fixes.mjs` measure the real tokens).

## Charts

`src/lib/chartTheme.ts`: Inter in charts, `chartFont(px)` for axis and legend text (never below 11.25px, grows with
the density), the categorical series order `--series-1` … `--series-8` (validated for colour-vision deficiency on
both chart surfaces; colour follows the entity, never its rank), a hairline grid, and the `zenplus-light` /
`zenplus-dark` ECharts themes (`theme={chartTheme()}`). Canvas charts read colours with `tokenColor()`; SVG and HTML
marks can use `seriesVar(i)` or the `series-*` Tailwind colours. Report and PDF renderers keep their own fixed sizes.

## Stat tiles

Every page's KPI tiles share `src/components/ui/kpiTone.ts`: a 3px top rule and a tinted icon chip in the status
colour when the figure has a state (quiet primary otherwise), an uppercase `KPI_LABEL` and a large `KPI_VALUE`.
