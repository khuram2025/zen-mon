import { memo, useEffect, useRef, useState } from 'react'
import { Handle, NodeResizer, Position, type NodeProps } from '@xyflow/react'
import { Lock } from 'lucide-react'
import { cn } from '@/lib/utils'
import { NetworkIcon } from '@/components/network-icons'
import { LINK_DASH_PATTERN, type MapShape } from '../core'
import { useMapMode } from './MapModeContext'
import { ConversationsWidget } from './ConversationsWidget'

export type ShapeNodeData = {
  shape: MapShape
  live: boolean
  /** Commit edited text (text/sticky shapes, and captions on geometric shapes). */
  onEditText?: (text: string) => void
  /** Persist new geometry after a resize. */
  onResizeEnd?: (rect: { x: number; y: number; width: number; height: number }) => void
}

const GEOMETRIC = new Set(['rectangle', 'circle', 'diamond', 'hexagon'])

/* Standalone canvas annotation: a built-in icon, an image, rich text / sticky
 * note, or a basic geometric shape. Sized by the RF node's width/height (the
 * NodeResizer persists changes); positioned by its top-left like a box.
 * Geometric shapes can carry a caption (double-click to edit) so they work as
 * zone/group boxes the way draw.io shapes do. */
function ShapeNodeImpl({ data, selected }: NodeProps) {
  const { shape, live, onEditText, onResizeEnd } = data as ShapeNodeData
  const { connectMode } = useMapMode()
  const m = shape.metadata || {}
  const locked = !!m.locked
  const editable = !live && !!onEditText && !locked
  const textRef = useRef<HTMLDivElement>(null)
  const [editing, setEditing] = useState(false)

  // Keep the DOM text in sync when not actively editing.
  useEffect(() => {
    if (textRef.current && !editing) textRef.current.textContent = shape.text || ''
  }, [shape.text, editing])

  const beginEdit = () => {
    if (!editable) return
    setEditing(true)
    requestAnimationFrame(() => {
      const el = textRef.current
      if (!el) return
      el.focus()
      // Put the caret at the end instead of selecting nothing.
      const range = document.createRange()
      range.selectNodeContents(el)
      range.collapse(false)
      const sel = window.getSelection()
      sel?.removeAllRanges()
      sel?.addRange(range)
    })
  }
  const commitText = () => {
    if (!editing) return
    setEditing(false)
    if (textRef.current && onEditText) onEditText(textRef.current.textContent || '')
  }
  const onTextKey = (e: React.KeyboardEvent) => {
    if (e.key === 'Escape') { e.preventDefault(); textRef.current?.blur() }
  }

  const isTextKind = shape.kind === 'text' || shape.kind === 'sticky'
  const textStyle: React.CSSProperties = {
    color: m.color || (shape.kind === 'sticky' ? '#1f2937' : 'rgb(var(--text))'),
    fontFamily: m.fontFamily || 'inherit',
    fontSize: m.fontSize ? `${m.fontSize}px` : isTextKind ? '16px' : '13px',
    fontWeight: m.bold ? 700 : isTextKind ? 400 : 600,
    fontStyle: m.italic ? 'italic' : 'normal',
    textAlign: m.align || (shape.kind === 'sticky' ? 'left' : 'center'),
    lineHeight: 1.25,
  }

  const Resizer = !live && !locked && (
    <NodeResizer
      isVisible={!!selected}
      minWidth={24}
      minHeight={20}
      lineClassName="!border-primary/60"
      handleClassName="!h-2 !w-2 !rounded-sm !border !border-surface !bg-primary"
      keepAspectRatio={shape.kind === 'image' && !!m.src}
      onResizeEnd={(_e, p) => onResizeEnd?.({ x: p.x, y: p.y, width: p.width, height: p.height })}
    />
  )

  const fill = shape.fill || 'rgba(59,130,246,0.12)'
  const stroke = shape.stroke || 'rgb(var(--primary))'
  const strokeW = typeof m.strokeWidth === 'number' ? m.strokeWidth : 2
  const dashCss = m.dash === 'dashed' ? 'dashed' : m.dash === 'dotted' ? 'dotted' : 'solid'
  const common = { background: fill, border: `${strokeW}px ${dashCss} ${stroke}` } as React.CSSProperties
  const rotation = typeof m.rotation === 'number' ? m.rotation : 0
  const opacity = typeof m.opacity === 'number' ? Math.max(0.05, Math.min(1, m.opacity)) : 1

  // Editable text layer, shared by text/sticky shapes and geometric captions.
  const textLayer = (extra?: React.CSSProperties) => (
    <div
      ref={textRef}
      className={cn('select-text whitespace-pre-wrap break-words outline-none', editing ? 'nodrag cursor-text' : 'pointer-events-none')}
      style={{
        ...textStyle,
        display: 'flex',
        alignItems: shape.kind === 'sticky' ? 'flex-start' : 'center',
        justifyContent: textStyle.textAlign === 'right' ? 'flex-end' : textStyle.textAlign === 'left' ? 'flex-start' : 'center',
        ...extra,
      }}
      contentEditable={editing}
      suppressContentEditableWarning
      onPointerDown={(e) => { if (editing) e.stopPropagation() }}
      onKeyDown={onTextKey}
      onBlur={commitText}
    />
  )
  const placeholder = editable && !editing && !(shape.text || '').trim()

  let content: React.ReactNode
  if (m.widget === 'conversations') {
    content = (
      <div className={cn('h-full w-full', selected && 'rounded-lg ring-2 ring-primary/70')}>
        <ConversationsWidget shape={shape} />
      </div>
    )
  } else if (shape.kind === 'image' && m.icon) {
    content = (
      <div
        className={cn('flex h-full w-full items-center justify-center', selected && 'rounded ring-2 ring-primary/70')}
        style={{ color: m.color || 'rgb(var(--text))' }}
      >
        <NetworkIcon name={m.icon} className="h-full w-full" />
      </div>
    )
  } else if (shape.kind === 'image') {
    content = (
      <div className={cn('h-full w-full overflow-hidden rounded', selected && 'ring-2 ring-primary/70')} style={{ background: shape.fill || 'transparent' }}>
        {m.src
          ? <img src={m.src} alt={shape.text || 'image'} className="h-full w-full object-contain" draggable={false} />
          : <div className="flex h-full w-full items-center justify-center text-[10px] text-muted">no image</div>}
      </div>
    )
  } else if (isTextKind) {
    content = (
      <div
        onDoubleClick={beginEdit}
        title={editable ? 'Double-click to edit' : undefined}
        className={cn(
          'relative flex h-full w-full overflow-hidden',
          shape.kind === 'sticky' ? 'items-start rounded-sm p-2 shadow-md' : 'items-center px-1.5 py-1',
          selected && 'ring-2 ring-primary/70',
          // An empty text box would be invisible and impossible to find again —
          // keep a faint dashed outline while it's empty in design mode.
          placeholder && !shape.stroke && 'border border-dashed border-border/70',
        )}
        style={{
          background: shape.fill || (shape.kind === 'sticky' ? '#fde68a' : 'transparent'),
          border: shape.stroke ? `${strokeW}px ${dashCss} ${shape.stroke}` : undefined,
          borderRadius: shape.kind === 'sticky' ? 2 : m.rounded === false ? 2 : 10,
        }}
      >
        {textLayer({ height: '100%', width: '100%' })}
        {placeholder && <span className="pointer-events-none absolute inset-0 flex items-center justify-center text-[11px] italic text-muted/70">Double-click to edit</span>}
      </div>
    )
  } else if (shape.kind === 'line' || shape.kind === 'arrow') {
    // Drawn as SVG so the arrowhead follows stroke colour/width and dash.
    const dashArr = LINK_DASH_PATTERN[m.dash || 'solid']
    content = (
      <svg className={cn('h-full w-full overflow-visible', selected && 'rounded ring-2 ring-primary/70')} viewBox="0 0 100 100" preserveAspectRatio="none">
        <defs>
          <marker id={`arr-${shape.id}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth={Math.max(6, strokeW * 3.5)} markerHeight={Math.max(6, strokeW * 3.5)} orient="auto" markerUnits="userSpaceOnUse">
            <path d="M 0 0 L 10 5 L 0 10 z" fill={stroke} />
          </marker>
        </defs>
        <line
          x1="0" y1="50" x2="100" y2="50"
          stroke={stroke} strokeWidth={strokeW} strokeDasharray={dashArr} strokeLinecap="round"
          vectorEffect="non-scaling-stroke"
          markerEnd={shape.kind === 'arrow' ? `url(#arr-${shape.id})` : undefined}
        />
      </svg>
    )
  } else {
    content = (
      <div className={cn('relative h-full w-full', selected && 'ring-2 ring-primary/70 ring-offset-1 ring-offset-bg')} onDoubleClick={beginEdit} title={editable ? 'Double-click to add a caption' : undefined}>
        {shape.kind === 'circle' && <div className="h-full w-full rounded-full" style={common} />}
        {shape.kind === 'rectangle' && <div className="h-full w-full" style={{ ...common, borderRadius: m.rounded ? 10 : 2 }} />}
        {shape.kind === 'diamond' && <div className="h-full w-full" style={{ ...common, clipPath: 'polygon(50% 0,100% 50%,50% 100%,0 50%)' }} />}
        {shape.kind === 'hexagon' && <div className="h-full w-full" style={{ ...common, clipPath: 'polygon(25% 0,75% 0,100% 50%,75% 100%,25% 100%,0 50%)' }} />}
        {GEOMETRIC.has(shape.kind) && (shape.text || editing) && (
          // Left-aligned captions sit in the top-left corner like a zone/group
          // title; centred ones float in the middle of the shape.
          <div className={cn('pointer-events-none absolute inset-0 flex p-2', m.align === 'left' ? 'items-start justify-start px-3 pt-2' : m.align === 'right' ? 'items-start justify-end px-3 pt-2' : 'items-center justify-center')}>
            {textLayer({ pointerEvents: editing ? 'auto' : 'none', maxWidth: '100%' })}
          </div>
        )}
      </div>
    )
  }

  // In connect mode a full-cover handle lets a cable be dragged to/from this
  // annotation, exactly like a device.
  const handleStyle = connectMode
    ? { left: 0, top: 0, width: '100%', height: '100%', transform: 'none', borderRadius: 6, background: 'transparent', border: 'none', opacity: 0, cursor: 'crosshair', zIndex: 5 } as const
    : { width: 1, height: 1, minWidth: 1, minHeight: 1, background: 'transparent', border: 'none', opacity: 0 } as const
  const quickHandleVisible = !live && !connectMode && !locked && m.widget !== 'conversations'

  return (
    <div className="group relative h-full w-full" style={{ opacity }}>
      {Resizer}
      {/* Handles render in BOTH modes — live annotation cables (device ↔ shape)
          need them to anchor; isConnectable still gates interaction. */}
      <Handle type="target" position={Position.Top} id="c" style={handleStyle} isConnectable={!live} />
      <Handle type="source" position={Position.Top} id="c" style={handleStyle} isConnectable={!live} />
      {quickHandleVisible && (
        <Handle
          type="source"
          position={Position.Right}
          id="q"
          title="Drag to connect"
          className="nm-quick-handle"
          style={{ right: -9, top: '50%', width: 16, height: 16, borderRadius: '50%', transform: 'translate(50%, -50%)', background: 'rgb(var(--primary))', border: '2px solid rgb(var(--surface))', cursor: 'crosshair', zIndex: 6, opacity: 0 }}
          isConnectable
        />
      )}
      {connectMode && !live && (
        <span aria-hidden className="pointer-events-none absolute inset-0 rounded-md ring-2 ring-primary/0 transition group-hover:ring-primary/70" />
      )}
      <div className="h-full w-full" style={rotation ? { transform: `rotate(${rotation}deg)` } : undefined}>
        {content}
      </div>
      {locked && !live && (
        <span className="pointer-events-none absolute -right-2 -top-2 flex h-4 w-4 items-center justify-center rounded-full border border-surface bg-surface2 text-muted shadow" title="Locked">
          <Lock className="h-2.5 w-2.5" />
        </span>
      )}
    </div>
  )
}

export const ShapeNode = memo(ShapeNodeImpl)
