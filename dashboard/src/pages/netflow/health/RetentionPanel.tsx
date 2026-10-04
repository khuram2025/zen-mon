import { Link } from 'react-router-dom'
import { Archive } from 'lucide-react'
import { useCan } from '@/stores/auth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { QueryPanel } from '@/components/netflow/QueryPanel'
import { nfKeys, useNfQuery } from '@/lib/netflow/api'
import { formatDateTime } from '@/lib/netflow/format'
import { displayTimeZone } from '@/lib/netflow/window'
import { retentionRows, retentionText, type RetentionInfo } from './model'
import { ToneChip } from './ui'

/**
 * Retention disclosure (FE-11): how long each flow dataset keeps data, at which granularity, the
 * oldest data actually present, and what each one powers — so a long range answered from
 * summaries, or a search older than raw retention, is explained rather than silently empty.
 */
export function RetentionPanel() {
  const q = useNfQuery<RetentionInfo>('/netflow/retention', null, { queryKey: nfKeys.retention(), refetchInterval: false, staleTime: 300_000 })
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <Archive className="h-4 w-4 text-primary" aria-hidden /> Data retention
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        <QueryPanel query={q} label="data retention" skeleton="rows">
          {(r) => <RetentionBody r={r.data} />}
        </QueryPanel>
      </CardContent>
    </Card>
  )
}

function RetentionBody({ r }: { r: RetentionInfo }) {
  const tz = displayTimeZone()
  const can = useCan()
  const rows = retentionRows(r)
  const repair = r.sampling_repair
  return (
    <div className="space-y-3">
      {r.known === false && (
        <ToneChip tone="warning">Retention could not be read from the flow database</ToneChip>
      )}
      <div className="hidden md:block">
        <Table>
          <THead>
            <Tr>
              <Th>Dataset</Th>
              <Th>Detail</Th>
              <Th>Kept</Th>
              <Th>Oldest data</Th>
              <Th>Used for</Th>
            </Tr>
          </THead>
          <TBody>
            {rows.map((x) => (
              <Tr key={x.key}>
                <Td className="text-xs font-medium text-text">{x.dataset}</Td>
                <Td className="whitespace-nowrap text-xs">{x.granularity}</Td>
                <Td className="whitespace-nowrap text-xs">
                  {retentionText(x.days)}
                  {x.configured != null && x.configured !== x.days && (
                    <div className="text-[11px] text-warning-fg">configured {x.configured} days, not applied</div>
                  )}
                </Td>
                <Td className="whitespace-nowrap text-xs tabular-nums">{x.earliest ? formatDateTime(x.earliest, tz, { seconds: false }) : 'No data yet'}</Td>
                <Td className="text-xs text-text2">{x.powers}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
      <ul className="space-y-2 md:hidden">
        {rows.map((x) => (
          <li key={x.key} className="rounded-md border border-border p-3 text-xs">
            <div className="font-medium text-text">{x.dataset}</div>
            <dl className="mt-1 grid grid-cols-2 gap-x-3 gap-y-0.5">
              <dt className="text-text2">Detail</dt><dd className="text-right text-text">{x.granularity}</dd>
              <dt className="text-text2">Kept</dt><dd className="text-right text-text">{retentionText(x.days)}</dd>
              <dt className="text-text2">Oldest</dt><dd className="text-right tabular-nums text-text">{x.earliest ? formatDateTime(x.earliest, tz, { seconds: false }) : 'No data yet'}</dd>
            </dl>
            <p className="mt-1 text-[11px] text-text2">{x.powers}</p>
          </li>
        ))}
      </ul>
      <div className="space-y-1 text-xs text-text2">
        <p>
          Beyond {r.raw_days != null ? `${r.raw_days} days` : 'raw retention'} individual flows are gone: long ranges are answered from
          the hourly and 5-minute summaries (views say so), and Forensics cannot search or export older records. Summaries are never
          expanded back into flows.
        </p>
        <p>
          Retention is set in{' '}
          {can('system.admin')
            ? <Link to="/settings/general?tab=storage" className="font-medium text-text underline underline-offset-2">Settings → Storage</Link>
            : 'Settings → Storage (administrators)'}
          .
        </p>
        {repair?.affected && (
          <p className="text-warning-fg">
            Sampled NetFlow v5 volumes recorded before the upgrade were corrected in raw data; summaries
            {repair.verified_from ? ` before ${formatDateTime(repair.verified_from, tz, { seconds: false })}` : ''} could not all be re-checked.
          </p>
        )}
      </div>
    </div>
  )
}
