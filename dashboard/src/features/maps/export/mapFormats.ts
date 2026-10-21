/* ──────────────────────────────────────────────────────────────────────────
 * Network map — file formats (pure, DOM-free)
 *
 * draw.io XML, the versioned JSON backup and the backup → POST /maps/{id}/bulk
 * conversion. Kept free of runtime imports (types only) so it runs in plain
 * Node for scripts/test-map-export.mjs; the browser-side image/PDF exporters
 * live in exportMap.ts, which re-exports everything here.
 * ────────────────────────────────────────────────────────────────────────── */

import type {
  AnnotationLink,
  LinkMetadata,
  ManualMapDetail,
  ManualMapLink,
  ManualMapNode,
  MapShape,
} from '../core'

// Mirrors LOGICAL_W / LOGICAL_H in core.ts (the test asserts they match) —
// duplicated so this module stays import-free at runtime.
const CANVAS_W = 4000
const CANVAS_H = 2400

/* ── Shared helpers ────────────────────────────────────────────────────── */

type NodeKind = 'device' | 'server' | 'service_check'

/** Node kind with the legacy fallback (older backends omit `kind`). */
function kindOf(n: { kind?: string | null; server_id?: string | null; service_check_id?: string | null }): NodeKind {
  if (n.kind === 'server' || n.kind === 'service_check' || n.kind === 'device') return n.kind
  if (n.server_id) return 'server'
  if (n.service_check_id) return 'service_check'
  return 'device'
}

/** The entity id a node represents, by kind. */
function entityOf(n: {
  kind?: string | null
  device_id?: string | null
  server_id?: string | null
  service_check_id?: string | null
}): string | null {
  const k = kindOf(n)
  const v = k === 'server' ? n.server_id : k === 'service_check' ? n.service_check_id : n.device_id
  return typeof v === 'string' && v ? v : null
}

function annotationLinksOfMeta(meta: unknown): AnnotationLink[] {
  const raw = (meta as Record<string, unknown> | null | undefined)?.annotation_links
  return Array.isArray(raw) ? (raw as AnnotationLink[]) : []
}

function clampNum(v: number, lo: number, hi: number) {
  return Math.max(lo, Math.min(hi, v))
}

/** File-system friendly slug for download names. */
export function slugify(name: string): string {
  return (name || 'map')
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    // Keep letters/digits of any script (Arabic site names stay readable).
    .replace(/[^\p{L}\p{N}\s-]/gu, ' ')
    .trim()
    .replace(/[\s_-]+/g, '-')
    .toLowerCase()
    .slice(0, 60) || 'map'
}

/** "zenplus-map-<slug>-YYYYMMDD-HHMM" (local time). */
export function exportFileBase(name: string, at: Date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0')
  const stamp = `${at.getFullYear()}${p(at.getMonth() + 1)}${p(at.getDate())}-${p(at.getHours())}${p(at.getMinutes())}`
  return `zenplus-map-${slugify(name)}-${stamp}`
}

/* ── draw.io (diagrams.net) ───────────────────────────────────────────────
 * Uncompressed mxGraph XML. One vertex per device node and per shape, one edge
 * per link / annotation link. draw.io opens it directly and can then export
 * Visio (.vsdx), so this also covers the "Visio" request. Coordinates come
 * from the percent model on the 4000×2400 logical canvas. */

const DRAWIO_STATUS: Record<string, { fill: string; stroke: string }> = {
  up: { fill: '#d5e8d4', stroke: '#82b366' },
  down: { fill: '#f8cecc', stroke: '#b85450' },
  degraded: { fill: '#fff2cc', stroke: '#d6b656' },
  maintenance: { fill: '#dae8fc', stroke: '#6c8ebf' },
  unknown: { fill: '#f5f5f5', stroke: '#666666' },
}

const DRAWIO_NODE_W = 140
const DRAWIO_NODE_H = 56

/** XML attribute escape; newlines become draw.io line breaks. */
export function xmlEscape(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;')
    .replace(/\r?\n/g, '&#xa;')
}

/** Any CSS colour we store (#rgb, #rrggbb, #rrggbbaa, rgb(), rgba()) →
 *  draw.io's hex + opacity (0–100). Unknown formats return null. */
