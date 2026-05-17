import { useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ArrowLeft,
  Calendar,
  Copy,
  Edit3,
  Eye,
  Network,
  Pause,
  Play,
  Radar,
  Settings2,
  Trash2,
} from 'lucide-react'
import { discoveryApi } from './api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage, formatDuration, relativeTime } from '@/lib/utils'
import {
  RunStatusBadge,
  describeNextRun,
  formatScope,
  ProtocolPill,
} from './helpers'

export function ProfileDetailPage() {
  const { id } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const qc = useQueryClient()

  const { data: profile } = useQuery({
    queryKey: ['discovery', 'profile', id],
    queryFn: () => discoveryApi.getProfile(id!),
    enabled: !!id,
    refetchInterval: 8000,
  })
  const { data: runs = [] } = useQuery({
    queryKey: ['discovery', 'runs', id],
    queryFn: () => discoveryApi.listRuns(id),
    enabled: !!id,
    refetchInterval: 6000,
  })

  const runMutation = useMutation({
    mutationFn: () => discoveryApi.startRun(id!),
    onSuccess: (run) => navigate(`/discovery/runs/${run.id}`),
    onError: (e: any) => toast.error('Run failed', apiErrorMessage(e)),
  })
  const cloneMutation = useMutation({
    mutationFn: () => discoveryApi.cloneProfile(id!),
    onSuccess: (p) => {
      qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] })
      navigate(`/discovery/profiles/${p.id}`)
    },
  })
  const deleteMutation = useMutation({
    mutationFn: () => discoveryApi.deleteProfile(id!),
    onSuccess: () => {
      toast.success('Profile deleted')
      navigate('/discovery')
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })
  const pauseScheduleMutation = useMutation({
    mutationFn: (sid: string) => discoveryApi.pauseSchedule(sid),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['discovery', 'profile', id] }),
  })
  const resumeScheduleMutation = useMutation({
    mutationFn: (sid: string) => discoveryApi.resumeSchedule(sid),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['discovery', 'profile', id] }),
  })

  if (!profile) {
    return (
      <Card>
        <CardContent className="py-12 text-center text-sm text-muted">
          Loading…
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <button
            onClick={() => navigate('/discovery')}
            className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Back to profiles
          </button>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <Radar className="h-6 w-6 text-primary" />
            {profile.name}
            {!profile.enabled && <Badge variant="outline">disabled</Badge>}
          </h1>
          {profile.description && (
            <p className="mt-1 max-w-2xl text-xs text-muted">{profile.description}</p>
          )}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => runMutation.mutate()} disabled={runMutation.isPending}>
            <Play className="h-4 w-4" /> Run now
          </Button>
          <Button variant="outline" onClick={() => navigate(`/discovery/profiles/${profile.id}/edit`)}>
            <Edit3 className="h-4 w-4" /> Edit
          </Button>
          <Button variant="outline" onClick={() => cloneMutation.mutate()}>
            <Copy className="h-4 w-4" /> Clone
          </Button>
          <Button
            variant="ghost"
            className="text-muted hover:text-danger"
            onClick={() => {
              if (confirm(`Delete profile "${profile.name}"?`)) deleteMutation.mutate()
            }}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        </div>
      </div>

      <div className="grid gap-3 md:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Network className="h-4 w-4 text-primary" /> Scope
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            <Row label="Type" value={profile.scope_type} />
            <Row label="Targets" value={formatScope(profile)} />
            {profile.exclusions.length > 0 && (
              <Row label="Excluded" value={`${profile.exclusions.length} range(s)`} />
            )}
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Settings2 className="h-4 w-4 text-primary" /> Protocols & options
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            <div className="mb-1 flex flex-wrap gap-1">
              {profile.protocols.map((p) => (
                <ProtocolPill key={p} p={p} />
              ))}
            </div>
            <Row label="Concurrency" value={profile.max_concurrency} />
            <Row label="Timeout" value={`${profile.scan_timeout_ms} ms`} />
            <Row label="SNMP creds" value={profile.snmp_credential_ids.length} />
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="flex items-center gap-2 text-sm">
              <Calendar className="h-4 w-4 text-primary" /> Schedule
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-1 text-xs">
            {profile.schedule_id ? (
              <>
                <Row label="Frequency" value={profile.schedule_summary || '—'} />
                <Row label="Next run" value={describeNextRun(profile.next_run_at)} />
                <div className="mt-2 flex gap-1">
                  {profile.enabled ? (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() =>
                        profile.schedule_id && pauseScheduleMutation.mutate(profile.schedule_id)
                      }
                    >
                      <Pause className="h-3.5 w-3.5" /> Pause
                    </Button>
                  ) : (
                    <Button
                      size="sm"
                      onClick={() =>
                        profile.schedule_id && resumeScheduleMutation.mutate(profile.schedule_id)
                      }
                    >
                      <Play className="h-3.5 w-3.5" /> Resume
                    </Button>
                  )}
                </div>
              </>
            ) : (
              <div className="text-muted">No schedule configured.</div>
            )}
          </CardContent>
        </Card>
      </div>

      <div>
        <h3 className="mb-2 text-sm font-semibold">Run history</h3>
        {runs.length === 0 ? (
          <Card>
            <CardContent className="py-12 text-center text-sm text-muted">
              No runs yet for this profile.
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardContent className="p-0">
              <Table>
                <THead className="bg-surface2/50">
                  <Tr>
                    <Th>Run</Th>
                    <Th>Status</Th>
                    <Th>Trigger</Th>
                    <Th>Started</Th>
                    <Th>Duration</Th>
                    <Th className="text-right">Total</Th>
                    <Th className="text-right">New</Th>
                    <Th className="text-right">Existing</Th>
                    <Th className="text-right">Failed</Th>
                    <Th className="w-24 text-right"></Th>
                  </Tr>
                </THead>
                <TBody>
                  {runs.map((r) => (
                    <Tr key={r.id}>
                      <Td className="font-mono text-xs">{r.id.slice(0, 8)}</Td>
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
                        {r.duration_ms
                          ? formatDuration(Math.round(r.duration_ms / 1000))
                          : '—'}
                      </Td>
                      <Td className="text-right tabular-nums">{r.responding_targets}</Td>
                      <Td className="text-right tabular-nums text-info">
                        {r.new_devices || '—'}
                      </Td>
                      <Td className="text-right tabular-nums text-muted">
                        {r.existing_devices || '—'}
                      </Td>
                      <Td className="text-right tabular-nums">
                        {r.failed_targets ? (
                          <span className="text-warning">{r.failed_targets}</span>
                        ) : (
                          <span className="text-muted">—</span>
                        )}
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
        )}
      </div>
    </div>
  )
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-muted">{label}</span>
      <span className="text-text">{value}</span>
    </div>
  )
}
