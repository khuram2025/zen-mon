import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { Eye, FileBarChart } from 'lucide-react'
import { discoveryApi } from './api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { relativeTime, formatDuration } from '@/lib/utils'
import { RunStatusBadge, RunCountLink } from './helpers'

export function ReportsPage() {
  const navigate = useNavigate()
  const { data: runs = [], isLoading } = useQuery({
    queryKey: ['discovery', 'runs'],
    queryFn: () => discoveryApi.listRuns(),
    refetchInterval: 8000,
  })

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted">Loading…</CardContent>
      </Card>
    )
  }
  if (runs.length === 0) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10">
            <FileBarChart className="h-7 w-7 text-primary" />
          </div>
          <h3 className="text-lg font-semibold">No discovery reports yet</h3>
          <p className="max-w-md text-sm text-muted">
            Reports appear here after a discovery scan completes. Run any profile to
            generate a report.
          </p>
        </CardContent>
      </Card>
    )
  }

  return (
    <Card>
      <CardContent className="p-0">
        <Table>
          <THead className="bg-surface2/50">
            <Tr>
              <Th>Profile</Th>
              <Th>Status</Th>
              <Th>Trigger</Th>
              <Th>Started</Th>
              <Th>Duration</Th>
              <Th className="text-right">Total</Th>
              <Th className="text-right">New</Th>
              <Th className="text-right">Existing</Th>
              <Th className="text-right">Failed</Th>
              <Th className="w-24 text-right">Report</Th>
            </Tr>
          </THead>
          <TBody>
            {runs.map((r) => (
              <Tr key={r.id}>
                <Td>
                  <button
                    type="button"
                    onClick={() => navigate(`/discovery/profiles/${r.profile_id}`)}
                    className="text-left font-medium hover:underline"
                  >
                    {r.profile_name || '—'}
                  </button>
                  <div className="text-[10px] font-mono text-muted">{r.id.slice(0, 8)}</div>
                </Td>
                <Td>
                  <RunStatusBadge status={r.status} />
                </Td>
                <Td>
                  <Badge variant="outline">{r.trigger_type}</Badge>
                </Td>
                <Td className="text-xs text-muted">
                  {relativeTime(r.started_at || r.created_at)}
                </Td>
                <Td className="text-xs">
                  {r.duration_ms ? formatDuration(Math.round(r.duration_ms / 1000)) : '—'}
                </Td>
                <Td className="text-right tabular-nums">
                  <RunCountLink runId={r.id} filter="all" value={r.responding_targets} />
                </Td>
                <Td className="text-right tabular-nums text-info">
                  <RunCountLink
                    runId={r.id}
                    filter="new"
                    value={r.new_devices}
                    className="text-info"
                  />
                </Td>
                <Td className="text-right tabular-nums text-muted">
                  <RunCountLink
                    runId={r.id}
                    filter="existing"
                    value={r.existing_devices}
                    className="text-muted"
                  />
                </Td>
                <Td className="text-right tabular-nums">
                  <RunCountLink
                    runId={r.id}
                    filter="failed"
                    value={r.failed_targets}
                    className={r.failed_targets ? 'text-warning' : 'text-muted'}
                  />
                </Td>
                <Td>
                  <div className="flex justify-end">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => navigate(`/discovery/runs/${r.id}`)}
                    >
                      <Eye className="h-3.5 w-3.5" /> View
                    </Button>
                  </div>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  )
}