export function toDrawioColor(c: string | null | undefined): { hex: string; opacity: number } | null {
  if (!c) return null
  const v = c.trim().toLowerCase()
  if (v === 'none' || v === 'transparent') return null
  let m = v.match(/^#([0-9a-f]{3})$/)
  if (m) return { hex: '#' + m[1].split('').map((ch) => ch + ch).join(''), opacity: 100 }
  m = v.match(/^#([0-9a-f]{6})([0-9a-f]{2})?$/)
  if (m) return { hex: '#' + m[1], opacity: m[2] ? Math.round((parseInt(m[2], 16) / 255) * 100) : 100 }
  m = v.match(/^rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)(?:[,\s/]+([\d.]+%?))?\s*\)$/)
  if (m) {
    const hex = '#' + [m[1], m[2], m[3]].map((x) => clampNum(Math.round(Number(x)), 0, 255).toString(16).padStart(2, '0')).join('')
    let a = 1
    if (m[4] != null) a = m[4].endsWith('%') ? Number(m[4].slice(0, -1)) / 100 : Number(m[4])
    return { hex, opacity: Math.round(clampNum(a, 0, 1) * 100) }
  }
  return null
}

/** draw.io style string; `true` emits a bare named style token (text, ellipse…). */
function styleString(parts: Record<string, string | number | boolean | null | undefined>): string {
  return Object.entries(parts)
    .filter(([, v]) => v !== null && v !== undefined && v !== '' && v !== false)
    .map(([k, v]) => (v === true ? k : `${k}=${v}`))
    .join(';') + ';'
}

function colorStyle(prefix: 'fill' | 'stroke', c: string | null | undefined, fallback: string): Record<string, string | number> {
  const parsed = toDrawioColor(c)
  if (c && !parsed && /^(none|transparent)$/i.test(c.trim())) return { [`${prefix}Color`]: 'none' }
  if (!parsed) return { [`${prefix}Color`]: fallback }
  const out: Record<string, string | number> = { [`${prefix}Color`]: parsed.hex }
  if (parsed.opacity < 100) out[`${prefix}Opacity`] = parsed.opacity
  return out
}

function nodeCaption(n: ManualMapNode): string {
  const name = (n.label || n.hostname || '').trim() || 'node'
  return n.ip_address && !n.metadata?.hide_ip ? `${name}\n${n.ip_address}` : name
}

function shapeCaption(s: MapShape): string {
  const text = (s.text || '').trim()
  if (text) return text
  const md = s.metadata || {}
  if (md.widget === 'conversations') return 'Top conversations (live)'
  if (s.kind === 'image') {
    const un = md.unmanaged as { hostname?: string | null } | null | undefined
    if (un?.hostname) return un.hostname
    if (typeof md.icon === 'string') return md.icon.replace(/_/g, ' ')
    return 'image'
  }
  return ''
}

function fontStyleOf(md: { bold?: boolean; italic?: boolean }): number | undefined {
  const v = (md.bold ? 1 : 0) | (md.italic ? 2 : 0)
  return v || undefined
}

function linkCaption(label: string | null | undefined, md: LinkMetadata | null | undefined): string {
  const a = (md?.src_interface || '').trim()
  const b = (md?.dst_interface || '').trim()
  const ifaces = a && b ? `${a} ⇄ ${b}` : a || b
  const members = Array.isArray(md?.members) && md!.members!.length > 1 ? ` (×${md!.members!.length})` : ''
  const lbl = (label || '').trim()
  if (ifaces) return lbl ? `${lbl}\n${ifaces}${members}` : `${ifaces}${members}`
  return lbl + members
}

function edgeStyle(kind: string, md: LinkMetadata | null | undefined, fallbackStroke: string): string {
  const shape = md?.shape
  const dashed = md?.dash === 'dashed' || md?.dash === 'dotted' || (!md?.dash && (kind === 'vpn' || kind === 'wireless'))
  const stroke = md?.color && toDrawioColor(md.color)
    ? toDrawioColor(md.color)!.hex
    : kind === 'fiber' ? '#f59e0b' : kind === 'vpn' ? '#8b5cf6' : fallbackStroke
  const arrows = md?.arrows || 'none'
  return styleString({
    endArrow: arrows === 'forward' || arrows === 'both' ? 'block' : 'none',
    startArrow: arrows === 'backward' || arrows === 'both' ? 'block' : 'none',
    html: 0,
    rounded: 0,
    edgeStyle: shape === 'orthogonal' ? 'orthogonalEdgeStyle' : 'none',
    curved: shape === 'curve' || !shape ? 1 : undefined,
    strokeWidth: Math.round(2 * (md?.width_scale || 1) * (kind === 'trunk' ? 1.4 : 1) * 10) / 10,
    strokeColor: stroke,
    dashed: dashed ? 1 : undefined,
    dashPattern: md?.dash === 'dotted' || (!md?.dash && kind === 'wireless') ? '1 4' : undefined,
    fontSize: 10,
    labelBackgroundColor: '#ffffff',
  })
}

