import * as echarts from 'echarts'

/**
 * One chart look for every page: Inter in charts, readable axis text that follows the display density, a hairline
 * grid and the categorical series order from styles/globals.css (--series-1 … --series-8). Series colours follow the
 * entity, never its rank; status colours (success/warning/danger) are reserved for state and are never a series.
 */

/** Root font size in px (15 by default; 14/17/20 for the compact, large and wall densities). */
export function rootFontPx(): number {
  if (typeof document === 'undefined') return 15
  const px = parseFloat(getComputedStyle(document.documentElement).fontSize)
  return Number.isFinite(px) && px > 0 ? px : 15
}

/**
 * Chart text size in px for a size written against the old 14px root. Axis ticks and legends never go below the
 * 2xs step (11.25px at the default density) and grow with the density, so a wall screen reads its axes.
 */
export function chartFont(px: number): number {
  const scale = rootFontPx() / 15
  const base = px <= 10 ? 11.25 : px <= 11 ? 12 : px <= 12 ? 12.75 : px * (15 / 14)
  return Math.round(base * scale * 2) / 2
}

/**
 * A theme token as a colour string, read from the live stylesheet (ECharts draws on canvas and cannot use var()).
 * `#rrggbb` when opaque, so callers that derive ids or append alpha suffixes get a plain hex value;
 * `rgba(…)` when an alpha is asked for.
 */
export function tokenColor(name: string, alpha = 1): string {
  if (typeof document === 'undefined') return 'transparent'
  const raw = getComputedStyle(document.documentElement).getPropertyValue(`--${name}`).trim()
  const rgb = raw.split(/\s+/).map(Number)
  if (rgb.length !== 3 || rgb.some((n) => !Number.isFinite(n))) return 'transparent'
  if (alpha < 1) return `rgba(${rgb.join(', ')}, ${alpha})`
  return `#${rgb.map((n) => Math.max(0, Math.min(255, Math.round(n))).toString(16).padStart(2, '0')).join('')}`
}

/** The categorical series order, resolved for the current theme. */
export function seriesColors(): string[] {
  return Array.from({ length: 8 }, (_, i) => tokenColor(`series-${i + 1}`))
}

/** CSS reference to series slot `i` (0-based), for SVG and HTML marks. */
export function seriesVar(i: number): string {
  return `rgb(var(--series-${(i % 8) + 1}))`
}

const FONT = '"Inter Variable", Inter, system-ui, sans-serif'

// Reference palette (validated for colour-vision deficiency in both modes); kept in step with globals.css.
const LIGHT = {
  series: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#6250d6', '#e34948'],
  text: '#0f1726', text2: '#334052', muted: '#64748b', grid: '#e5e9ef', axis: '#cbd3de', surface: '#ffffff', border: '#e2e7ee',
}
const DARK = {
  series: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9', '#e66767'],
  text: '#e8ecf2', text2: '#bcc5d3', muted: '#8a95a8', grid: '#1e2431', axis: '#363f52', surface: '#11151e', border: '#242b39',
}

function theme(p: typeof LIGHT) {
  const axis = {
    axisLine: { lineStyle: { color: p.axis } },
    axisTick: { lineStyle: { color: p.axis } },
    axisLabel: { color: p.muted, fontFamily: FONT },
    splitLine: { lineStyle: { color: p.grid, type: 'solid' } },
    nameTextStyle: { color: p.muted, fontFamily: FONT },
  }
  return {
    color: p.series,
    backgroundColor: 'transparent',
    textStyle: { fontFamily: FONT, color: p.text2 },
    title: { textStyle: { color: p.text, fontFamily: FONT, fontWeight: 600 }, subtextStyle: { color: p.muted, fontFamily: FONT } },
    legend: { textStyle: { color: p.text2, fontFamily: FONT }, icon: 'roundRect', itemWidth: 10, itemHeight: 10 },
    tooltip: {
      backgroundColor: p.surface,
      borderColor: p.border,
      borderWidth: 1,
      textStyle: { color: p.text, fontFamily: FONT },
      extraCssText: 'border-radius:8px;box-shadow:0 8px 24px -8px rgba(0,0,0,.35);',
    },
    line: { symbolSize: 6, lineStyle: { width: 2 }, smooth: false },
    bar: { itemStyle: { borderRadius: [4, 4, 0, 0] } },
    categoryAxis: axis,
    valueAxis: axis,
    timeAxis: axis,
    logAxis: axis,
  }
}

echarts.registerTheme('zenplus-light', theme(LIGHT))
echarts.registerTheme('zenplus-dark', theme(DARK))

/** ECharts theme name for the current colour scheme. */
export function chartTheme(): 'zenplus-light' | 'zenplus-dark' {
  if (typeof document === 'undefined') return 'zenplus-dark'
  return document.documentElement.classList.contains('dark') ? 'zenplus-dark' : 'zenplus-light'
}
