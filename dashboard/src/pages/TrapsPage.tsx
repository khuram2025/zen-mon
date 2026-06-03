import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Inbox, RefreshCw, Search } from 'lucide-react'
import { api } from '@/lib/api'
import { relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'

const RANGES = [
  { label: '1h', hours: 1 },
  { label: '24h', hours: 24 },
  { label: '7d', hours: 168 },
  { label: '30d', hours: 720 },
]
const SEVERITIES = ['all', 'critical', 'warning', 'info'] as const

function sevVariant(s: string): 'danger' | 'warning' | 'info' {
  return s === 'critical' ? 'danger' : s === 'warning' ? 'warning' : 'info'
}

// Friendly names for the well-known generic traps (mirrors the poller's
// severity mapping); anything else shows the raw OID.
const WELL_KNOWN: Record<string, string> = {
  '1.3.6.1.6.3.1.1.5.1': 'coldStart',
  '1.3.6.1.6.3.1.1.5.2': 'warmStart',
  '1.3.6.1.6.3.1.1.5.3': 'linkDown',
  '1.3.6.1.6.3.1.1.5.4': 'linkUp',
  '1.3.6.1.6.3.1.1.5.5': 'authenticationFailure',
}
function trapLabel(oid: string): string {
  return WELL_KNOWN[(oid || '').replace(/^\./, '')] || oid
}

export function TrapsPage() {
  const [hours, setHours] = useState(24)
  const [severity, setSeverity] = useState<(typeof SEVERITIES)[number]>('all')
  const [search, setSearch] = useState('')

  const { data: stats } = useQuery<any>({
    queryKey: ['traps', 'stats', hours],
    queryFn: async () => (await api.get(`/traps/stats?hours=${hours}`)).data,
    refetchInterval: 30000,
  })

  const { data, isLoading, isError, refetch, isFetching } = useQuery<any>({
    queryKey: ['traps', hours, severity, search],
    queryFn: async () => {
      const params = new URLSearchParams({ hours: String(hours), limit: '500' })
      if (severity !== 'all') params.set('severity', severity)
      if (search.trim()) params.set('search', search.trim())
      return (await api.get(`/traps?${params.toString()}`)).data
    },
    refetchInterval: 30000,
  })
  const traps: any[] = data?.data || []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Inbox className="h-5 w-5 text-primary" />
            SNMP Traps
          </h1>
          <p className="text-xs text-muted">Network-wide SNMP trap &amp; event feed · received on UDP/162</p>
        </div>
        <div className="flex items-center gap-2">
          <div className="flex rounded-md border border-border bg-surface2 p-0.5 text-xs">
            {RANGES.map((r) => (
              <button
                key={r.hours}
                onClick={() => setHours(r.hours)}
                className={`rounded px-2.5 py-1 font-medium ${hours === r.hours ? 'bg-primary text-white' : 'text-muted hover:text-text'}`}
              >
                {r.label}
              </button>
            ))}
          </div>
          <Button variant="outline" size="sm" onClick={() => refetch()}>
            <RefreshCw className={`h-3.5 w-3.5 ${isFetching ? 'animate-spin' : ''}`} />
            Refresh
          </Button>
        </div>
      </div>

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { label: 'Total', value: stats?.total ?? 0, cls: 'text-text' },
          { label: 'Critical', value: stats?.critical ?? 0, cls: 'text-danger' },
          { label: 'Warning', value: stats?.warning ?? 0, cls: 'text-warning' },
          { label: 'Info', value: stats?.info ?? 0, cls: 'text-info' },
        ].map((c) => (
          <Card key={c.label}>
            <CardContent className="py-3">
              <div className="text-xs text-muted">{c.label}</div>
              <div className={`text-2xl font-semibold ${c.cls}`}>{c.value}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="flex rounded-md border border-border bg-surface2 p-0.5 text-xs">
          {SEVERITIES.map((s) => (
            <button
              key={s}
              onClick={() => setSeverity(s)}
              className={`rounded px-2.5 py-1 font-medium capitalize ${severity === s ? 'bg-primary text-white' : 'text-muted hover:text-text'}`}
            >
              {s}
            </button>
          ))}
        </div>
        <div className="relative min-w-[220px] flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
          <Input
            className="pl-8"
            placeholder="Search OID, name, message, source IP…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-36">Time</Th>
                  <Th className="w-24">Severity</Th>
                  <Th>Source / Device</Th>
                  <Th>Trap</Th>
                  <Th>Message</Th>
                </Tr>
              </THead>
              <TBody>
                {traps.map((t, i) => (
                  <Tr key={i}>
                    <Td className="whitespace-nowrap text-xs text-muted" title={t.timestamp}>
                      {relativeTime(t.timestamp)}
                    </Td>
                    <Td>
                      <Badge variant={sevVariant(t.severity)}>{t.severity}</Badge>
                    </Td>
                    <Td className="text-sm">
                      {t.device_hostname ? (
                        <span className="font-medium">{t.device_hostname}</span>
                      ) : (
                        <span className="text-muted">unmapped</span>
                      )}
                      <span className="ml-1 font-mono text-xs text-muted">{t.source_ip}</span>
                    </Td>
                    <Td className="font-mono text-xs">
                      <div className="text-text">
                        {t.trap_name && t.trap_name !== t.trap_oid ? t.trap_name : trapLabel(t.trap_oid)}
                      </div>
                      <div className="text-muted">{t.trap_oid}</div>
                    </Td>
                    <Td className="max-w-[24rem] truncate text-xs text-muted" title={t.message}>
                      {t.message}
                    </Td>
                  </Tr>
                ))}
                {!isLoading && traps.length === 0 && !isError && (
                  <Tr>
                    <Td colSpan={5} className="py-12 text-center text-muted">
                      No SNMP traps in this window. Point your devices' trap sink at this collector
                      (UDP/162) to populate this feed.
                    </Td>
                  </Tr>
                )}
                {isError && (
                  <Tr>
                    <Td colSpan={5} className="py-12 text-center text-danger">Failed to load traps.</Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
          {traps.length > 0 && (
            <div className="mt-2 text-xs text-muted">
              Showing {traps.length} trap{traps.length === 1 ? '' : 's'}.
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