function waypointsXml(md: LinkMetadata | null | undefined): string {
  const w = Array.isArray(md?.waypoints) ? md!.waypoints! : []
  if (!w.length) return ''
  const pts = w
    .filter((p) => typeof p?.x_pct === 'number' && typeof p?.y_pct === 'number')
    .map((p) => `<mxPoint x="${round1((p.x_pct / 100) * CANVAS_W)}" y="${round1((p.y_pct / 100) * CANVAS_H)}" />`)
    .join('')
  return pts ? `<Array as="points">${pts}</Array>` : ''
}

function round1(v: number) {
  return Math.round(v * 10) / 10
}

/** draw.io mxGraph XML for a map. */
export function buildDrawioXml(detail: ManualMapDetail): string {
  const cells: string[] = []
  const nodeIds = new Map<string, string>()
  const shapeIds = new Map<string, string>()
  const nodes = detail.nodes || []
  const shapes = (detail.shapes || []).slice().sort((a, b) => (a.z_index || 0) - (b.z_index || 0))
  const statusOf = new Map(nodes.map((n) => [n.id, String(n.status || 'unknown').toLowerCase()]))

  // 1) Shapes first (lowest z) so zones sit behind cables and devices.
  for (const s of shapes) {
    const md = s.metadata || {}
    const x = round1((s.x_pct / 100) * CANVAS_W)
    const y = round1((s.y_pct / 100) * CANVAS_H)
    const w = Math.max(4, round1((s.w_pct / 100) * CANVAS_W))
    const h = Math.max(4, round1((s.h_pct / 100) * CANVAS_H))
    const id = `s-${s.id}`
    const caption = shapeCaption(s)

    // Lines / arrows become free-standing edges across the box.
    if (s.kind === 'line' || s.kind === 'arrow') {
      const style = styleString({
        endArrow: s.kind === 'arrow' ? 'block' : 'none',
        html: 0,
        ...colorStyle('stroke', s.stroke || md.color, '#64748b'),
        strokeWidth: md.strokeWidth || 2,
        dashed: md.dash && md.dash !== 'solid' ? 1 : undefined,
      })
      cells.push(
        `<mxCell id="${xmlEscape(id)}" value="${xmlEscape(caption)}" style="${xmlEscape(style)}" edge="1" parent="1">` +
          `<mxGeometry relative="1" as="geometry">` +
          `<mxPoint x="${x}" y="${round1(y + h / 2)}" as="sourcePoint" />` +
          `<mxPoint x="${round1(x + w)}" y="${round1(y + h / 2)}" as="targetPoint" />` +
          `</mxGeometry></mxCell>`,
      )
      continue
    }

    const base: Record<string, string | number | boolean | undefined> = {
      whiteSpace: 'wrap',
      html: 0,
      fontSize: typeof md.fontSize === 'number' ? Math.round(md.fontSize) : undefined,
      fontStyle: fontStyleOf(md),
      fontColor: toDrawioColor(md.color)?.hex,
      align: md.align || undefined,
      rotation: typeof md.rotation === 'number' && md.rotation ? md.rotation : undefined,
      opacity: typeof md.opacity === 'number' && md.opacity < 1 ? Math.round(md.opacity * 100) : undefined,
      strokeWidth: typeof md.strokeWidth === 'number' ? md.strokeWidth : undefined,
      dashed: md.dash && md.dash !== 'solid' ? 1 : undefined,
    }
    let style: string
    switch (s.kind) {
      case 'text':
        style = styleString({ text: true, ...base, strokeColor: 'none', fillColor: 'none', verticalAlign: 'middle' })
        break
      case 'circle':
        style = styleString({ ellipse: true, ...base, ...colorStyle('fill', s.fill, 'none'), ...colorStyle('stroke', s.stroke, '#64748b') })
        break
      case 'diamond':
        style = styleString({ rhombus: true, ...base, ...colorStyle('fill', s.fill, 'none'), ...colorStyle('stroke', s.stroke, '#64748b') })
        break
      case 'hexagon':
        style = styleString({ shape: 'hexagon', perimeter: 'hexagonPerimeter2', ...base, ...colorStyle('fill', s.fill, 'none'), ...colorStyle('stroke', s.stroke, '#64748b') })
        break
      case 'sticky':
        style = styleString({ shape: 'note', size: 12, ...base, verticalAlign: 'top', ...colorStyle('fill', s.fill, '#fde68a'), ...colorStyle('stroke', s.stroke, '#d6b656') })
        break
      case 'image':
        // Icons / images / unmanaged neighbours: rounded box with the caption.
        style = styleString({ rounded: 1, ...base, ...colorStyle('fill', s.fill, '#f8fafc'), ...colorStyle('stroke', s.stroke || md.color, '#94a3b8'), verticalAlign: 'middle' })
        break
      default:
        // rectangle (incl. zones and live widgets): captions of boxes with a
        // title read like a zone header → top-aligned.
        style = styleString({
          rounded: md.rounded ? 1 : 0,
          arcSize: md.rounded ? 4 : undefined,
          ...base,
          verticalAlign: caption ? 'top' : undefined,
          ...colorStyle('fill', s.fill, 'none'),
          ...colorStyle('stroke', s.stroke, '#64748b'),
        })
    }
    shapeIds.set(s.id, id)
    cells.push(
      `<mxCell id="${xmlEscape(id)}" value="${xmlEscape(caption)}" style="${xmlEscape(style)}" vertex="1" parent="1">` +
        `<mxGeometry x="${x}" y="${y}" width="${w}" height="${h}" as="geometry" /></mxCell>`,
    )
  }

  // 2) Device / server / check vertices (registered now, emitted last so they
  //    paint above the cables).
  const nodeCells: string[] = []
  for (const n of nodes) {
    const st = DRAWIO_STATUS[String(n.status || 'unknown').toLowerCase()] || DRAWIO_STATUS.unknown
    const cx = (n.x_pct / 100) * CANVAS_W
    const cy = (n.y_pct / 100) * CANVAS_H
    const id = `n-${n.id}`
    nodeIds.set(n.id, id)
    const style = styleString({
      rounded: 1,
      whiteSpace: 'wrap',
      html: 0,
      fillColor: st.fill,
      strokeColor: st.stroke,
      fontColor: '#1f2937',
      fontSize: 11,
      fontStyle: 1,
    })
    nodeCells.push(
      `<mxCell id="${xmlEscape(id)}" value="${xmlEscape(nodeCaption(n))}" style="${xmlEscape(style)}" vertex="1" parent="1">` +
        `<mxGeometry x="${round1(cx - DRAWIO_NODE_W / 2)}" y="${round1(cy - DRAWIO_NODE_H / 2)}" width="${DRAWIO_NODE_W}" height="${DRAWIO_NODE_H}" as="geometry" /></mxCell>`,
    )
  }

  // 3) Device↔device links.
  const linkStroke = (a?: string, b?: string) => {
    if (a === 'down' || b === 'down') return '#b85450'
    if (a === 'degraded' || b === 'degraded') return '#d6b656'
    return '#64748b'
  }
  for (const l of detail.links || []) {
    const src = nodeIds.get(l.source_node_id)
    const tgt = nodeIds.get(l.target_node_id)
    if (!src || !tgt) continue
    const md = l.metadata || {}
    const kind = String(md.kind || l.link_type || 'ethernet')
    cells.push(
      `<mxCell id="${xmlEscape(`l-${l.id}`)}" value="${xmlEscape(linkCaption(l.label, md))}" ` +
        `style="${xmlEscape(edgeStyle(kind, md, linkStroke(statusOf.get(l.source_node_id), statusOf.get(l.target_node_id))))}" ` +
        `edge="1" parent="1" source="${xmlEscape(src)}" target="${xmlEscape(tgt)}">` +
        `<mxGeometry relative="1" as="geometry">${waypointsXml(md)}</mxGeometry></mxCell>`,
    )
  }

  // 4) Annotation links (cables touching a shape/icon).
  for (const al of annotationLinksOfMeta(detail.metadata)) {
    const src = al.source_type === 'shape' ? shapeIds.get(al.source) : nodeIds.get(al.source)
    const tgt = al.target_type === 'shape' ? shapeIds.get(al.target) : nodeIds.get(al.target)
    if (!src || !tgt) continue
    const md = al.metadata || {}
    const kind = String(md.kind || al.link_type || 'ethernet')
    cells.push(
      `<mxCell id="${xmlEscape(`a-${al.id}`)}" value="${xmlEscape(linkCaption(al.label, md))}" ` +
        `style="${xmlEscape(edgeStyle(kind, md, '#64748b'))}" ` +
        `edge="1" parent="1" source="${xmlEscape(src)}" target="${xmlEscape(tgt)}">` +
        `<mxGeometry relative="1" as="geometry">${waypointsXml(md)}</mxGeometry></mxCell>`,
    )
  }

  const name = detail.name || 'Network map'
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    `<mxfile host="ZenPlus" agent="ZenPlus network maps" type="device">`,
    `<diagram id="${xmlEscape(detail.id || 'map')}" name="${xmlEscape(name)}">`,
    `<mxGraphModel dx="${CANVAS_W}" dy="${CANVAS_H}" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="${CANVAS_W}" pageHeight="${CANVAS_H}" math="0" shadow="0">`,
    '<root>',
    '<mxCell id="0" />',
    '<mxCell id="1" parent="0" />',
    ...cells,
    ...nodeCells,
    '</root>',
    '</mxGraphModel>',
    '</diagram>',
    '</mxfile>',
    '',
  ].join('\n')
}

