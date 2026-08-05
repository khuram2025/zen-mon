import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Copy, Check, KeyRound, Loader2, ShieldCheck } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Badge } from '@/components/ui/Badge'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'

interface IngestKey {
  id: string
  name: string
  kind: 'sdk' | 'rum'
  key_prefix: string
  env: string | null
  enabled: boolean
  revoked_at: string | null
  created_at: string
}

interface Environment {
  id: string
  name: string
}

export function ApmSettingsPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [revoking, setRevoking] = useState<IngestKey | null>(null)
  const [createdKey, setCreatedKey] = useState<string | null>(null)

  const keysQuery = useQuery<IngestKey[]>({
    queryKey: ['apm', 'ingest-keys'],
    queryFn: async () => (await api.get('/apm/ingest-keys')).data,
  })

  const revoke = useMutation({
    mutationFn: async (id: string) => api.delete(`/apm/ingest-keys/${id}`),
    onSuccess: () => {
      toast.success('Ingest key revoked')
      qc.invalidateQueries({ queryKey: ['apm', 'ingest-keys'] })
      setRevoking(null)
    },
    onError: (e: any) => toast.error('Revoke failed', apiErrorMessage(e)),
  })

  const keys = keysQuery.data ?? []

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-2xl font-semibold text-text">APM Settings</h1>
        <p className="text-sm text-muted mt-1">
          Manage OpenTelemetry ingest keys for sending traces to ZenPlus APM.
        </p>
      </div>

      {keysQuery.isError && (
        <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load ingest keys — {apiErrorMessage(keysQuery.error)}
        </div>
      )}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-primary" /> Ingest Keys
          </CardTitle>
          <Button onClick={() => setFormOpen(true)}>
            <Plus className="w-4 h-4 mr-1" /> Create ingest key
          </Button>
        </CardHeader>
        <CardContent>
          {keysQuery.isLoading ? (
            <div className="flex items-center gap-2 text-muted py-8 justify-center">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : keys.length === 0 ? (
            <div className="text-center text-muted py-10">
              No ingest keys yet. Create one to start sending OTLP traces.
            </div>
          ) : (
            <Table>
              <THead>
                <Tr>
                  <Th>Name</Th>
                  <Th>Type</Th>
                  <Th>Key prefix</Th>
                  <Th>Environment</Th>
                  <Th>Status</Th>
                  <Th>Created</Th>
                  <Th className="text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {keys.map((k) => (
                  <Tr key={k.id}>
                    <Td className="font-medium text-text">{k.name}</Td>
                    <Td><Badge variant="outline">{k.kind.toUpperCase()}</Badge></Td>
                    <Td className="font-mono text-xs">{k.key_prefix}…</Td>
                    <Td>{k.env ?? <span className="text-muted">all</span>}</Td>
                    <Td>
                      {k.revoked_at || !k.enabled ? (
                        <Badge variant="danger">Revoked</Badge>
                      ) : (
                        <Badge variant="success">Active</Badge>
                      )}
                    </Td>
                    <Td className="text-muted text-xs">
                      {new Date(k.created_at).toLocaleDateString()}
                    </Td>
                    <Td className="text-right">
                      {!k.revoked_at && k.enabled && (
                        <Button variant="ghost" size="sm" onClick={() => setRevoking(k)}>
                          <Trash2 className="w-4 h-4 text-danger" />
                        </Button>
                      )}
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <DataQualityCard />

      <IngestKeyFormDialog
        open={formOpen}
        onOpenChange={setFormOpen}
        onCreated={(plaintext) => { setFormOpen(false); setCreatedKey(plaintext) }}
      />

      <CopyOnceDialog value={createdKey} onClose={() => setCreatedKey(null)} />

      <ConfirmDialog
        open={!!revoking}
        onOpenChange={(o) => !o && setRevoking(null)}
        title="Revoke ingest key"
        description={revoking ? `Revoke "${revoking.name}"? Agents using it will be rejected within 30s and the key cannot be restored.` : ''}
        confirmText="Revoke"
        destructive
        loading={revoke.isPending}
        onConfirm={() => { if (revoking) revoke.mutate(revoking.id) }}
      />
    </div>
  )
}

// ─── Data quality ───────────────────────────────────────────────────────────

interface DataQuality {
  ingest: {
    accepted: number; rejected: number; dropped: number; skewed: number
    flushes: number; reject_rate: number; queue_depth: number | null
    series: { t: string; accepted: number; rejected: number; dropped: number }[]
  }
  services: { name: string; health: string; last_seen_at: string | null; silent_for_s: number | null; reporting: boolean }[]
  agent_forwarders: {
    agent_id: string; hostname: string; agent_status: string
    enabled?: boolean; failed?: boolean; spans_forwarded_1m?: number
    export_errors_1m?: number; spool_depth_spans?: number; last_error?: string
  }[]
  health: 'ok' | 'issues'
  issues: string[]
}

function DqStat({ label, value, tone }: { label: string; value: React.ReactNode; tone?: 'danger' | 'warning' }) {
  return (
    <div className="rounded-lg border border-border bg-surface2/30 px-3 py-2">
      <div className="text-[11px] text-muted">{label}</div>
      <div className={`mt-0.5 text-lg font-semibold tabular-nums ${
        tone === 'danger' ? 'text-danger' : tone === 'warning' ? 'text-warning' : 'text-text'}`}>
        {value}
      </div>
    </div>
  )
}

function DataQualityCard() {
  const q = useQuery<DataQuality>({
    queryKey: ['apm', 'data-quality'],
    queryFn: async () => (await api.get('/apm/data-quality?hours=24')).data,
    refetchInterval: 30000,
  })
  const d = q.data

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldCheck className="w-4 h-4 text-primary" /> Data Quality
        </CardTitle>
        <p className="text-xs text-muted">
          Monitors the telemetry pipeline itself — rejected or clock-skewed spans, flush
          failures, silent services and agent forwarder health — so bad data is caught
          before it corrupts dashboards. Counters cover the last 24 hours.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {q.isError && (
          <div className="rounded-lg border border-danger/30 bg-danger/10 px-4 py-3 text-sm text-danger">
            Failed to load — {apiErrorMessage(q.error)}
          </div>
        )}
        {d && (
          <>
            <div className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-sm ${
              d.health === 'ok'
                ? 'border-success/30 bg-success/10 text-success'
                : 'border-warning/40 bg-warning/10 text-warning'}`}>
              {d.health === 'ok'
                ? <><Check className="h-4 w-4 shrink-0" /> Telemetry pipeline healthy — no data-quality issues in the last 24h.</>
                : <div>{d.issues.map((i, n) => <div key={n}>• {i}</div>)}</div>}
            </div>

            <div className="grid gap-2 sm:grid-cols-5">
              <DqStat label="Spans accepted" value={d.ingest.accepted.toLocaleString()} />
              <DqStat label="Rejected" value={d.ingest.rejected.toLocaleString()}
                tone={d.ingest.rejected ? 'warning' : undefined} />
              <DqStat label="Clock-skewed" value={d.ingest.skewed.toLocaleString()}
                tone={d.ingest.skewed ? 'warning' : undefined} />
              <DqStat label="Dropped (flush)" value={d.ingest.dropped.toLocaleString()}
                tone={d.ingest.dropped ? 'danger' : undefined} />
              <DqStat label="Queue depth" value={d.ingest.queue_depth ?? '—'} />
            </div>

            {d.services.length > 0 && (
              <div>
                <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
                  Service reporting freshness
                </div>
                <div className="flex flex-wrap gap-2">
                  {d.services.map((s) => (
                    <span key={s.name} className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs ${
                      s.reporting
                        ? 'border-border bg-surface2 text-text2'
                        : 'border-danger/40 bg-danger/10 text-danger'}`}>
                      <span className={`h-1.5 w-1.5 rounded-full ${s.reporting ? 'bg-success' : 'bg-danger'}`} />
                      {s.name}
                      <span className="text-muted">
                        {s.silent_for_s == null ? 'never seen'
                          : s.silent_for_s < 120 ? 'live'
                          : `${Math.round(s.silent_for_s / 60)}m ago`}
                      </span>
                    </span>
                  ))}
                </div>
              </div>
            )}

            {d.agent_forwarders.length > 0 && (
              <div>
                <div className="mb-1.5 text-[11px] font-medium uppercase tracking-wide text-muted">
                  Agent APM forwarders
                </div>
                <Table>
                  <THead>
                    <Tr>
                      <Th>Host</Th><Th>State</Th>
                      <Th className="text-right">Spans/min</Th>
                      <Th className="text-right">Export errors</Th>
                      <Th className="text-right">Spool</Th>
                      <Th>Last error</Th>
                    </Tr>
                  </THead>
                  <TBody>
                    {d.agent_forwarders.map((f) => (
                      <Tr key={f.agent_id}>
                        <Td className="font-medium text-text">{f.hostname}</Td>
                        <Td>
                          {f.failed
                            ? <Badge variant="danger">Failed</Badge>
                            : f.enabled
                              ? <Badge variant="success">Forwarding</Badge>
                              : <Badge variant="outline">Off</Badge>}
                        </Td>
                        <Td className="text-right tabular-nums">{f.spans_forwarded_1m ?? '—'}</Td>
                        <Td className={`text-right tabular-nums ${f.export_errors_1m ? 'text-danger' : ''}`}>
                          {f.export_errors_1m ?? '—'}
                        </Td>
                        <Td className="text-right tabular-nums">{f.spool_depth_spans ?? '—'}</Td>
                        <Td className="max-w-[16rem] truncate text-xs text-muted" title={f.last_error || ''}>
                          {f.last_error || '—'}
                        </Td>
                      </Tr>
                    ))}
                  </TBody>
                </Table>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  )
}

