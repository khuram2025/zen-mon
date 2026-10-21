/* ──────────────────────────────────────────────────────────────────────────
 * Network map — export (PNG / SVG / PDF) + file helpers
 *
 * Images follow React Flow v12's documented "download image" recipe: take the
 * bounds of every node (getNodesBounds), fit them into an image of a chosen
 * size (getViewportForBounds) and render the `.react-flow__viewport` element
 * through html-to-image with that transform — so the export is the whole map
 * at a fixed resolution, independent of the user's current pan/zoom.
 *
 * The pure file formats (draw.io XML, JSON backup, bulk import payload) live
 * in mapFormats.ts and are re-exported from here.
 * ────────────────────────────────────────────────────────────────────────── */

import { getNodesBounds, getViewportForBounds, type Node, type Rect } from '@xyflow/react'
import { toPng, toSvg } from 'html-to-image'
import { jsPDF } from 'jspdf'

export * from './mapFormats'

export type MapImageKind = 'png' | 'svg' | 'pdf'

export type ImageExportOptions = {
  /** Fraction of the image left as margin around the nodes (React Flow padding). */
  padding?: number
  /** Concrete CSS colour; defaults to the current theme's `--bg`. */
  background?: string
  /** PNG only: device pixel ratio of the raster (default 2). */
  pixelRatio?: number
  /** Longest side of the image in CSS px before pixelRatio (default 3200). */
  maxSide?: number
}

// Overlays that must never end up in an export (they are outside the viewport
// element in practice, but a portal or a future refactor could move them in).
const EXCLUDED_CLASSES = [
  'react-flow__minimap',
  'react-flow__controls',
  'react-flow__panel',
  'react-flow__attribution',
  'react-flow__selection',
  'react-flow__nodesselection',
  'react-flow__edgeupdater',
]

/** html-to-image filter: drop React Flow chrome and anything marked
 *  `data-export-exclude` (e.g. editor-only handles). */
export function exportFilter(node: HTMLElement): boolean {
  const el = node as unknown as Element
  if (!el || typeof el.getAttribute !== 'function') return true
  if (el.getAttribute('data-export-exclude') != null) return false
  const cl = el.classList
  if (!cl) return true
  return !EXCLUDED_CLASSES.some((c) => cl.contains(c))
}

/** Resolve the theme background (`rgb(var(--bg))`) to a concrete colour —
 *  html-to-image paints the canvas before CSS variables are available. */
export function themeBackground(fallback = '#0b1220'): string {
  if (typeof document === 'undefined') return fallback
  try {
    const probe = document.createElement('div')
    probe.style.cssText = 'position:absolute;width:0;height:0;visibility:hidden;background-color:rgb(var(--bg))'
    document.body.appendChild(probe)
    const c = getComputedStyle(probe).backgroundColor
    probe.remove()
    if (c && c !== 'rgba(0, 0, 0, 0)' && c !== 'transparent') return c
  } catch {
    /* fall through */
  }
  return fallback
}

/** Image size (CSS px) that keeps the bounds' aspect ratio. */
function imageSizeFor(bounds: Rect, maxSide: number) {
  const w = Math.max(1, bounds.width)
  const h = Math.max(1, bounds.height)
  const s = maxSide / Math.max(w, h)
  return { width: Math.max(400, Math.round(w * s)), height: Math.max(300, Math.round(h * s)) }
}

/** Accept the viewport element itself or any ancestor (e.g. the canvas wrapper). */
function resolveViewport(el: HTMLElement): HTMLElement {
  if (el.classList.contains('react-flow__viewport')) return el
  const vp = el.querySelector<HTMLElement>('.react-flow__viewport')
  if (!vp) throw new Error('Map canvas not found — open the map and try again.')
  return vp
}

function renderOptions(bounds: Rect, opts: ImageExportOptions) {
  const { width, height } = imageSizeFor(bounds, opts.maxSide ?? 3200)
  const vp = getViewportForBounds(bounds, width, height, 0.05, 4, opts.padding ?? 0.04)
  return {
    width,
    height,
    backgroundColor: opts.background || themeBackground(),
    filter: exportFilter,
    // Don't embed webfonts: the Inter stylesheet is cross-origin (its rules
    // can't be read, which floods the console) and an offline appliance can't
    // fetch it anyway. The export falls back to the system UI font and the
    // SVG stays a reasonable size.
    skipFonts: true,
    // Cross-origin images that refuse CORS become a transparent pixel instead
    // of failing the whole export.
    imagePlaceholder: 'data:image/gif;base64,R0lGODlhAQABAAAAACH5BAEKAAEALAAAAAABAAEAAAICTAEAOw==',
    style: {
      width: `${width}px`,
      height: `${height}px`,
      transform: `translate(${vp.x}px, ${vp.y}px) scale(${vp.zoom})`,
    },
  }
}

/** Render the map as a PNG data URL. `el` = `.react-flow__viewport` (or an ancestor). */
export async function exportPng(el: HTMLElement, bounds: Rect, opts: ImageExportOptions = {}): Promise<{ dataUrl: string; width: number; height: number }> {
  const o = renderOptions(bounds, opts)
  const dataUrl = await toPng(resolveViewport(el), { ...o, pixelRatio: opts.pixelRatio ?? 2 })
  return { dataUrl, width: o.width, height: o.height }
}