/* ── JSON backup ──────────────────────────────────────────────────────────
 * A versioned, self-describing copy of the map's layout. Live fields (status,
 * last_seen, location…) are dropped; hostname/IP stay so an import can say
 * which nodes it could not place. */

export const MAP_BACKUP_FORMAT = 'zenplus-map'
export const MAP_BACKUP_VERSION = 1

export type MapBackupNode = {
  id: string
  kind: NodeKind
  device_id: string | null
  server_id: string | null
  service_check_id: string | null
  label: string | null
  icon: string | null
  x_pct: number
  y_pct: number
  metadata: Record<string, unknown> | null
  /** Informational — used for import warnings only. */
  hostname?: string | null
  ip_address?: string | null
  device_type?: string | null
}

export type MapBackupLink = {
  id: string
  source_node_id: string
  target_node_id: string
  label: string | null
  link_type: string | null
  metadata: Record<string, unknown> | null
}

export type MapBackupShape = {
  id: string
  kind: string
  x_pct: number
  y_pct: number
  w_pct: number
  h_pct: number
  text: string | null
  fill: string | null
  stroke: string | null
  z_index: number
  metadata: Record<string, unknown> | null
}

export type MapBackup = {
  format: typeof MAP_BACKUP_FORMAT
  version: number
  exported_at: string
  /** Map-level metadata includes annotation_links (as stored on the server). */
  map: { name: string; description: string | null; metadata: Record<string, unknown> }
  nodes: MapBackupNode[]
  links: MapBackupLink[]
  shapes: MapBackupShape[]
}

