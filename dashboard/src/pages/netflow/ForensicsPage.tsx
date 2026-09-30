import { useEffect, useMemo, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { ArrowLeft, ChevronRight, Download, Loader2, Network, Search, Save } from 'lucide-react'
import { api } from '@/lib/api'
import { formatBytes, relativeTime } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'

type FlowRow = {
  timestamp: string
  exporter_ip: string
  src: string
  dst: string
  src_port: number
  dst_port: number
  protocol: number
  protocol_name: string
  tcp_flags: number
  dscp: number
  input_snmp: number
  output_snmp: number
  input_interface?: InterfaceRef | null
  output_interface?: InterfaceRef | null
  packets: number
  bytes: number
  duration_ms: number
  service: string
}

type InterfaceRef = {
  ifindex: number
  display_name?: string | null
  if_name?: string | null
  if_descr?: string | null
  if_alias?: string | null
}

const PROTO_OPTIONS = [
  { value: '', label: 'Any' },
  { value: '6', label: 'TCP (6)' },
  { value: '17', label: 'UDP (17)' },
  { value: '1', label: 'ICMP (1)' },
  { value: '47', label: 'GRE (47)' },
  { value: '50', label: 'ESP (50)' },
]

export function NetflowForensicsPage() {
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [params] = useSearchParams()
  const [src, setSrc] = useState(params.get('src') || '')
  const [dst, setDst] = useState(params.get('dst') || '')
  const [proto, setProto] = useState(params.get('proto') || '')
  const [srcPort, setSrcPort] = useState(params.get('src_port') || '')
  const [dstPort, setDstPort] = useState(params.get('dst_port') || '')
  const [minBytes, setMinBytes] = useState(params.get('min_bytes') || '')
  const [dscp, setDscp] = useState(params.get('dscp') || '')
  const [exporter, setExporter] = useState(params.get('exporter') || '')
  const [sort, setSort] = useState<'timestamp' | 'bytes' | 'packets' | 'src_port' | 'dst_port'>('timestamp')
  const [order, setOrder] = useState<'asc' | 'desc'>('desc')
  const seeded = !!(params.get('src') || params.get('dst') || params.get('proto') || params.get('exporter'))
  const [armed, setArmed] = useState(seeded)
  // If we navigated in with URL params, automatically run on mount.
  useEffect(() => { if (seeded) setArmed(true) }, [seeded])

  const qs = useMemo(() => {
    const p = new URLSearchParams({ hours: String(range.hours), limit: '500', sort, order })
    if (isCustom) { p.set('from', range.fromISO); p.set('to', range.toISO) }
    if (src.trim()) p.set('src', src.trim())
    if (dst.trim()) p.set('dst', dst.trim())
    if (proto) p.set('proto', proto)
    if (srcPort) p.set('src_port', srcPort)
    if (dstPort) p.set('dst_port', dstPort)
    if (minBytes) p.set('min_bytes', minBytes)
    if (dscp) p.set('dscp', dscp)
    if (exporter.trim()) p.set('exporter', exporter.trim())
    return p.toString()
  }, [range.hours, range.fromISO, range.toISO, isCustom, src, dst, proto, srcPort, dstPort, minBytes, dscp, exporter, sort, order])

  const flows = useQuery<FlowRow[]>({
    queryKey: ['netflow', 'forensics', qs, armed],
    queryFn: async () => (await api.get(`/netflow/forensics?${qs}`)).data,
    enabled: armed,
    refetchInterval: false,
  })

  const exportCsv = () => {
    const rows = flows.data || []
    if (rows.length === 0) return
    const header = ['timestamp', 'exporter_ip', 'src', 'src_port', 'dst', 'dst_port', 'protocol', 'service', 'tcp_flags', 'dscp', 'input_interface', 'output_interface', 'input_snmp', 'output_snmp', 'bytes', 'packets', 'duration_ms']
    const esc = (v: any) => `"${String(v ?? '').replace(/"/g, '""')}"`
    const csv = [header.join(','), ...rows.map((r) => header.map((h) => {
      if (h === 'input_interface') return esc(interfaceLabel(r.input_interface, r.input_snmp))
      if (h === 'output_interface') return esc(interfaceLabel(r.output_interface, r.output_snmp))
      return esc((r as any)[h])
    }).join(','))].join('\n')
    const blob = new Blob([csv], { type: 'text/csv' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `netflow-forensics-${new Date().toISOString().slice(0, 19)}.csv`
    a.click()
    URL.revokeObjectURL(url)
  }

  const saveView = async () => {
    const name = window.prompt('Save this query as…')
    if (!name) return
    await api.post('/netflow/saved-views', {
      name,
      query: { src, dst, proto, src_port: srcPort, dst_port: dstPort, min_bytes: minBytes, dscp, exporter, sort, order },
      pinned: false,
    })
    alert('Saved.')
  }

  const reset = () => {
    setSrc(''); setDst(''); setProto(''); setSrcPort(''); setDstPort(''); setMinBytes(''); setDscp(''); setExporter(''); setArmed(false)
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
        <div>
          <div className="mb-1 flex items-center gap-1.5 text-[11px] text-muted">
            <Link to="/netflow" className="inline-flex items-center gap-1 hover:text-text">
              <ArrowLeft className="h-3 w-3" />
              NetFlow
            </Link>
            <ChevronRight className="h-3 w-3" />
            <span>Forensics</span>
          </div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Search className="h-5 w-5 text-primary" />
            Flow Forensics
          </h1>
          <p className="text-xs text-muted">Search un-aggregated flow records by any field, sort, and export to CSV.</p>
        </div>
        <TimeRangePicker
          rangeIdx={rangeIdx}
          isCustom={isCustom}
          customFrom={range.fromISO}
          customTo={range.toISO}
          onPreset={setPreset}
          onCustom={setCustom}
        />
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Filters</CardTitle>
          <p className="text-[11px] text-muted">Leave a field blank to skip it. Source/Dest accept IPs and CIDRs (e.g. 10.0.0.0/8).</p>
        </CardHeader>
        <CardContent>
          <div className="grid grid-cols-1 gap-3 md:grid-cols-3 xl:grid-cols-4">
            <Field label="Source IP / CIDR" value={src} onChange={setSrc} placeholder="10.0.0.0/8" />
            <Field label="Destination IP / CIDR" value={dst} onChange={setDst} placeholder="8.8.8.8" />
            <SelectField label="Protocol" value={proto} onChange={setProto} options={PROTO_OPTIONS} />
            <Field label="Exporter (collector)" value={exporter} onChange={setExporter} placeholder="192.168.100.102" />
            <Field label="Source port" value={srcPort} onChange={setSrcPort} placeholder="any" inputMode="numeric" />
            <Field label="Destination port" value={dstPort} onChange={setDstPort} placeholder="443" inputMode="numeric" />
            <Field label="DSCP code-point" value={dscp} onChange={setDscp} placeholder="46 = EF" inputMode="numeric" />
            <Field label="Min bytes" value={minBytes} onChange={setMinBytes} placeholder="1048576" inputMode="numeric" />
          </div>
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <button
              onClick={() => setArmed(true)}
              className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-1.5 text-xs font-semibold text-black hover:bg-primary/90"
            >
              <Search className="h-3.5 w-3.5" />
              Search
            </button>
            <button onClick={reset} className="rounded-md border border-border px-3 py-1.5 text-xs text-muted hover:text-text">
              Reset
            </button>
            <button
              onClick={saveView}
              disabled={!armed}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-muted hover:text-text disabled:opacity-50"
            >
              <Save className="h-3.5 w-3.5" />
              Save view
            </button>
            <button
              onClick={exportCsv}
              disabled={!flows.data?.length}
              className="inline-flex items-center gap-1.5 rounded-md border border-border px-3 py-1.5 text-xs text-muted hover:text-text disabled:opacity-50"
            >
              <Download className="h-3.5 w-3.5" />
              Export CSV
            </button>
            <span className="ml-auto text-[11px] text-muted">
              {flows.data ? `${flows.data.length} rows · ${range.label}` : range.label}
            </span>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between pb-3">
          <CardTitle className="text-sm">Results</CardTitle>
          {flows.isFetching && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
        </CardHeader>
        <CardContent>
          {!armed ? (
            <div className="rounded-md border border-dashed border-border bg-surface2/30 p-10 text-center text-xs text-muted">
              Configure filters above and click <span className="text-text">Search</span> to query raw flow records.
            </div>
          ) : flows.error ? (
            <div className="rounded-md border border-rose-500/40 bg-rose-500/10 p-3 text-xs text-rose-300">
              Query failed: {(flows.error as any)?.response?.data?.detail || (flows.error as any)?.message}
            </div>
          ) : (flows.data?.length ?? 0) === 0 ? (
            <div className="rounded-md border border-dashed border-border bg-surface2/30 p-10 text-center text-xs text-muted">
              No flow records matched these filters.
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th><HeaderSort label="When" col="timestamp" sort={sort} order={order} setSort={setSort} setOrder={setOrder} /></Th>
                  <Th>Exporter</Th>
                  <Th>Source</Th>
                  <Th><HeaderSort label="SPort" col="src_port" sort={sort} order={order} setSort={setSort} setOrder={setOrder} /></Th>
                  <Th>Destination</Th>
                  <Th><HeaderSort label="DPort" col="dst_port" sort={sort} order={order} setSort={setSort} setOrder={setOrder} /></Th>
                  <Th>Service</Th>
                  <Th>Proto</Th>
                  <Th>Flags</Th>
                  <Th>DSCP</Th>
                  <Th>If In/Out</Th>
                  <Th className="text-right"><HeaderSort label="Bytes" col="bytes" sort={sort} order={order} setSort={setSort} setOrder={setOrder} /></Th>
                  <Th className="text-right"><HeaderSort label="Pkts" col="packets" sort={sort} order={order} setSort={setSort} setOrder={setOrder} /></Th>
                </Tr>
              </THead>
              <TBody>
                {(flows.data || []).map((r, i) => (
                  <Tr key={`${r.timestamp}-${r.src}-${r.dst}-${r.src_port}-${r.dst_port}-${i}`}>
                    <Td className="text-[11px] text-muted" title={r.timestamp}>{relativeTime(r.timestamp)}</Td>
                    <Td className="font-mono text-[10px]">{r.exporter_ip}</Td>
                    <Td className="font-mono text-[11px]">{r.src}</Td>
                    <Td className="text-[11px]">{r.src_port}</Td>
                    <Td className="font-mono text-[11px]">{r.dst}</Td>
                    <Td className="text-[11px]">{r.dst_port}</Td>
                    <Td className="text-[11px]">{r.service}</Td>
                    <Td><Badge variant="outline">{r.protocol_name}</Badge></Td>
                    <Td className="font-mono text-[10px]">{flagsString(r.tcp_flags)}</Td>
                    <Td className="text-[11px]">{r.dscp}</Td>
                    <Td className="text-[10px]">
                      <div className="font-medium">{interfaceLabel(r.input_interface, r.input_snmp)}</div>
                      <div className="text-muted">{interfaceLabel(r.output_interface, r.output_snmp)}</div>
                    </Td>
                    <Td className="text-right font-mono text-[11px]">{formatBytes(r.bytes)}</Td>
                    <Td className="text-right text-[11px]">{r.packets.toLocaleString()}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function interfaceLabel(iface: InterfaceRef | null | undefined, fallbackIndex: number): string {
  if (!fallbackIndex) return '—'
  return iface?.display_name || iface?.if_name || iface?.if_descr || iface?.if_alias || `ifIndex ${fallbackIndex}`
}

function flagsString(flags: number): string {
  const out: string[] = []
  if (flags & 32) out.push('U')
  if (flags & 16) out.push('A')
  if (flags & 8) out.push('P')
  if (flags & 4) out.push('R')
  if (flags & 2) out.push('S')
  if (flags & 1) out.push('F')
  return out.join('') || '—'
}

function HeaderSort({ label, col, sort, order, setSort, setOrder }: { label: string; col: any; sort: any; order: any; setSort: any; setOrder: any }) {
  const active = sort === col
  return (
    <button
      onClick={() => {
        if (active) setOrder(order === 'asc' ? 'desc' : 'asc')
        else { setSort(col); setOrder('desc') }
      }}
      className="flex items-center gap-1 hover:text-text"
    >
      {label}{active ? (order === 'asc' ? ' ↑' : ' ↓') : ''}
    </button>
  )
}

function Field({ label, value, onChange, placeholder, inputMode }: { label: string; value: string; onChange: (v: string) => void; placeholder?: string; inputMode?: any }) {
  return (
    <label className="block">
      <span className="text-[10px] uppercase tracking-wider text-muted">{label}</span>
      <input
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        inputMode={inputMode}
        className="mt-1 w-full rounded-md border border-border bg-surface2/60 px-2 py-1.5 text-xs text-text placeholder:text-muted focus:border-primary focus:outline-none"
      />
    </label>
  )
}

function SelectField({ label, value, onChange, options }: { label: string; value: string; onChange: (v: string) => void; options: { value: string; label: string }[] }) {
  return (
    <label className="block">
      <span className="text-[10px] uppercase tracking-wider text-muted">{label}</span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="mt-1 w-full rounded-md border border-border bg-surface2/60 px-2 py-1.5 text-xs text-text focus:border-primary focus:outline-none"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
    </label>
  )
}

// Tag the Network icon use so the import isn't pruned.
export const _Network = Network
