/* Export / import entries for the map's ContextMenu (canvas right-click or a
 * toolbar "Export" button that opens the same menu). Each item does its own
 * work and reports through toasts, so the caller only supplies context. */

import type { Node } from '@xyflow/react'
import { FileCode2, FileJson, FileText, Image as ImageIcon, Share2, Upload } from 'lucide-react'
import { toast } from '@/components/ui/Toast'
import type { ManualMapDetail } from '../core'
import {
  buildDrawioXml,
  buildMapJson,
  downloadText,
  exportFileBase,
  exportMapImage,
  type MapImageKind,
} from '../export/exportMap'
import type { MenuItem } from './ContextMenu'

export type ExportMenuContext = {
  /** The loaded map (nothing exportable while it is still loading). */
  detail: ManualMapDetail | null | undefined
  /** React Flow's getNodes (useReactFlow()). */
  getNodes: () => Node[]
  /** `.react-flow__viewport` or any ancestor (e.g. the canvas wrapper div). */
  getViewportEl: () => HTMLElement | null
  /** Concrete background colour for images; defaults to the theme `--bg`. */
  background?: string
  /** Opens the JSON import flow. Omit to hide "Import JSON…" (e.g. no maps.manage). */
  onImport?: () => void
  /** Include an "Export" header row (default true). */
  header?: boolean
  /** Put the canvas in export state (true-scale labels, no blur) and return
   *  a function that restores it. */
  prepare?: () => Promise<() => void>
}

const ic = 'h-3.5 w-3.5'

export function exportMenuItems(ctx: ExportMenuContext): MenuItem[] {
  const ready = !!ctx.detail
  const name = ctx.detail?.name || 'map'

  const image = (kind: MapImageKind) => async () => {
    if (!ctx.detail) return
    const label = kind.toUpperCase()
    let restore: (() => void) | undefined
    try {
      restore = await ctx.prepare?.()
      await exportMapImage(kind, {
        getNodes: ctx.getNodes,
        viewportEl: ctx.getViewportEl(),
        fileName: exportFileBase(name),
        title: name,
        background: ctx.background,
      })
      toast.success(`${label} exported`)
    } catch (e) {
      toast.error(`${label} export failed`, (e as Error)?.message || String(e))
    } finally {
      restore?.()
    }
  }

  const text = (ext: string, mime: string, build: (d: ManualMapDetail) => string, what: string) => () => {
    if (!ctx.detail) return
    try {
      downloadText(`${exportFileBase(name)}.${ext}`, mime, build(ctx.detail))
      toast.success(`${what} exported`)
    } catch (e) {
      toast.error(`${what} export failed`, (e as Error)?.message || String(e))
    }
  }

  const items: MenuItem[] = []
  if (ctx.header !== false) items.push({ type: 'header', label: 'Export' })
  items.push(
    { type: 'item', label: 'PNG image (2×)', icon: <ImageIcon className={ic} />, onClick: image('png'), disabled: !ready },
    { type: 'item', label: 'SVG vector', icon: <FileCode2 className={ic} />, onClick: image('svg'), disabled: !ready },
    { type: 'item', label: 'PDF (A3 landscape)', icon: <FileText className={ic} />, onClick: image('pdf'), disabled: !ready },
    {
      type: 'item',
      label: 'draw.io (→ Visio via draw.io)',
      icon: <Share2 className={ic} />,
      onClick: text('drawio', 'application/vnd.jgraph.mxfile', buildDrawioXml, 'draw.io file'),
      disabled: !ready,
    },
    {
      type: 'item',
      label: 'JSON backup',
      icon: <FileJson className={ic} />,
      onClick: text('json', 'application/json', (d) => JSON.stringify(buildMapJson(d), null, 2), 'JSON backup'),
      disabled: !ready,
    },
  )
  if (ctx.onImport) {
    const onImport = ctx.onImport
    items.push(
      { type: 'divider' },
      { type: 'item', label: 'Import JSON…', icon: <Upload className={ic} />, onClick: () => onImport(), disabled: !ready },
    )
  }
  return items
}