export function buildMapJson(detail: ManualMapDetail, now: Date = new Date()): MapBackup {
  return {
    format: MAP_BACKUP_FORMAT,
    version: MAP_BACKUP_VERSION,
    exported_at: now.toISOString(),
    map: {
      name: detail.name,
      description: detail.description ?? null,
      metadata: { ...(detail.metadata || {}) },
    },
    nodes: (detail.nodes || []).map((n) => {
      const kind = kindOf(n)
      return {
        id: n.id,
        kind,
        device_id: kind === 'device' ? n.device_id ?? null : null,
        server_id: kind === 'server' ? n.server_id ?? null : null,
        service_check_id: kind === 'service_check' ? n.service_check_id ?? null : null,
        label: n.label ?? null,
        icon: n.icon ?? null,
        x_pct: n.x_pct,
        y_pct: n.y_pct,
        metadata: (n.metadata as Record<string, unknown> | null | undefined) ?? null,
        hostname: n.hostname ?? null,
        ip_address: n.ip_address ?? null,
        device_type: n.device_type ?? null,
      }
    }),
    links: (detail.links || []).map((l: ManualMapLink) => ({
      id: l.id,
      source_node_id: l.source_node_id,
      target_node_id: l.target_node_id,
      label: l.label ?? null,
      link_type: l.link_type ?? null,
      metadata: (l.metadata as Record<string, unknown> | null | undefined) ?? null,
    })),
    shapes: (detail.shapes || []).map((s) => ({
      id: s.id,
      kind: s.kind,
      x_pct: s.x_pct,
      y_pct: s.y_pct,
      w_pct: s.w_pct,
      h_pct: s.h_pct,
      text: s.text ?? null,
      fill: s.fill ?? null,
      stroke: s.stroke ?? null,
      z_index: s.z_index ?? 0,
      metadata: (s.metadata as Record<string, unknown> | null | undefined) ?? null,
    })),
  }
}

