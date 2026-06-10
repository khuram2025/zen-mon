import { memo, useEffect, useRef, useState } from 'react'
import { Handle, NodeResizer, Position, type NodeProps } from '@xyflow/react'
import { cn } from '@/lib/utils'
import { NetworkIcon } from '@/components/network-icons'
import type { MapShape } from '../core'
import { useMapMode } from './MapModeContext'
import { ConversationsWidget } from './ConversationsWidget'

export type ShapeNodeData = {
  shape: MapShape
  live: boolean
  /** Commit edited text (text/sticky shapes). */
  onEditText?: (text: string) => void
  /** Persist new geometry after a resize. */
  onResizeEnd?: (rect: { x: number; y: number; width: number; height: number }) => void
}

/* Standalone canvas annotation: a built-in icon, an image, rich text / sticky
 * note, or a basic geometric shape. Sized by the RF node's width/height (the
 * NodeResizer persists changes); positioned by its top-left like a box. */
function ShapeNodeImpl({ data, selected }: NodeProps) {
  const { shape, live, onEditText, onResizeEnd } = data as ShapeNodeData
  const { connectMode } = useMapMode()
  const m = shape.metadata || {}
  const editable = !live && !!onEditText
  const textRef = useRef<HTMLDivElement>(null)
  const [editing, setEditing] = useState(false)

  // Keep the DOM text in sync when not actively editing.
  useEffect(() => {
    if (textRef.current && !editing) textRef.current.textContent = shape.text || ''
  }, [shape.text, editing])

  const beginEdit = () => {
    if (!editable) return
    setEditing(true)
    requestAnimationFrame(() => textRef.current?.focus())
  }
  const commitText = () => {
    setEditing(false)
    if (textRef.current && onEditText) onEditText(textRef.current.textContent || '')
  }

  const textStyle: React.CSSProperties = {
    color: m.color || (shape.kind === 'sticky' ? '#1f2937' : 'rgb(var(--text))'),
    fontFamily: m.fontFamily || 'inherit',
    fontSize: m.fontSize ? `${m.fontSize}px` : '16px',
    fontWeight: m.bold ? 700 : 400,
    fontStyle: m.italic ? 'italic' : 'normal',
    textAlign: m.align || (shape.kind === 'sticky' ? 'left' : 'center'),
    lineHeight: 1.25,
  }

  const Resizer = !live && (
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
  const common = { background: fill, border: `2px solid ${stroke}` } as React.CSSProperties

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
  } else if (shape.kind === 'text' || shape.kind === 'sticky') {
    content = (
      <div
        onDoubleClick={beginEdit}
        title={editable ? 'Double-click to edit' : undefined}
        className={cn(
          'flex h-full w-full overflow-hidden',
          shape.kind === 'sticky' ? 'items-start rounded-sm p-2 shadow-md' : 'items-center px-1.5 py-1',
          selected && 'ring-2 ring-primary/70',
        )}
        style={{
          background: shape.fill || (shape.kind === 'sticky' ? '#fde68a' : 'transparent'),
          border: shape.stroke ? `2px solid ${shape.stroke}` : undefined,
          borderRadius: shape.kind === 'sticky' ? 2 : m.rounded === false ? 2 : 10,
        }}
      >
        <div
          ref={textRef}
          className={cn('h-full w-full select-text whitespace-pre-wrap break-words outline-none', editing ? 'nodrag cursor-text' : 'pointer-events-none')}
          style={{ ...textStyle, display: 'flex', alignItems: shape.kind === 'sticky' ? 'flex-start' : 'center', justifyContent: textStyle.textAlign === 'right' ? 'flex-end' : textStyle.textAlign === 'left' ? 'flex-start' : 'center' }}
          contentEditable={editing}
          suppressContentEditableWarning
          onPointerDown={(e) => { if (editing) e.stopPropagation() }}
          onBlur={commitText}
        />
      </div>
    )
  } else {
    content = (
      <div className={cn('relative h-full w-full', selected && 'ring-2 ring-primary/70 ring-offset-1 ring-offset-bg')}>
        {shape.kind === 'circle' && <div className="h-full w-full rounded-full" style={common} />}
        {shape.kind === 'rectangle' && <div className="h-full w-full" style={{ ...common, borderRadius: m.rounded ? 10 : 2 }} />}
        {shape.kind === 'diamond' && <div className="h-full w-full" style={{ ...common, clipPath: 'polygon(50% 0,100% 50%,50% 100%,0 50%)' }} />}
        {shape.kind === 'hexagon' && <div className="h-full w-full" style={{ ...common, clipPath: 'polygon(25% 0,75% 0,100% 50%,75% 100%,25% 100%,0 50%)' }} />}
        {(shape.kind === 'line' || shape.kind === 'arrow') && (
          <div className="flex h-full w-full items-center">
            <div className="h-0 w-full" style={{ borderTop: `2px solid ${stroke}` }} />
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

  return (
    <>
      {Resizer}
      {/* Handles render in BOTH modes — live annotation cables (device ↔ shape)
          need them to anchor; isConnectable still gates interaction. */}
      <Handle type="target" position={Position.Top} id="c" style={handleStyle} isConnectable={connectMode && !live} />
      <Handle type="source" position={Position.Top} id="c" style={handleStyle} isConnectable={connectMode && !live} />
      {connectMode && !live && (
        <span aria-hidden className="pointer-events-none absolute inset-0 rounded-md ring-2 ring-primary/0 transition group-hover:ring-primary/70" />
      )}
      {content}
    </>
  )
}

export const ShapeNode = memo(ShapeNodeImpl)
