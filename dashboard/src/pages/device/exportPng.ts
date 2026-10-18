import { pngLayout } from './performanceModel'

/* PNG export of the Performance chart stack: every panel marked `data-export-panel` (title in
 * `data-export-title`, statistics line in `data-export-stats`, a full-width panel with
 * `data-export-full`) is drawn onto one canvas in the page's current theme. Recharts draws SVG whose
 * colours are theme variables (rgb(var(--primary))), which a detached SVG image cannot resolve, so each
 * element's computed paint is inlined before the SVG is rasterised. Nothing leaves the browser. */

const PAINT = ['fill', 'stroke', 'stroke-width', 'stroke-dasharray', 'stroke-opacity', 'fill-opacity', 'opacity',
  'font-size', 'font-family', 'font-weight', 'text-anchor', 'dominant-baseline', 'visibility', 'display'] as const

function inlineStyles(source: Element, target: Element) {
  const computed = window.getComputedStyle(source)
  const style = PAINT.map((p) => `${p}:${computed.getPropertyValue(p)}`).join(';')
  target.setAttribute('style', style)
  const s = source.children
  const t = target.children
  for (let i = 0; i < s.length && i < t.length; i++) inlineStyles(s[i], t[i])
}

function svgImage(svg: SVGSVGElement): Promise<{ image: HTMLImageElement; width: number; height: number }> {
  const box = svg.getBoundingClientRect()
  const clone = svg.cloneNode(true) as SVGSVGElement
  inlineStyles(svg, clone)
  clone.setAttribute('xmlns', 'http://www.w3.org/2000/svg')
  clone.setAttribute('width', String(box.width))
  clone.setAttribute('height', String(box.height))
  const url = URL.createObjectURL(new Blob([new XMLSerializer().serializeToString(clone)], { type: 'image/svg+xml;charset=utf-8' }))
  return new Promise((resolve, reject) => {
    const image = new Image()
    image.onload = () => { URL.revokeObjectURL(url); resolve({ image, width: box.width, height: box.height }) }
    image.onerror = () => { URL.revokeObjectURL(url); reject(new Error('A chart could not be rendered')) }
    image.src = url
  })
}

const cssColor = (el: Element, prop: string, fallback: string) => window.getComputedStyle(el).getPropertyValue(prop).trim() || fallback

export async function exportPanelsPng(container: HTMLElement, opts: { title: string; subtitle: string; filename: string }): Promise<void> {
  const panels = [...container.querySelectorAll<HTMLElement>('[data-export-panel]')]
  const charts: Array<{ title: string; stats: string; full: boolean; img: Awaited<ReturnType<typeof svgImage>> | null }> = []
  for (const panel of panels) {
    const svg = panel.querySelector<SVGSVGElement>('svg.recharts-surface')
    charts.push({
      title: panel.dataset.exportTitle || '',
      stats: panel.dataset.exportStats || '',
      full: panel.dataset.exportFull === 'true',
      img: svg ? await svgImage(svg) : null,
    })
  }
  if (!charts.length) throw new Error('There are no charts to export')
  const layout = pngLayout(charts.map((c) => ({ width: c.img?.width || 720, height: c.img?.height || 120, full: c.full })))
  const scale = Math.min(2, window.devicePixelRatio || 1)
  const canvas = document.createElement('canvas')
  canvas.width = Math.round(layout.width * scale)
  canvas.height = Math.round(layout.height * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) throw new Error('This browser cannot draw the image')
  ctx.scale(scale, scale)
  const background = cssColor(document.body, 'background-color', '#ffffff')
  const text = cssColor(document.body, 'color', '#111827')
  const sample = panels[0]
  const surface = cssColor(sample, 'background-color', background)
  const border = cssColor(sample, 'border-top-color', 'rgba(127,127,127,0.3)')
  const font = cssColor(document.body, 'font-family', 'sans-serif')
  ctx.fillStyle = background
  ctx.fillRect(0, 0, layout.width, layout.height)
  ctx.fillStyle = text
  ctx.font = `600 20px ${font}`
  ctx.fillText(opts.title, 24, 34)
  ctx.globalAlpha = 0.7
  ctx.font = `13px ${font}`
  ctx.fillText(opts.subtitle, 24, 56)
  ctx.globalAlpha = 1
  charts.forEach((c, i) => {
    const cell = layout.cells[i]
    ctx.fillStyle = surface
    ctx.strokeStyle = border
    ctx.lineWidth = 1
    ctx.beginPath()
    // roundRect is missing from older browsers' canvas.
    const rounded = typeof (ctx as { roundRect?: unknown }).roundRect === 'function'
    if (rounded) ctx.roundRect(cell.x - 8, cell.y - 8, cell.w + 16, cell.h + 8, 10)
    else ctx.rect(cell.x - 8, cell.y - 8, cell.w + 16, cell.h + 8)
    ctx.fill()
    ctx.stroke()
    ctx.fillStyle = text
    ctx.font = `600 14px ${font}`
    ctx.fillText(c.title, cell.x, cell.y + 14)
    const chartH = cell.h - 28 - 24
    if (c.img) ctx.drawImage(c.img.image, cell.x, cell.y + 28, cell.w, chartH)
    ctx.globalAlpha = 0.75
    ctx.font = `12px ${font}`
    ctx.fillText(c.stats, cell.x, cell.y + 28 + chartH + 16)
    ctx.globalAlpha = 1
  })
  const blob: Blob | null = await new Promise((resolve) => canvas.toBlob(resolve, 'image/png'))
  if (!blob) throw new Error('The image could not be created')
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = opts.filename
  a.click()
  window.setTimeout(() => URL.revokeObjectURL(url), 1000)
}