const isObj = (v: unknown): v is Record<string, unknown> => !!v && typeof v === 'object' && !Array.isArray(v)
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v)
const isStr = (v: unknown): v is string => typeof v === 'string' && v.length > 0
const strOrNull = (v: unknown): string | null => (typeof v === 'string' ? v : null)

/** Parse + validate a backup file. Also accepts a raw version snapshot
 *  ({name, metadata, nodes, links, shapes}). Throws Error with a message
 *  that can be shown to the user as-is. */
export function parseMapJson(text: string): MapBackup {
  let raw: unknown
  try {
    raw = JSON.parse(text)
  } catch (e) {
    throw new Error(`The file is not valid JSON (${(e as Error).message}).`)
  }
  if (!isObj(raw)) throw new Error('The file does not contain a map backup object.')

  let mapPart: Record<string, unknown>
  if (raw.format === undefined && Array.isArray(raw.nodes) && typeof raw.name === 'string') {
    // Version snapshot (GET /maps/{id}/versions/{vid}.snapshot) saved to a file.
    mapPart = { name: raw.name, description: raw.description ?? null, metadata: raw.metadata ?? {} }
  } else {
    if (raw.format !== MAP_BACKUP_FORMAT) throw new Error('This is not a ZenPlus map backup (missing "format": "zenplus-map").')
    if (!isNum(raw.version) || raw.version < 1) throw new Error('The backup has no valid version number.')
    if (raw.version > MAP_BACKUP_VERSION) {
      throw new Error(`The backup was made by a newer ZenPlus (format version ${raw.version}); this version reads up to ${MAP_BACKUP_VERSION}.`)
    }
    if (!isObj(raw.map)) throw new Error('The backup has no "map" section.')
    mapPart = raw.map
  }

  const nodesRaw = raw.nodes ?? []
  const linksRaw = raw.links ?? []
  const shapesRaw = raw.shapes ?? []
  if (!Array.isArray(nodesRaw)) throw new Error('"nodes" must be a list.')
  if (!Array.isArray(linksRaw)) throw new Error('"links" must be a list.')
  if (!Array.isArray(shapesRaw)) throw new Error('"shapes" must be a list.')

  const seen = new Set<string>()
  const nodes: MapBackupNode[] = nodesRaw.map((n, i) => {
    const where = `Node ${i + 1}`
    if (!isObj(n)) throw new Error(`${where} is not an object.`)
    if (!isStr(n.id)) throw new Error(`${where} has no id.`)
    if (seen.has(n.id)) throw new Error(`${where} repeats id ${n.id}.`)
    seen.add(n.id)
    if (!isNum(n.x_pct) || !isNum(n.y_pct)) throw new Error(`${where} (${n.id}) has no numeric x_pct / y_pct.`)
    const kind = kindOf(n as { kind?: string })
    const node: MapBackupNode = {
      id: n.id,
      kind,
      device_id: kind === 'device' ? strOrNull(n.device_id) : null,
      server_id: kind === 'server' ? strOrNull(n.server_id) : null,
      service_check_id: kind === 'service_check' ? strOrNull(n.service_check_id) : null,
      label: strOrNull(n.label),
      icon: strOrNull(n.icon),
      x_pct: n.x_pct,
      y_pct: n.y_pct,
      metadata: isObj(n.metadata) ? n.metadata : null,
      hostname: strOrNull(n.hostname),
      ip_address: strOrNull(n.ip_address),
      device_type: strOrNull(n.device_type),
    }
    if (!entityOf(node)) throw new Error(`${where} (${n.id}) has no ${kind === 'device' ? 'device_id' : `${kind}_id`}.`)
    return node
  })

  const shapeIds = new Set<string>()
  const shapes: MapBackupShape[] = shapesRaw.map((s, i) => {
    const where = `Shape ${i + 1}`
    if (!isObj(s)) throw new Error(`${where} is not an object.`)
    if (!isStr(s.id)) throw new Error(`${where} has no id.`)
    if (seen.has(s.id) || shapeIds.has(s.id)) throw new Error(`${where} repeats id ${s.id}.`)
    shapeIds.add(s.id)
    if (!isStr(s.kind)) throw new Error(`${where} (${s.id}) has no kind.`)
    for (const k of ['x_pct', 'y_pct', 'w_pct', 'h_pct'] as const) {
      if (!isNum(s[k])) throw new Error(`${where} (${s.id}) has no numeric ${k}.`)
    }
    return {
      id: s.id,
      kind: s.kind,
      x_pct: s.x_pct as number,
      y_pct: s.y_pct as number,
      w_pct: s.w_pct as number,
      h_pct: s.h_pct as number,
      text: strOrNull(s.text),
      fill: strOrNull(s.fill),
      stroke: strOrNull(s.stroke),
      z_index: isNum(s.z_index) ? s.z_index : 0,
      metadata: isObj(s.metadata) ? s.metadata : null,
    }
  })

  const links: MapBackupLink[] = linksRaw.map((l, i) => {
    const where = `Link ${i + 1}`
    if (!isObj(l)) throw new Error(`${where} is not an object.`)
    if (!isStr(l.source_node_id) || !isStr(l.target_node_id)) throw new Error(`${where} needs source_node_id and target_node_id.`)
    return {
      id: isStr(l.id) ? l.id : `link-${i + 1}`,
      source_node_id: l.source_node_id,
      target_node_id: l.target_node_id,
      label: strOrNull(l.label),
      link_type: strOrNull(l.link_type),
      metadata: isObj(l.metadata) ? l.metadata : null,
    }
  })

  const metadata = isObj(mapPart.metadata) ? { ...mapPart.metadata } : {}
  if (metadata.annotation_links !== undefined && !Array.isArray(metadata.annotation_links)) {
    throw new Error('"map.metadata.annotation_links" must be a list.')
  }
  return {
    format: MAP_BACKUP_FORMAT,
    version: MAP_BACKUP_VERSION,
    exported_at: typeof raw.exported_at === 'string' ? raw.exported_at : new Date(0).toISOString(),
    map: {
      name: typeof mapPart.name === 'string' && mapPart.name ? mapPart.name : 'Imported map',
      description: strOrNull(mapPart.description),
      metadata,
    },
    nodes,
    links,
    shapes,
  }
}

