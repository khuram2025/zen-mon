import { Fragment, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { ChevronRight, Cable, Router } from 'lucide-react'
import { formatBytes, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import type { NetflowInterfaceRef, Conversation, SortKey } from '../types'
import { normalizeInterfaceRefs, interfaceLabel } from '../model'

// ─────────────────────────────────────────────────────────────────
// Recent flow records (table)
// ─────────────────────────────────────────────────────────────────

function ConversationTable({ conversations, lastSeen, label }: { conversations: Conversation[]; lastSeen?: string | null; label?: string }) {
  const [sortKey, setSortKey] = useState<SortKey>('bytes')
  const [sortDir, setSortDir] = useState<'asc' | 'desc'>('desc')
  const [expanded, setExpanded] = useState<string | null>(null)

  const sorted = useMemo(() => {
    const out = [...conversations]
    out.sort((a, b) => {
      const av = a[sortKey] as string | number
      const bv = b[sortKey] as string | number
      const cmp = typeof av === 'number' && typeof bv === 'number' ? av - bv : String(av).localeCompare(String(bv))
      return sortDir === 'asc' ? cmp : -cmp
    })
    return out
  }, [conversations, sortKey, sortDir])

  const toggle = (k: SortKey) => {
    if (k === sortKey) setSortDir((d) => (d === 'asc' ? 'desc' : 'asc'))
    else { setSortKey(k); setSortDir(k === 'src' || k === 'dst' || k === 'service' || k === 'protocol_name' ? 'asc' : 'desc') }
  }
  const arrow = (k: SortKey) => (sortKey === k ? (sortDir === 'asc' ? ' ↑' : ' ↓') : '')

  return (
    <Card>
      <CardHeader className="flex flex-row items-center justify-between">
        <div>
          <CardTitle>Recent Flow Records</CardTitle>
          <p className="text-xs text-muted">Highest-volume endpoint pairs in {label || 'selected range'}</p>
        </div>
        <div className="text-xs text-muted">Last flow {relativeTime(lastSeen)}</div>
      </CardHeader>
      <CardContent>
        <Table>
          <THead>
            <Tr>
              <Th className="w-6" />
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('src')}>Source{arrow('src')}</button></Th>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('dst')}>Destination{arrow('dst')}</button></Th>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('service')}>Service{arrow('service')}</button></Th>
              <Th><button className="flex items-center gap-1 hover:text-text" onClick={() => toggle('protocol_name')}>Protocol{arrow('protocol_name')}</button></Th>
              <Th className="text-right"><button className="ml-auto flex items-center gap-1 hover:text-text" onClick={() => toggle('bytes')}>Bytes{arrow('bytes')}</button></Th>
              <Th className="text-right"><button className="ml-auto flex items-center gap-1 hover:text-text" onClick={() => toggle('packets')}>Packets{arrow('packets')}</button></Th>
              <Th className="text-right">Flows</Th>
            </Tr>
          </THead>
          <TBody>
            {sorted.length === 0 && (
              <Tr><Td colSpan={8} className="py-8 text-center text-xs text-muted">No flow conversations match.</Td></Tr>
            )}
            {sorted.map((c) => {
              const key = `${c.src}-${c.dst}-${c.protocol_name}-${c.dst_port}`
              const open = expanded === key
              return (
                <Fragment key={key}>
                  <Tr
                    className="cursor-pointer"
                    onClick={() => setExpanded(open ? null : key)}
                    title={open ? 'Collapse' : 'Expand to see where this flow was recorded'}
                  >
                    <Td className="w-6 pr-0"><ChevronRight className={`h-3.5 w-3.5 text-muted transition-transform ${open ? 'rotate-90' : ''}`} /></Td>
                    <Td className="font-mono text-xs">{c.src}</Td>
                    <Td className="font-mono text-xs">{c.dst}</Td>
                    <Td>{c.service}</Td>
                    <Td><Badge variant="outline">{c.protocol_name}</Badge></Td>
                    <Td className="text-right text-sm">{formatBytes(c.bytes)}</Td>
                    <Td className="text-right text-sm">{c.packets.toLocaleString()}</Td>
                    <Td className="text-right text-sm">{c.flows.toLocaleString()}</Td>
                  </Tr>
                  {open && (
                    <Tr>
                      <Td colSpan={8} className="bg-surface2/20 py-3">
                        <ConversationRecordedOn conversation={c} />
                      </Td>
                    </Tr>
                  )}
                </Fragment>
              )
            })}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function ConversationRecordedOn({ conversation: c }: { conversation: Conversation }) {
  const exporters = c.exporters || []
  const inputs = normalizeInterfaceRefs(c.input_interfaces, c.input_snmp)
  const outputs = normalizeInterfaceRefs(c.output_interfaces, c.output_snmp)
  const multiExporter = exporters.length > 1
  const chip = (iface: NetflowInterfaceRef) => (
    <span key={`${iface.exporter_ip}-${iface.ifindex}`} className="inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-1 text-[11px]">
      <Cable className="h-3 w-3 text-muted" />
      <span className="font-medium">{interfaceLabel(iface)}</span>
      {iface.if_alias && iface.if_alias !== interfaceLabel(iface) && <span className="text-muted">({iface.if_alias})</span>}
      {(multiExporter || !iface.if_name) && iface.exporter_ip && (
        <span className="font-mono text-muted">@ {iface.device_hostname || iface.exporter_ip}</span>
      )}
      <span className="font-mono text-muted">#{iface.ifindex}</span>
    </span>
  )
  return (
    <div className="space-y-2 px-1">
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="w-24 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-muted">Recorded by</span>
        {exporters.length ? (
          exporters.map((e) => (
            <Link
              key={e.ip}
              to={`/netflow/devices/${encodeURIComponent(e.ip)}`}
              onClick={(ev) => ev.stopPropagation()}
              className="inline-flex items-center gap-1 rounded border border-border bg-surface px-2 py-1 text-[11px] transition-colors hover:border-primary/60 hover:bg-primary/5"
            >
              <Router className="h-3 w-3 text-muted" />
              <span className="font-medium">{e.hostname || e.ip}</span>
              {e.hostname && <span className="font-mono text-muted">{e.ip}</span>}
            </Link>
          ))
        ) : (
          <span className="text-xs text-muted">No exporter recorded</span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="w-24 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-muted">In on port</span>
        {inputs.length ? inputs.map(chip) : <span className="text-xs text-muted">Not reported</span>}
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="w-24 shrink-0 text-[10px] font-semibold uppercase tracking-wider text-muted">Out on port</span>
        {outputs.length ? outputs.map(chip) : <span className="text-xs text-muted">Not reported</span>}
      </div>
      <div className="flex flex-wrap gap-x-4 gap-y-1 pt-1 text-[11px] text-muted">
        {c.first_seen && <span>First seen {relativeTime(c.first_seen)}</span>}
        {c.last_seen && <span>Last seen {relativeTime(c.last_seen)}</span>}
        {c.src_ports && c.src_ports.length > 0 && (
          <span>
            Source ports: <span className="font-mono">{c.src_ports.slice(0, 6).join(', ')}{c.src_ports.length > 6 ? '…' : ''}</span>
          </span>
        )}
      </div>
    </div>
  )
}

export { ConversationTable }
