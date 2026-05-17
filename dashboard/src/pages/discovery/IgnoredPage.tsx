import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { SlashSquare, Trash2 } from 'lucide-react'
import { discoveryApi } from './api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, relativeTime } from '@/lib/utils'

export function IgnoredPage() {
  const qc = useQueryClient()
  const { data: ignored = [], isLoading } = useQuery({
    queryKey: ['discovery', 'ignored'],
    queryFn: discoveryApi.listIgnored,
  })
  const removeMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.removeIgnored(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery', 'ignored'] })
      toast.success('Removed from ignore list')
    },
    onError: (e: any) => toast.error('Could not remove', apiErrorMessage(e)),
  })

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted">Loading…</CardContent>
      </Card>
    )
  }
  if (ignored.length === 0) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10">
            <SlashSquare className="h-7 w-7 text-primary" />
          </div>
          <h3 className="text-lg font-semibold">No ignored devices</h3>
          <p className="max-w-md text-sm text-muted">
            Devices you choose to ignore during discovery results review will appear here
            and be excluded from future scans.
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
              <Th>IP address</Th>
              <Th>Hostname</Th>
              <Th>MAC</Th>
              <Th>Reason</Th>
              <Th>Ignored</Th>
              <Th className="w-12"></Th>
            </Tr>
          </THead>
          <TBody>
            {ignored.map((r) => (
              <Tr key={r.id}>
                <Td className="font-mono text-xs">{r.ip_address || '—'}</Td>
                <Td>{r.hostname || '—'}</Td>
                <Td className="font-mono text-xs text-muted">{r.mac_address || '—'}</Td>
                <Td className="text-xs text-muted">{r.reason || '—'}</Td>
                <Td className="text-xs text-muted">{relativeTime(r.ignored_at)}</Td>
                <Td>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-muted hover:text-danger"
                    onClick={() => removeMutation.mutate(r.id)}
                    title="Remove from ignore list"
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </CardContent>
    </Card>
  )
}