/* ── Backup → POST /maps/{id}/bulk ────────────────────────────────────────
 * Contract item 10. Client ids are the backup's own node / shape ids; the
 * server answers with id_map {client_id: new_id}. Nodes whose entity no
 * longer exists here are dropped (returned for a warning), together with any
 * cable that touched them. */

export type BulkNode = {
  client_id: string
  device_id?: string
  server_id?: string
  service_check_id?: string
  label?: string | null
  icon?: string | null
  x_pct: number
  y_pct: number
  metadata?: Record<string, unknown> | null
}

export type BulkShape = {
  client_id: string
  kind: string
  x_pct: number
  y_pct: number
  w_pct: number
  h_pct: number
  text?: string | null
  fill?: string | null
  stroke?: string | null
  z_index?: number
  metadata?: Record<string, unknown> | null
}

export type BulkLink = {
  source: string
  target: string
  label?: string | null
  link_type?: string | null
  metadata?: Record<string, unknown> | null
}

export type BulkAnnotationLink = {
  id?: string
  source: string
  target: string
  source_type: 'node' | 'shape'
  target_type: 'node' | 'shape'
  label?: string | null
  link_type?: string
  metadata?: Record<string, unknown>
}

export type BulkPayload = {
  nodes: BulkNode[]
  shapes: BulkShape[]
  links: BulkLink[]
  annotation_links: BulkAnnotationLink[]
}

export type BulkResponse = {
  id_map: Record<string, string>
  link_ids: string[]
  annotation_link_ids: string[]
}

export type DroppedNode = {
  id: string
  kind: NodeKind
  entity_id: string | null
  name: string
}

