import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  AlertTriangle,
  Calendar,
  Copy,
  Edit3,
  Layers,
  MoreVertical,
  Pause,
  Play,
  Plus,
  Radar,
  Trash2,
} from 'lucide-react'
import { discoveryApi } from './api'
import { DiscoveryProfile } from './types'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Badge } from '@/components/ui/Badge'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { RunStatusBadge, formatScope, RunCountLink } from './helpers'

export function ProfilesPage() {
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [search, setSearch] = useState('')
  const [confirmDelete, setConfirmDelete] = useState<DiscoveryProfile | null>(null)

  const { data: profiles = [], isLoading } = useQuery({
    queryKey: ['discovery', 'profiles'],
    queryFn: discoveryApi.listProfiles,
    refetchInterval: 8000,
  })

  const runMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.startRun(id),
    onSuccess: (run) => {
      qc.invalidateQueries({ queryKey: ['discovery'] })
      toast.success('Scan started', `Run ${run.id.slice(0, 8)} is queued`)
      navigate(`/discovery/runs/${run.id}`)
    },
    onError: (e: any) => toast.error('Could not start scan', apiErrorMessage(e)),
  })

  const cloneMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.cloneProfile(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] })
      toast.success('Profile cloned')
    },
    onError: (e: any) => toast.error('Clone failed', apiErrorMessage(e)),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => discoveryApi.deleteProfile(id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] })
      toast.success('Profile deleted')
      setConfirmDelete(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const toggleEnabledMutation = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      discoveryApi.updateProfile(id, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['discovery', 'profiles'] }),
  })

  const filtered = profiles.filter((p) =>
    !search ||
    p.name.toLowerCase().includes(search.toLowerCase()) ||
    (p.description || '').toLowerCase().includes(search.toLowerCase()) ||
    p.targets.some((t) => t.includes(search)),
  )

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative flex-1 min-w-[220px]">
          <Input
            placeholder="Search profiles by name, description, or target…"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
          />
        </div>
        <Button onClick={() => navigate('/discovery/new')}>
          <Plus className="h-4 w-4" /> New discovery profile
        </Button>
      </div>

      {isLoading ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted">
            Loading profiles…
          </CardContent>
        </Card>
      ) : filtered.length === 0 && !search ? (
        <Card>
          <CardContent className="flex flex-col items-center gap-3 py-16 text-center">
            <div className="flex h-14 w-14 items-center justify-center rounded-full bg-primary/10">
              <Layers className="h-7 w-7 text-primary" />
            </div>
            <h3 className="text-lg font-semibold text-text">No discovery profiles yet</h3>
            <p className="max-w-md text-sm text-muted">
              Create a profile to scan your network and find devices automatically.
              Each profile bundles a scan scope, credentials, and import rules.
            </p>
            <Button onClick={() => navigate('/discovery/new')}>
              <Plus className="h-4 w-4" /> Create first profile
            </Button>
          </CardContent>
        </Card>
      ) : filtered.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted">
            No profiles match your search.
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th>Name</Th>
                  <Th>Scope</Th>
                  <Th>Schedule</Th>
                  <Th>Last run</Th>
                  <Th className="text-right">Found</Th>
                  <Th className="text-right">New</Th>
                  <Th className="text-right">Existing</Th>
                  <Th className="text-right">Failed</Th>
                  <Th className="w-44 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {filtered.map((p) => (
                  <Tr key={p.id}>
                    <Td>
                      <button
                        type="button"
                        onClick={() => navigate(`/discovery/profiles/${p.id}`)}
                        className="block max-w-xs text-left"
                      >
                        <div className="flex items-center gap-2 font-medium text-text">
                          {p.name}
                          {!p.enabled && (
                            <Badge variant="outline" className="text-[10px]">
                              disabled
                            </Badge>
                          )}
                        </div>
                        {p.description && (
                          <div className="truncate text-xs text-muted">{p.description}</div>
                        )}
                      </button>
                    </Td>
                    <Td className="font-mono text-xs">{formatScope(p)}</Td>
                    <Td className="text-xs">
                      {p.schedule_summary ? (
                        <div className="flex items-center gap-1.5 text-text">
                          <Calendar className="h-3.5 w-3.5 text-primary" />
                          {p.schedule_summary}
                        </div>
                      ) : (
                        <span className="text-muted">One-time</span>
                      )}
                    </Td>
                    <Td>
                      <div className="flex flex-col gap-0.5">
                        <RunStatusBadge status={p.last_run_status as any} />
                        <span className="text-[10px] text-muted">
                          {relativeTime(p.last_run_at)}
                        </span>
                      </div>
                    </Td>
                    <Td className="text-right font-medium tabular-nums">
                      <RunCountLink
                        runId={p.last_run_id}
                        filter="all"
                        value={p.total_devices_found}
                        className="font-medium"
                      />
                    </Td>
                    <Td className="text-right font-medium tabular-nums text-info">
                      <RunCountLink
                        runId={p.last_run_id}
                        filter="new"
                        value={p.new_devices_found}
                        className="font-medium text-info"
                      />
                    </Td>
                    <Td className="text-right font-medium tabular-nums text-muted">
                      <RunCountLink
                        runId={p.last_run_id}
                        filter="existing"
                        value={p.existing_devices_matched}
                        className="font-medium text-muted"
                      />
                    </Td>
                    <Td className="text-right font-medium tabular-nums">
                      <RunCountLink
                        runId={p.last_run_id}
                        filter="failed"
                        value={p.failed_targets}
                        className={p.failed_targets ? 'font-medium text-warning' : 'font-medium text-muted'}
                      />
                    </Td>
                    <Td>
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          size="sm"
                          onClick={() => runMutation.mutate(p.id)}
                          disabled={runMutation.isPending}
                        >
                          <Play className="h-3.5 w-3.5" /> Run now
                        </Button>
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
                          onClick={() => cloneMutation.mutate(p.id)}
                          title="Clone"
                          className="h-8 w-8"
                        >
                          <Copy className="h-4 w-4" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() =>
                            toggleEnabledMutation.mutate({ id: p.id, enabled: !p.enabled })
                          }
                          title={p.enabled ? 'Disable' : 'Enable'}
                          className="h-8 w-8"
                        >
                          <Pause className={`h-4 w-4 ${p.enabled ? '' : 'text-muted'}`} />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => setConfirmDelete(p)}
                          title="Delete"
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
      )}

      <Dialog open={!!confirmDelete} onOpenChange={(o) => !o && setConfirmDelete(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <AlertTriangle className="h-5 w-5 text-warning" />
              Delete discovery profile?
            </DialogTitle>
          </DialogHeader>
          {confirmDelete && (
            <div className="space-y-2 text-sm">
              <p>
                This permanently deletes the profile <strong>{confirmDelete.name}</strong>,
                its schedule, all run history, and unimported results.
                Devices that were already imported into inventory are not affected.
              </p>
              <p className="text-muted">This cannot be undone.</p>
            </div>
          )}
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmDelete(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => confirmDelete && deleteMutation.mutate(confirmDelete.id)}
              disabled={deleteMutation.isPending}
            >
              Delete profile
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
