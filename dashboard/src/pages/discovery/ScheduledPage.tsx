import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Calendar, Edit3, Eye, Pause, Play, PlayCircle, Trash2 } from 'lucide-react'
import { discoveryApi } from './api'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { RunStatusBadge, describeNextRun } from './helpers'

export function ScheduledPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: profiles = [], isLoading } = useQuery({
    queryKey: ['discovery', 'profiles'],
    queryFn: discoveryApi.listProfiles,
    refetchInterval: 8000,
  })

  const scheduled = profiles.filter((p) => p.schedule_id)

  const pauseMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.pauseSchedule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] }),
  })
  const resumeMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.resumeSchedule(id),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] }),
  })
  const deleteScheduleMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.deleteSchedule(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] })
      toast.success('Schedule removed')
    },
    onError: (e: any) => toast.error('Could not remove schedule', apiErrorMessage(e)),
  })
  const runMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.startRun(id),
    onSuccess: (run) => navigate(`/discovery/runs/${run.id}`),
    onError: (e: any) => toast.error('Run failed', apiErrorMessage(e)),
  })
  const tickMutation = useMutation({
    mutationFn: () => discoveryApi.tickScheduler(),
    onSuccess: (r) => {
      qc.invalidateQueries({ queryKey: ['discovery'] })
      toast.success(
        `Scheduler check ran`,
        r.triggered.length ? `${r.triggered.length} scan(s) triggered` : 'No schedules ready',
      )
    },
  })

  if (isLoading) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted">
          Loading…
        </CardContent>
      </Card>
    )
  }

  if (scheduled.length === 0) {
    return (
      <Card>
        <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
          <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10">
            <Calendar className="h-7 w-7 text-primary" />
          </div>
          <h3 className="text-lg font-semibold">No scheduled discovery yet</h3>
          <p className="max-w-md text-sm text-muted">
            Schedule recurring scans from the wizard. Discovery Profiles with a schedule
            will appear here, along with their next run time.
          </p>
          <Button onClick={() => navigate('/discovery/new')}>Create profile</Button>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <Button variant="outline" size="sm" onClick={() => tickMutation.mutate()}>
          <PlayCircle className="h-4 w-4" /> Run scheduler now
        </Button>
      </div>
      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th>Profile</Th>
                <Th>Frequency</Th>
                <Th>Next run</Th>
                <Th>Last run</Th>
                <Th className="text-right">New from last</Th>
                <Th className="w-44 text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {scheduled.map((p) => (
                <Tr key={p.id}>
                  <Td>
                    <button
                      onClick={() => navigate(`/discovery/profiles/${p.id}`)}
                      className="text-left font-medium hover:underline"
                    >
                      {p.name}
                    </button>
                    {!p.enabled && (
                      <Badge variant="outline" className="ml-2">
                        paused
                      </Badge>
                    )}
                  </Td>
                  <Td className="text-sm">{p.schedule_summary || '—'}</Td>
                  <Td className="text-sm">{describeNextRun(p.next_run_at)}</Td>
                  <Td>
                    <div className="flex flex-col gap-0.5">
                      <RunStatusBadge status={p.last_run_status as any} />
                      <span className="text-[10px] text-muted">
                        {relativeTime(p.last_run_at)}
                      </span>
                    </div>
                  </Td>
                  <Td className="text-right font-medium tabular-nums text-info">
                    {p.new_devices_found || '—'}
                  </Td>
                  <Td>
                    <div className="flex items-center justify-end gap-1">
                      <Button size="sm" onClick={() => runMutation.mutate(p.id)}>
                        <Play className="h-3.5 w-3.5" /> Run now
                      </Button>
                      {p.enabled ? (
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => p.schedule_id && pauseMutation.mutate(p.schedule_id)}
                          title="Pause schedule"
                          className="h-8 w-8"
                        >
                          <Pause className="h-4 w-4" />
                        </Button>
                      ) : (
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => p.schedule_id && resumeMutation.mutate(p.schedule_id)}
                          title="Resume schedule"
                          className="h-8 w-8"
                        >
                          <Play className="h-4 w-4" />
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() => navigate(`/discovery/profiles/${p.id}/edit`)}
                        title="Edit"
                        className="h-8 w-8"
                      >
                        <Edit3 className="h-4 w-4" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        onClick={() =>
                          p.schedule_id && deleteScheduleMutation.mutate(p.schedule_id)
                        }
                        title="Remove schedule"
                        className="h-8 w-8 text-muted hover:text-danger"
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    </div>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
