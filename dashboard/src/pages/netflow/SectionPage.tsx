import { Fragment, useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  ArrowDownUp,
  ArrowLeft,
  ChevronRight,
  Database,
  Layers,
  Loader2,
  RadioTower,
  Smartphone,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, formatBytes } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { TimeRangePicker, useTimeRange } from '@/components/TimeRangePicker'
import { pollMs } from './model'
import { EmptyState } from './components/EmptyState'
import { IpTrafficDetailPanel } from './details/IpTrafficDetailPanel'
import { ApplicationDetailPanel } from './details/ApplicationDetailPanel'
import { ProtocolDetailPanel } from './details/ProtocolDetailPanel'
import { ConversationDetailPanel } from './details/ConversationDetailPanel'

const NETFLOW_SECTION_META: Record<string, { title: string; description: string; endpoint: string; icon: React.ComponentType<{ className?: string }> }> = {
  'top-talkers': {
    title: 'Top Talkers',
    description: 'Highest-volume IP addresses by total bytes in the selected window.',
    endpoint: 'top-talkers',
    icon: RadioTower,
  },
  conversations: {
    title: 'Conversations',
    description: 'Highest-volume source to destination pairs with protocol and service context.',
    endpoint: 'top-conversations',
    icon: ArrowDownUp,
  },
  'top-endpoints': {
    title: 'Top Endpoints',
    description: 'Endpoint IPs ranked by total source and destination traffic contribution.',
    endpoint: 'top-endpoints',
    icon: Smartphone,
  },
  applications: {
    title: 'Top Applications',
    description: 'Application buckets inferred from destination service ports.',
    endpoint: 'applications',
    icon: Layers,
  },
  protocols: {
    title: 'Protocol Distribution',
    description: 'Traffic split by IP protocol.',
    endpoint: 'protocols',
    icon: Database,
  },
}

export function NetflowSectionPage() {
  const { section = 'top-talkers' } = useParams<{ section: string }>()
  const meta = NETFLOW_SECTION_META[section] || NETFLOW_SECTION_META['top-talkers']
  const { range, rangeIdx, isCustom, setPreset, setCustom } = useTimeRange()
  const [searchParams] = useSearchParams()

  const qs = useMemo(() => {
    const params = new URLSearchParams({ hours: String(range.hours), limit: '50' })
    if (isCustom) {
      params.set('from', range.fromISO)
      params.set('to', range.toISO)
    }
    for (const key of ['exporter', 'iface', 'talker', 'protocol', 'dscp', 'app', 'netclass', 'tcpflag', 'hour', 'dow']) {
      const value = searchParams.get(key)
      if (value) params.set(key, value)
    }
    return params.toString()
  }, [range.hours, range.fromISO, range.toISO, isCustom, searchParams])

  const { data = [], isLoading, error } = useQuery<any[]>({
    queryKey: ['netflow', 'section', section, qs],
    queryFn: async () => (await api.get(`/netflow/${meta.endpoint}?${qs}`)).data,
    refetchInterval: isCustom ? false : pollMs(range.hours),
  })

  const Icon = meta.icon

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
            <span>{meta.title}</span>
          </div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <Icon className="h-5 w-5 text-primary" />
            {meta.title}
          </h1>
          <p className="text-xs text-muted">{meta.description}</p>
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
        <CardHeader className="flex flex-row items-center justify-between">
          <div>
            <CardTitle>{meta.title}</CardTitle>
            <p className="text-xs text-muted">{range.label} · {data.length.toLocaleString()} rows</p>
          </div>
          {isLoading && <Loader2 className="h-4 w-4 animate-spin text-muted" />}
        </CardHeader>
        <CardContent>
          {error ? (
            <div className="rounded-md border border-danger/40 bg-danger/10 p-4 text-sm text-danger">
              Failed to load {meta.title.toLowerCase()}: {apiErrorMessage(error)}
            </div>
          ) : (
            <NetflowSectionTable section={section} rows={data} searchParams={searchParams} />
          )}
        </CardContent>
      </Card>
    </div>
  )
}

