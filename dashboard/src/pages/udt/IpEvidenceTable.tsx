import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { udtApi } from './api'
import { fmtDate, relTime } from './helpers'

export function IpEvidenceTable({ endpointId, ip }: { endpointId: string; ip: string }) {
  const [skip, setSkip] = useState(0)
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ['udt', 'endpoint', endpointId, 'ip-evidence', ip, skip],
    queryFn: () => udtApi.ipEvidence(endpointId, ip, skip),
    refetchInterval: 20_000,
  })
  return <div className="space-y-3 border-t border-border bg-surface2/30 p-4">
    <h4 className="text-sm font-medium">Recent reporter evidence · <span className="font-mono">{ip}</span></h4>
    <p className="text-xs text-muted">Reports seen within 24 hours. Multiple MACs may indicate stale ARP, proxy ARP, overlapping networks, or an IP conflict. Network scope (VRF/subnet) is not reported, so ownership and conflicts are unconfirmed.</p>
    {isPending && <p role="status" className="text-sm text-muted">Loading reporter evidence…</p>}
    {isError && <div role="alert" className="text-sm">Unable to load evidence. <Button size="sm" variant="outline" onClick={() => refetch()}>Retry</Button></div>}
    {data && <>
      {data.data.length === 0 ? <p className="text-sm text-muted">No recent reporter evidence.</p> : <div className="overflow-x-auto">
        <Table>
          <THead><Tr><Th>Endpoint / MAC</Th><Th>Reporting device</Th><Th>Interface / source</Th><Th>Evidence</Th><Th>Last observed</Th></Tr></THead>
          <TBody>{data.data.map((r) => <Tr key={`${r.endpoint_id}:${r.reporting_device_id}:${r.if_index}:${r.source}:${r.legacy}`}>
            <Td><Link className="font-mono text-xs text-primary hover:underline" to={`/udt/endpoints/${r.endpoint_id}`}>{r.mac}</Link><span className="block text-xs text-muted">{r.endpoint_name || (r.endpoint_id === endpointId ? 'This endpoint' : 'Unnamed endpoint')}</span></Td>
            <Td className="text-xs">{r.reporter || 'Unknown reporter'}{r.reporter_ip && r.reporter_ip !== r.reporter && <span className="block font-mono text-muted">{r.reporter_ip}</span>}</Td>
            <Td className="text-xs">{r.interface_name || (r.if_index ? `Interface ${r.if_index}` : 'Interface not recorded')}<span className="block uppercase text-muted">{r.source}</span></Td>
            <Td><Badge variant="outline">{r.legacy ? 'Legacy record' : r.repeated ? 'Repeatedly observed' : 'Awaiting repeat evidence'}</Badge><span className="mt-1 block text-[11px] text-muted">{r.legacy ? 'Poll count unavailable' : `${r.observation_count} collected ${r.observation_count === 1 ? 'observation' : 'observations'}`}</span></Td>
            <Td className="text-xs text-muted"><time dateTime={r.last_seen} title={fmtDate(r.last_seen)}>{relTime(r.last_seen)}</time><span className="block" title={fmtDate(r.first_seen)}>First: {fmtDate(r.first_seen)}</span></Td>
          </Tr>)}</TBody>
        </Table>
      </div>}
      <p className="text-xs text-muted">Repeatedly observed means at least 3 separate polls from the same reporter and interface within 24 hours. This supports a reported binding; it does not confirm continuous connectivity. Legacy records retain only the last saved reporter.</p>
      {data.meta.total > 25 && <div className="flex items-center justify-between text-xs text-muted">
        <span>{skip + 1}–{Math.min(skip + data.data.length, data.meta.total)} of {data.meta.total} reports</span>
        <div className="flex gap-2"><Button size="sm" variant="outline" disabled={skip === 0} onClick={() => setSkip(Math.max(0, skip - 25))}>Previous reports</Button><Button size="sm" variant="outline" disabled={skip + 25 >= data.meta.total} onClick={() => setSkip(skip + 25)}>Next reports</Button></div>
      </div>}
    </>}
  </div>
}