function IngestKeyFormDialog({ open, onOpenChange, onCreated }: {
  open: boolean
  onOpenChange: (o: boolean) => void
  onCreated: (plaintext: string) => void
}) {
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [kind, setKind] = useState<'sdk' | 'rum'>('sdk')
  const [env, setEnv] = useState<string>('prod')

  const envs = useQuery<Environment[]>({
    queryKey: ['apm', 'environments'],
    queryFn: async () => (await api.get('/apm/environments')).data,
    enabled: open,
  })

  const create = useMutation({
    mutationFn: async () =>
      (await api.post('/apm/ingest-keys', { name, kind, env })).data,
    onSuccess: (data: { key: string }) => {
      qc.invalidateQueries({ queryKey: ['apm', 'ingest-keys'] })
      setName('')
      onCreated(data.key)
    },
    onError: (e: any) => toast.error('Could not create key', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Create ingest key</DialogTitle></DialogHeader>
        <div className="space-y-4">
          <FormField label="Name" required>
            <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="prod-checkout-service" />
          </FormField>
          <FormField label="Type">
            <Select value={kind} onValueChange={(v) => setKind(v as 'sdk' | 'rum')}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="sdk">SDK / Collector (zpi_)</SelectItem>
                <SelectItem value="rum">Browser RUM (zpr_)</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          <FormField label="Environment">
            <Select value={env} onValueChange={setEnv}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {(envs.data ?? []).map((e) => (
                  <SelectItem key={e.id} value={e.name}>{e.name}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </FormField>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button disabled={!name.trim() || create.isPending} onClick={() => create.mutate()}>
            {create.isPending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
            Create
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CopyOnceDialog({ value, onClose }: { value: string | null; onClose: () => void }) {
  const [copied, setCopied] = useState(false)
  const copy = () => {
    if (value) {
      navigator.clipboard?.writeText(value)
      setCopied(true)
      toast.success('Copied to clipboard')
    }
  }
  return (
    <Dialog open={!!value} onOpenChange={(o) => { if (!o) { setCopied(false); onClose() } }}>
      <DialogContent className="max-w-lg">
        <DialogHeader><DialogTitle>Your new ingest key</DialogTitle></DialogHeader>
        <p className="text-sm text-muted">
          Copy this key now — for security it is shown <strong>only once</strong> and cannot be retrieved again.
        </p>
        <div className="flex items-center gap-2 mt-3">
          <code className="flex-1 px-3 py-2 rounded-md bg-surface2 text-text font-mono text-sm break-all">
            {value}
          </code>
          <Button variant="outline" size="sm" onClick={copy}>
            {copied ? <Check className="w-4 h-4" /> : <Copy className="w-4 h-4" />}
          </Button>
        </div>
        <DialogFooter>
          <Button onClick={() => { setCopied(false); onClose() }}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