function NetflowSectionTable({ section, rows, searchParams }: { section: string; rows: any[]; searchParams: URLSearchParams }) {
  const [expandedRow, setExpandedRow] = useState<string | null>(null)
  const withParams = (path: string, updates: Record<string, string | null>) => {
    const params = new URLSearchParams(searchParams)
    if (!params.get('range')) params.set('range', '1h')
    for (const [key, value] of Object.entries(updates)) {
      if (value == null) params.delete(key)
      else params.set(key, value)
    }
    return `${path}?${params.toString()}`
  }

  if (rows.length === 0) {
    return <EmptyState />
  }

  if (section === 'conversations') {
    return (
      <Table>
        <THead>
          <Tr>
            <Th>Source</Th>
            <Th>Destination</Th>
            <Th>Application</Th>
            <Th>Protocol</Th>
            <Th className="text-right">Bytes</Th>
            <Th className="text-right">Packets</Th>
            <Th className="text-right">Flows</Th>
            <Th></Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((row) => {
            const rowId = `${row.src}-${row.dst}-${row.protocol}-${row.dst_port}`
            const expanded = expandedRow === rowId
            return (
              <Fragment key={rowId}>
                <Tr className={expanded ? 'bg-primary/5' : undefined}>
                  <Td className="font-mono text-xs">{row.src}</Td>
                  <Td className="font-mono text-xs">{row.dst}</Td>
                  <Td>{row.service}</Td>
                  <Td><Badge variant="outline">{row.protocol_name}</Badge></Td>
                  <Td className="text-right">{formatBytes(row.bytes)}</Td>
                  <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                  <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                  <Td className="text-right">
                    <button
                      type="button"
                      className="text-xs font-medium text-primary hover:underline"
                      onClick={() => setExpandedRow(expanded ? null : rowId)}
                    >
                      {expanded ? 'Hide' : 'Details'}
                    </button>
                  </Td>
                </Tr>
                {expanded && (
                  <Tr className="hover:bg-transparent">
                    <Td colSpan={8} className="bg-surface2/20 p-4">
                      <ConversationDetailPanel
                        row={row}
                        forensicsTo={withParams('/netflow/forensics', { src: row.src, dst: row.dst, proto: String(row.protocol), dst_port: String(row.dst_port) })}
                      />
                    </Td>
                  </Tr>
                )}
              </Fragment>
            )
          })}
        </TBody>
      </Table>
    )
  }

  if (section === 'applications') {
    return (
      <Table>
        <THead>
          <Tr>
            <Th>Application</Th>
            <Th className="text-right">Bytes</Th>
            <Th className="text-right">Packets</Th>
            <Th className="text-right">Flows</Th>
            <Th></Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((row) => {
            const rowId = `app-${row.name}`
            const expanded = expandedRow === rowId
            return (
              <Fragment key={rowId}>
                <Tr className={expanded ? 'bg-primary/5' : undefined}>
                  <Td className="font-medium">{row.name}</Td>
                  <Td className="text-right">{formatBytes(row.bytes)}</Td>
                  <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                  <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                  <Td className="text-right">
                    <button type="button" className="text-xs font-medium text-primary hover:underline" onClick={() => setExpandedRow(expanded ? null : rowId)}>
                      {expanded ? 'Hide' : 'Details'}
                    </button>
                  </Td>
                </Tr>
                {expanded && (
                  <Tr className="hover:bg-transparent">
                    <Td colSpan={5} className="bg-surface2/20 p-4">
                      <ApplicationDetailPanel row={row} filterTo={withParams('/netflow', { app: row.name })} />
                    </Td>
                  </Tr>
                )}
              </Fragment>
            )
          })}
        </TBody>
      </Table>
    )
  }

  if (section === 'protocols') {
    return (
      <Table>
        <THead>
          <Tr>
            <Th>Protocol</Th>
            <Th>Number</Th>
            <Th className="text-right">Bytes</Th>
            <Th className="text-right">Packets</Th>
            <Th className="text-right">Flows</Th>
            <Th></Th>
          </Tr>
        </THead>
        <TBody>
          {rows.map((row) => {
            const rowId = `proto-${row.protocol}`
            const expanded = expandedRow === rowId
            return (
              <Fragment key={rowId}>
                <Tr className={expanded ? 'bg-primary/5' : undefined}>
                  <Td className="font-medium">{row.name}</Td>
                  <Td>{row.protocol}</Td>
                  <Td className="text-right">{formatBytes(row.bytes)}</Td>
                  <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                  <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                  <Td className="text-right">
                    <button type="button" className="text-xs font-medium text-primary hover:underline" onClick={() => setExpandedRow(expanded ? null : rowId)}>
                      {expanded ? 'Hide' : 'Details'}
                    </button>
                  </Td>
                </Tr>
                {expanded && (
                  <Tr className="hover:bg-transparent">
                    <Td colSpan={6} className="bg-surface2/20 p-4">
                      <ProtocolDetailPanel row={row} filterTo={withParams('/netflow', { protocol: String(row.protocol) })} />
                    </Td>
                  </Tr>
                )}
              </Fragment>
            )
          })}
        </TBody>
      </Table>
    )
  }

  const isEndpoints = section === 'top-endpoints'
  return (
    <Table>
      <THead>
        <Tr>
          <Th>IP Address</Th>
          <Th className="text-right">Bytes</Th>
          <Th className="text-right">Packets</Th>
          <Th className="text-right">Flows</Th>
          {isEndpoints && <Th className="text-right">As Source</Th>}
          {isEndpoints && <Th className="text-right">As Destination</Th>}
          <Th></Th>
        </Tr>
      </THead>
      <TBody>
        {rows.map((row) => {
          const rowId = `ip-${row.ip}`
          const expanded = expandedRow === rowId
          const colSpan = isEndpoints ? 7 : 5
          return (
            <Fragment key={rowId}>
              <Tr className={expanded ? 'bg-primary/5' : undefined}>
                <Td className="font-mono text-xs">{row.ip}</Td>
                <Td className="text-right">{formatBytes(row.bytes)}</Td>
                <Td className="text-right">{Number(row.packets || 0).toLocaleString()}</Td>
                <Td className="text-right">{Number(row.flows || 0).toLocaleString()}</Td>
                {isEndpoints && <Td className="text-right">{formatBytes(row.src_bytes || 0)} · {Number(row.src_flows || 0).toLocaleString()}</Td>}
                {isEndpoints && <Td className="text-right">{formatBytes(row.dst_bytes || 0)} · {Number(row.dst_flows || 0).toLocaleString()}</Td>}
                <Td className="text-right">
                  <button type="button" className="text-xs font-medium text-primary hover:underline" onClick={() => setExpandedRow(expanded ? null : rowId)}>
                    {expanded ? 'Hide' : 'Details'}
                  </button>
                </Td>
              </Tr>
              {expanded && (
                <Tr className="hover:bg-transparent">
                  <Td colSpan={colSpan} className="bg-surface2/20 p-4">
                    <IpTrafficDetailPanel
                      row={row}
                      mode={isEndpoints ? 'endpoint' : 'talker'}
                      filterTo={withParams('/netflow', { talker: row.ip })}
                      forensicsTo={withParams('/netflow/forensics', { talker: row.ip })}
                    />
                  </Td>
                </Tr>
              )}
            </Fragment>
          )
        })}
      </TBody>
    </Table>
  )
}
