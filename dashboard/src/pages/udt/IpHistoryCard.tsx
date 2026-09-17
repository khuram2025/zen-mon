import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { udtApi } from './api'
import { IpEvidenceTable } from './IpEvidenceTable'
import { fmtDate, relTime } from './helpers'
import type { IpAddressHistory } from './types'

function RecordedPeriods({ endpointId, ip }: { endpointId: string; ip: string }) {
  const [skip, setSkip] = useState(0)
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ['udt', 'endpoint', endpointId, 'ip-periods', ip, skip],
    queryFn: () => udtApi.ipPeriods(endpointId, ip, skip),
    refetchInterval: 20_000,
  })
  return (
    <div className="space-y-3 border-t border-border bg-surface2/30 p-4">
      <h4 className="text-sm font-medium">Recorded periods · <span className="font-mono">{ip}</span></h4>
      <p className="text-xs text-muted">Original records are retained, including repetitions produced by earlier collection behavior. Record counts are not reconnects, IP changes, or poll counts. Gaps do not establish disconnection.</p>
      {isPending && <p className="text-sm text-muted" role="status">Loading recorded periods…</p>}
      {isError && <div role="alert" className="text-sm">Unable to load audit records. <Button size="sm" variant="outline" onClick={() => refetch()}>Retry</Button></div>}
      {data && <>
        <div className="max-h-72 overflow-auto">
          <Table>
            <THead><Tr><Th>First observed</Th><Th>Last observed</Th><Th>Reporter / source</Th><Th>State</Th></Tr></THead>
            <TBody>{data.data.map((period) => <Tr key={period.id}>
              <Td className="whitespace-nowrap text-xs">{fmtDate(period.first_seen)}</Td>
              <Td className="whitespace-nowrap text-xs">{fmtDate(period.last_seen)}</Td>
              <Td className="text-xs">{period.reporting_device || 'Unknown reporter'}<span className="block uppercase text-muted">{period.source}</span></Td>
              <Td><Badge variant={period.active ? 'success' : 'outline'}>{period.active ? 'Recent' : 'Past'}</Badge></Td>
            </Tr>)}</TBody>
          </Table>
        </div>
        <div className="flex items-center justify-between gap-2 text-xs text-muted">
          <span>{data.meta.total ? skip + 1 : 0}–{Math.min(skip + data.data.length, data.meta.total)} of {data.meta.total} records</span>
          <div className="flex gap-2">
            <Button size="sm" variant="outline" disabled={skip === 0} onClick={() => setSkip(Math.max(0, skip - 25))}>Newer records</Button>
            <Button size="sm" variant="outline" disabled={skip + 25 >= data.meta.total} onClick={() => setSkip(skip + 25)}>Older records</Button>
          </div>
        </div>
      </>}
    </div>
  )
}

export function IpHistoryCard({ endpointId, addresses }: { endpointId: string; addresses: IpAddressHistory[] }) {
  const [selected, setSelected] = useState<string | null>(null)
  const [page, setPage] = useState(0)
  const currentPage = Math.min(page, Math.max(0, Math.ceil(addresses.length / 10) - 1))
  const start = currentPage * 10
  return (
    <Card>
      <CardContent className="p-0">
        <div className="border-b border-border px-4 py-3">
          <div className="flex items-center justify-between gap-2"><h3 className="text-sm font-semibold">IP address history</h3><span className="text-xs text-muted">{addresses.length} unique addresses</span></div>
          <p className="mt-1 text-xs text-muted">One row per address. Recent means observed within 24 hours; it does not confirm connectivity. Primary address changes require repeat evidence. Previous records are retained.</p>
        </div>
        {addresses.length === 0 ? <div className="p-6 text-center text-sm text-muted">No IP bindings observed (needs ARP from an L3 device).</div> : <>
          <Table>
            <THead className="bg-surface2/40"><Tr><Th>IP / source</Th><Th>State</Th><Th>First observed</Th><Th>Last observed</Th><Th>History</Th></Tr></THead>
            <TBody>{addresses.slice(start, start + 10).map((address) => <Tr key={address.ip}>
              <Td><span className="font-mono text-xs tabular-nums">{address.ip}</span><span className="block text-[11px] uppercase text-muted">{address.source}</span></Td>
              <Td><Badge variant={address.active ? 'success' : 'outline'}>{address.active ? 'Recent' : 'Past'}</Badge>
                {address.active && address.active_endpoint_count > 1 && <span className="mt-1 block text-[11px] text-muted" title="This address has recent observations on multiple endpoints. It may represent overlapping networks, proxy ARP, or an address conflict.">Shared · {address.active_endpoint_count} endpoints</span>}
              </Td>
              <Td className="text-xs text-muted"><time dateTime={address.first_seen} title={fmtDate(address.first_seen)}>{fmtDate(address.first_seen)}</time></Td>
              <Td className="text-xs text-muted"><time dateTime={address.last_seen} title={fmtDate(address.last_seen)}>{relTime(address.last_seen)}</time></Td>
              <Td><button className="text-xs text-primary hover:underline" aria-expanded={selected === address.ip} aria-label={`View history and reporter evidence for ${address.ip}`} onClick={() => setSelected(selected === address.ip ? null : address.ip)}>View history</button></Td>
            </Tr>)}</TBody>
          </Table>
          <div className="flex items-center justify-between gap-2 border-t border-border px-4 py-3 text-xs text-muted">
            <span>{start + 1}–{Math.min(start + 10, addresses.length)} of {addresses.length} addresses</span>
            {addresses.length > 10 && <div className="flex gap-2">
              <Button size="sm" variant="outline" disabled={currentPage === 0} onClick={() => { setPage(currentPage - 1); setSelected(null) }}>Previous</Button>
              <Button size="sm" variant="outline" disabled={start + 10 >= addresses.length} onClick={() => { setPage(currentPage + 1); setSelected(null) }}>Next</Button>
            </div>}
          </div>
          {selected && <div key={`${endpointId}:${selected}`}>
            <IpEvidenceTable endpointId={endpointId} ip={selected} />
            <details className="border-t border-border">
              <summary className="cursor-pointer px-4 py-3 text-xs text-primary">Preserved audit records ({addresses.find(a => a.ip === selected)?.period_count ?? 0})</summary>
              <RecordedPeriods endpointId={endpointId} ip={selected} />
            </details>
          </div>}
        </>}
      </CardContent>
    </Card>
  )
}