/** Entity ids that exist on this appliance. A plain Set is checked for every
 *  kind (pass devices ∪ servers ∪ checks); the object form checks per kind and
 *  treats an omitted kind as "keep everything of that kind". */
export type ExistingEntities =
  | Set<string>
  | { devices?: Set<string>; servers?: Set<string>; service_checks?: Set<string> }

export type BulkConversion = {
  payload: BulkPayload
  /** Nodes whose device/server/check is not present here. */
  dropped: DroppedNode[]
  /** Links + annotation links skipped because an endpoint was dropped/missing. */
  droppedLinks: number
  /** Map-level metadata without annotation_links (they travel in the payload);
   *  the caller may PUT it to restore background/theme settings. */
  mapMetadata: Record<string, unknown>
}

function entityExists(existing: ExistingEntities, kind: NodeKind, id: string): boolean {
  if (existing instanceof Set) return existing.has(id)
  const set = kind === 'server' ? existing.servers : kind === 'service_check' ? existing.service_checks : existing.devices
  return set ? set.has(id) : true
}

export function toBulkPayload(backup: MapBackup, existingDeviceIds: ExistingEntities): BulkConversion {
  const dropped: DroppedNode[] = []
  const nodes: BulkNode[] = []
  const keptNodes = new Set<string>()
  for (const n of backup.nodes) {
    const entity = entityOf(n)
    if (!entity || !entityExists(existingDeviceIds, n.kind, entity)) {
      dropped.push({ id: n.id, kind: n.kind, entity_id: entity, name: n.label || n.hostname || n.ip_address || entity || n.id })
      continue
    }
    keptNodes.add(n.id)
    const ref =
      n.kind === 'server' ? { server_id: entity } : n.kind === 'service_check' ? { service_check_id: entity } : { device_id: entity }
    nodes.push({
      client_id: n.id,
      ...ref,
      label: n.label,
      icon: n.icon,
      // The API only accepts node positions in 2–98 %.
      x_pct: clampNum(n.x_pct, 2, 98),
      y_pct: clampNum(n.y_pct, 2, 98),
      metadata: n.metadata,
    })
  }

  const keptShapes = new Set<string>()
  const shapes: BulkShape[] = backup.shapes.map((s) => {
    keptShapes.add(s.id)
    return {
      client_id: s.id,
      kind: s.kind,
      x_pct: clampNum(s.x_pct, 0, 100),
      y_pct: clampNum(s.y_pct, 0, 100),
      w_pct: clampNum(s.w_pct, 0.1, 100),
      h_pct: clampNum(s.h_pct, 0.1, 100),
      text: s.text,
      fill: s.fill,
      stroke: s.stroke,
      z_index: s.z_index,
      metadata: s.metadata,
    }
  })

  let droppedLinks = 0
  const links: BulkLink[] = []
  for (const l of backup.links) {
    if (!keptNodes.has(l.source_node_id) || !keptNodes.has(l.target_node_id)) { droppedLinks++; continue }
    links.push({ source: l.source_node_id, target: l.target_node_id, label: l.label, link_type: l.link_type, metadata: l.metadata })
  }

  // Annotation links: endpoints are node or shape ids from the backup, i.e.
  // exactly the client ids above. The link's own id is left to the server.
  const annotation_links: BulkAnnotationLink[] = []
  const endpointKept = (id: string, t: 'node' | 'shape') => (t === 'shape' ? keptShapes.has(id) : keptNodes.has(id))
  for (const al of annotationLinksOfMeta(backup.map.metadata)) {
    if (!al || typeof al.source !== 'string' || typeof al.target !== 'string') { droppedLinks++; continue }
    const st = al.source_type === 'shape' ? 'shape' : 'node'
    const tt = al.target_type === 'shape' ? 'shape' : 'node'
    if (!endpointKept(al.source, st) || !endpointKept(al.target, tt)) { droppedLinks++; continue }
    const out: BulkAnnotationLink = { source: al.source, target: al.target, source_type: st, target_type: tt }
    if (al.label != null) out.label = al.label
    if (al.link_type) out.link_type = al.link_type
    if (al.metadata) out.metadata = al.metadata as Record<string, unknown>
    annotation_links.push(out)
  }

  const { annotation_links: _omit, ...mapMetadata } = backup.map.metadata || {}
  return { payload: { nodes, shapes, links, annotation_links }, dropped, droppedLinks, mapMetadata }
}
