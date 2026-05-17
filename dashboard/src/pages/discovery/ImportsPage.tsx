import { useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Eye, Inbox } from 'lucide-react'
import { discoveryApi } from './api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { relativeTime } from '@/lib/utils'

export function ImportsPage() {
  const navigate = useNavigate()
  const { data: batches = [], isLoading } = useQuery({
    queryKey: ['discovery', 'imports'],
    queryFn: () => discoveryApi.listImports(),
    refetchInterval: 8000,
  })
  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted">Loading…</CardContent>
      </Card>
    )
  }
  if (batches.length === 0) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10">
            <Inbox className="h-7 w-7 text-primary" />
          </div>
          <h3 className="text-lg font-semibold">No import history yet</h3>
          <p className="max-w-md text-sm text-muted">
            Every time you import devices from a discovery run, the batch shows up here
            so you can audit what was added, skipped, or conflicted.
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
              <Th>Batch</Th>
              <Th>Status</Th>
              <Th className="text-right">Total</Th>
              <Th className="text-right">Imported</Th>
              <Th className="text-right">Skipped</Th>
              <Th className="text-right">Failed</Th>
              <Th>Started</Th>
              <Th className="w-24 text-right">Run</Th>
            </Tr>
          </THead>
          <TBody>
            {batches.map((b) => (
              <Tr key={b.id}>
                <Td className="font-mono text-xs">{b.id.slice(0, 8)}</Td>
                <Td>
                  <Badge
                    variant={
                      b.status === 'completed'
                        ? 'success'
                        : b.status === 'failed'
                          ? 'danger'
                          : b.status === 'partial'
                            ? 'warning'
                            : 'info'
                    }
                  >
                    {b.status}
                  </Badge>
                </Td>
                <Td className="text-right tabular-nums">{b.total_items}</Td>
                <Td className="text-right tabular-nums text-success">{b.successful_items}</Td>
                <Td className="text-right tabular-nums text-muted">{b.skipped_items}</Td>
                <Td className="text-right tabular-nums text-warning">{b.failed_items}</Td>
                <Td className="text-xs text-muted">{relativeTime(b.started_at)}</Td>
                <Td>
                  <div className="flex justify-end">
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => navigate(`/discovery/runs/${b.run_id}`)}
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
