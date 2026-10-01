import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { AlertTriangle, ChevronDown, ChevronUp, Search, ShieldCheck } from 'lucide-react'
import { cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'

type Kind = 'hunk' | 'add' | 'del' | 'ctx' | 'meta'
type Row = { kind: Kind; text: string; oldNo: number | null; newNo: number | null }

const HUNK_RE = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/

/** Parse a unified diff (difflib, lineterm='') into numbered rows. */
function parseDiff(diff: string): Row[] {
  const rows: Row[] = []
  let oldNo = 0
  let newNo = 0
  let inHunk = false
  for (const line of diff.replace(/\n+$/, '').split('\n')) {
    if (line.startsWith('@@')) {
      const m = HUNK_RE.exec(line)
      if (m) {
        oldNo = Number(m[1])
        newNo = Number(m[3])
      }
      inHunk = true
      rows.push({ kind: 'hunk', text: line, oldNo: null, newNo: null })
      continue
    }
    // '---' / '+++' file headers only appear before the first hunk.
    if (!inHunk) {
      if (line.startsWith('---') || line.startsWith('+++') || line === '') continue
      rows.push({ kind: 'meta', text: line, oldNo: null, newNo: null })
      continue
    }
    if (line.startsWith('+')) rows.push({ kind: 'add', text: line.slice(1), oldNo: null, newNo: newNo++ })
    else if (line.startsWith('-')) rows.push({ kind: 'del', text: line.slice(1), oldNo: oldNo++, newNo: null })
    else if (line.startsWith('\\')) rows.push({ kind: 'meta', text: line, oldNo: null, newNo: null })
    else rows.push({ kind: 'ctx', text: line.startsWith(' ') ? line.slice(1) : line, oldNo: oldNo++, newNo: newNo++ })
  }
  return rows
}

type Side = { row: number; text: string; no: number | null; kind: Kind } | null
type Pair = { hunk?: number; left: Side; right: Side }

/** Pair removed/added runs row by row for the side-by-side view. */
function pairRows(rows: Row[]): Pair[] {
  const out: Pair[] = []
  let i = 0
  while (i < rows.length) {
    const r = rows[i]
    if (r.kind === 'hunk' || r.kind === 'meta') {
      out.push({ hunk: i, left: null, right: null })
      i++
      continue
    }
    if (r.kind === 'ctx') {
      out.push({ left: { row: i, text: r.text, no: r.oldNo, kind: 'ctx' }, right: { row: i, text: r.text, no: r.newNo, kind: 'ctx' } })
      i++
      continue
    }
    const dels: number[] = []
    const adds: number[] = []
    while (i < rows.length && rows[i].kind === 'del') dels.push(i++)
    while (i < rows.length && rows[i].kind === 'add') adds.push(i++)
    for (let k = 0; k < Math.max(dels.length, adds.length); k++) {
      const d = dels[k] != null ? rows[dels[k]] : null
      const a = adds[k] != null ? rows[adds[k]] : null
      out.push({
        left: d ? { row: dels[k], text: d.text, no: d.oldNo, kind: 'del' } : null,
        right: a ? { row: adds[k], text: a.text, no: a.newNo, kind: 'add' } : null,
      })
    }
  }
  return out
}

function findAll(text: string, q: string): number[] {
  if (!q) return []
  const hay = text.toLowerCase()
  const out: number[] = []
  let at = hay.indexOf(q)
  while (at !== -1) {
    out.push(at)
    at = hay.indexOf(q, at + q.length)
  }
  return out
}

const ROW_BG: Record<Kind, string> = {
  add: 'bg-success/10',
  del: 'bg-danger/10',
  ctx: '',
  hunk: 'bg-info/10 text-info',
  meta: 'bg-surface2 text-muted',
}
const SIGN: Record<Kind, string> = { add: '+', del: '-', ctx: ' ', hunk: '', meta: '' }
const SIGN_CLS: Record<Kind, string> = { add: 'text-success', del: 'text-danger', ctx: 'text-muted', hunk: '', meta: '' }

export function DiffView({ diff, added, removed, identical, sensitiveChanged, redacted, normalized }: {
  diff: string; added?: number; removed?: number; identical?: boolean; sensitiveChanged?: boolean; redacted?: boolean; normalized?: boolean
}) {
  const [view, setView] = useState<'unified' | 'split'>('unified')
  const [query, setQuery] = useState('')
  const [current, setCurrent] = useState(0)
  const scrollRef = useRef<HTMLDivElement>(null)

  const rows = useMemo(() => parseDiff(diff || ''), [diff])
  const pairs = useMemo(() => pairRows(rows), [rows])
  const addCount = added ?? rows.filter((r) => r.kind === 'add').length
  const delCount = removed ?? rows.filter((r) => r.kind === 'del').length

  const q = query.trim().toLowerCase()
  const { offsets, starts, total } = useMemo(() => {
    const offsets: number[][] = []
    const starts: number[] = []
    let total = 0
    for (const r of rows) {
      const found = findAll(r.text, q)
      starts.push(total)
      offsets.push(found)
      total += found.length
    }
    return { offsets, starts, total }
  }, [rows, q])

  useEffect(() => { setCurrent(0) }, [q, diff])

  useEffect(() => {
    if (!total) return
    const el = scrollRef.current?.querySelector(`[data-match="${current}"]`)
    el?.scrollIntoView({ block: 'center', inline: 'nearest' })
  }, [current, total, view, q])

  const step = (d: number) => { if (total) setCurrent((c) => (c + d + total) % total) }

  /** Line text with search matches highlighted. */
  const renderText = (rowIdx: number, text: string): ReactNode => {
    const found = offsets[rowIdx]
    if (!found?.length) return text || ' '
    const parts: ReactNode[] = []
    let last = 0
    found.forEach((at, k) => {
      const g = starts[rowIdx] + k
      if (at > last) parts.push(text.slice(last, at))
      parts.push(
        <mark key={k} data-match={g} className={cn('rounded-sm text-text', g === current ? 'bg-warning/70 ring-1 ring-warning' : 'bg-warning/30')}>
          {text.slice(at, at + q.length)}
        </mark>,
      )
      last = at + q.length
    })
    if (last < text.length) parts.push(text.slice(last))
    return parts
  }

  const noDiff = identical || (!rows.length && !(diff || '').trim())

  return (
    <div className="min-w-0 space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono tabular-nums">
          <span className="text-success">+{addCount}</span>
          <span className="sr-only"> added,</span>{' '}
          <span className="text-danger">−{delCount}</span>
          <span className="sr-only"> removed</span>
        </span>
        {sensitiveChanged && (
          <Badge variant="warning"><AlertTriangle className="h-3 w-3" aria-hidden /> Sensitive lines changed</Badge>
        )}
        {redacted && <Badge variant="outline"><ShieldCheck className="h-3 w-3" aria-hidden /> Redacted view</Badge>}
        {normalized && <Badge variant="info">Normalized (runtime noise ignored)</Badge>}
        <div className="ml-auto flex flex-wrap items-center gap-2">
          <div role="group" aria-label="Diff layout" className="inline-flex rounded-md border border-border p-0.5">
            {(['unified', 'split'] as const).map((v) => (
              <button key={v} type="button" aria-pressed={view === v} onClick={() => setView(v)}
                className={cn('rounded px-2 py-1 text-xs font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
                  view === v ? 'bg-primary text-white' : 'text-muted hover:bg-surface2 hover:text-text')}>
                {v === 'unified' ? 'Unified' : 'Side-by-side'}
              </button>
            ))}
          </div>
          <div className="flex items-center gap-1">
            <div className="relative">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted" aria-hidden />
              <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Find in diff"
                aria-label="Find in diff" className="h-8 w-40 pl-7 text-xs sm:w-48"
                onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); step(e.shiftKey ? -1 : 1) } }} />
            </div>
            {q && (
              <span className="whitespace-nowrap tabular-nums text-muted" aria-live="polite">
                {total ? `${current + 1} of ${total} match${total === 1 ? '' : 'es'}` : '0 matches'}
              </span>
            )}
            <Button variant="outline" size="icon" className="h-8 w-8" aria-label="Previous match" disabled={!total} onClick={() => step(-1)}>
              <ChevronUp className="h-4 w-4" />
            </Button>
            <Button variant="outline" size="icon" className="h-8 w-8" aria-label="Next match" disabled={!total} onClick={() => step(1)}>
              <ChevronDown className="h-4 w-4" />
            </Button>
          </div>
        </div>
      </div>

      <div ref={scrollRef} role="region" aria-label="Configuration differences" tabIndex={0}
        className="max-h-[65vh] overflow-auto rounded-md border border-border bg-surface font-mono text-[12px] leading-5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40">
        {noDiff ? (
          <div className="px-4 py-8 text-center font-sans text-sm text-muted">
            {identical ? 'No differences after excluding known runtime metadata.' : 'Only redacted content differs. Secrets are hidden in this view, so no visible lines changed.'}
          </div>
        ) : view === 'unified' ? (
          <div className="inline-block min-w-full">
            {rows.map((r, i) => (
              <div key={i} className={cn('flex', ROW_BG[r.kind])}>
                <span className="w-12 shrink-0 select-none border-r border-border/60 pr-2 text-right text-muted tabular-nums">{r.oldNo ?? ''}</span>
                <span className="w-12 shrink-0 select-none border-r border-border/60 pr-2 text-right text-muted tabular-nums">{r.newNo ?? ''}</span>
                <span className={cn('w-5 shrink-0 select-none text-center', SIGN_CLS[r.kind])} aria-hidden={r.kind === 'ctx'}>{SIGN[r.kind]}</span>
                <span className={cn('whitespace-pre pr-4', r.kind === 'ctx' && 'text-muted')}>{renderText(i, r.text)}</span>
              </div>
            ))}
          </div>
        ) : (
          <table className="w-full min-w-[720px] table-fixed border-collapse">
            <caption className="sr-only">Before on the left, after on the right</caption>
            <colgroup>
              <col className="w-12" /><col className="w-5" /><col /><col className="w-12" /><col className="w-5" /><col />
            </colgroup>
            <thead className="sticky top-0 z-10 bg-surface2 font-sans text-[11px] uppercase tracking-wider text-muted">
              <tr>
                <th colSpan={3} className="px-2 py-1 text-left font-medium">Before</th>
                <th colSpan={3} className="border-l border-border px-2 py-1 text-left font-medium">After</th>
              </tr>
            </thead>
            <tbody>
              {pairs.map((p, i) => {
                if (p.hunk != null) {
                  const r = rows[p.hunk]
                  return (
                    <tr key={i} className={ROW_BG[r.kind]}>
                      <td colSpan={6} className="whitespace-pre-wrap break-all px-2">{renderText(p.hunk, r.text)}</td>
                    </tr>
                  )
                }
                return (
                  <tr key={i} className="align-top">
                    <SideCells side={p.left} renderText={renderText} />
                    <SideCells side={p.right} renderText={renderText} divider />
                  </tr>
                )
              })}
            </tbody>
          </table>
        )}
      </div>
    </div>
  )
}

function SideCells({ side, renderText, divider }: { side: Side; renderText: (row: number, text: string) => ReactNode; divider?: boolean }) {
  if (!side) {
    return <td colSpan={3} className={cn('bg-surface2/60', divider && 'border-l border-border')} aria-label="No line" />
  }
  return (
    <>
      <td className={cn('select-none pr-2 text-right text-muted tabular-nums', ROW_BG[side.kind], divider && 'border-l border-border')}>{side.no ?? ''}</td>
      <td className={cn('select-none text-center', ROW_BG[side.kind], SIGN_CLS[side.kind])}>{SIGN[side.kind]}</td>
      <td className={cn('whitespace-pre-wrap break-all pr-2', ROW_BG[side.kind], side.kind === 'ctx' && 'text-muted')}>{renderText(side.row, side.text)}</td>
    </>
  )
}