/** Render the map as an SVG data URL (foreignObject-based, as html-to-image does). */
export async function exportSvg(el: HTMLElement, bounds: Rect, opts: ImageExportOptions = {}): Promise<{ dataUrl: string; width: number; height: number }> {
  const o = renderOptions(bounds, opts)
  const dataUrl = await toSvg(resolveViewport(el), o)
  return { dataUrl, width: o.width, height: o.height }
}

/* ── Downloads ─────────────────────────────────────────────────────────── */

function triggerDownload(name: string, href: string) {
  const a = document.createElement('a')
  a.href = href
  a.download = name
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/** Save a string as a file. */
export function downloadText(name: string, mime: string, text: string) {
  downloadBlob(name, new Blob([text], { type: mime }))
}

export function downloadBlob(name: string, blob: Blob) {
  const url = URL.createObjectURL(blob)
  triggerDownload(name, url)
  // Give the browser time to start the download before revoking.
  setTimeout(() => URL.revokeObjectURL(url), 30_000)
}

/** data: URL → Blob without fetch() (keeps working under a strict CSP). */
export function dataUrlToBlob(dataUrl: string): Blob {
  const comma = dataUrl.indexOf(',')
  const header = dataUrl.slice(5, comma) // after "data:"
  const body = dataUrl.slice(comma + 1)
  const mime = header.split(';')[0] || 'application/octet-stream'
  if (/;base64/i.test(header)) {
    const bin = atob(body)
    const bytes = new Uint8Array(bin.length)
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
    return new Blob([bytes], { type: mime })
  }
  return new Blob([decodeURIComponent(body)], { type: mime })
}

/** Ask the user for a text file (e.g. a JSON backup). Resolves null on cancel. */
export function pickTextFile(accept = '.json,application/json'): Promise<{ name: string; text: string } | null> {
  return new Promise((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = accept
    input.style.display = 'none'
    let settled = false
    const done = (v: { name: string; text: string } | null) => {
      if (settled) return
      settled = true
      input.remove()
      resolve(v)
    }
    input.addEventListener('change', async () => {
      const f = input.files?.[0]
      if (!f) return done(null)
      try {
        done({ name: f.name, text: await f.text() })
      } catch {
        done(null)
      }
    })
    // `cancel` fires in current Chromium/Firefox when the picker is dismissed.
    input.addEventListener('cancel', () => done(null))
    document.body.appendChild(input)
    input.click()
  })
}

/* ── High-level entry point ────────────────────────────────────────────── */

export type MapImageExportArgs = {
  /** React Flow's getNodes (from useReactFlow()). */
  getNodes: () => Node[]
  /** `.react-flow__viewport`, or any ancestor such as the canvas wrapper. */
  viewportEl: HTMLElement | null
  /** Base name without extension (see exportFileBase). */
  fileName: string
  /** Map title — printed in the PDF header. */
  title: string
  background?: string
}

/** Render + download the map as PNG, SVG or PDF (A3 landscape). */
export async function exportMapImage(kind: MapImageKind, args: MapImageExportArgs): Promise<void> {
  const { getNodes, viewportEl, fileName, title } = args
  if (!viewportEl) throw new Error('Map canvas not found — open the map and try again.')
  const nodes = getNodes().filter((n) => !n.hidden)
  if (!nodes.length) throw new Error('The map is empty — nothing to export.')
  const bounds = getNodesBounds(nodes)
  const opts: ImageExportOptions = { background: args.background }

  if (kind === 'svg') {
    const { dataUrl } = await exportSvg(viewportEl, bounds, opts)
    downloadBlob(`${fileName}.svg`, dataUrlToBlob(dataUrl))
    return
  }

  const { dataUrl, width, height } = await exportPng(viewportEl, bounds, opts)
  if (kind === 'png') {
    downloadBlob(`${fileName}.png`, dataUrlToBlob(dataUrl))
    return
  }

  // PDF: A3 landscape, header with title + timestamp, map fitted below.
  const pdf = new jsPDF({ orientation: 'landscape', unit: 'mm', format: 'a3' })
  const pageW = pdf.internal.pageSize.getWidth()
  const pageH = pdf.internal.pageSize.getHeight()
  const margin = 10
  const headerH = 14
  pdf.setFont('helvetica', 'bold')
  pdf.setFontSize(15)
  pdf.setTextColor(17, 24, 39)
  pdf.text(title || 'Network map', margin, margin + 6)
  pdf.setFont('helvetica', 'normal')
  pdf.setFontSize(9)
  pdf.setTextColor(100, 116, 139)
  pdf.text(`Exported ${new Date().toLocaleString()} · ZenPlus`, pageW - margin, margin + 6, { align: 'right' })
  pdf.setDrawColor(203, 213, 225)
  pdf.line(margin, margin + headerH - 4, pageW - margin, margin + headerH - 4)

  const boxW = pageW - margin * 2
  const boxH = pageH - margin * 2 - headerH
  const s = Math.min(boxW / width, boxH / height)
  const w = width * s
  const h = height * s
  pdf.addImage(dataUrl, 'PNG', margin + (boxW - w) / 2, margin + headerH + (boxH - h) / 2, w, h, undefined, 'FAST')
  pdf.save(`${fileName}.pdf`)
}
