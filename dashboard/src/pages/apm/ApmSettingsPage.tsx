import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Copy, Check, KeyRound, Loader2 } from 'lucide-react'
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
    <div className="p-6 space-y-6">
      <div>
        <h1 className="text-2xl font-semibold text-[var(--text-primary)]">APM Settings</h1>
        <p className="text-sm text-[var(--text-muted)] mt-1">
          Manage OpenTelemetry ingest keys for sending traces to ZenPlus APM.
        </p>
      </div>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="w-4 h-4 text-[var(--accent)]" /> Ingest Keys
          </CardTitle>
          <Button onClick={() => setFormOpen(true)}>
            <Plus className="w-4 h-4 mr-1" /> Create ingest key
          </Button>
        </CardHeader>
        <CardContent>
          {keysQuery.isLoading ? (
            <div className="flex items-center gap-2 text-[var(--text-muted)] py-8 justify-center">
              <Loader2 className="w-4 h-4 animate-spin" /> Loading…
            </div>
          ) : keys.length === 0 ? (
            <div className="text-center text-[var(--text-muted)] py-10">
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
                    <Td className="font-medium text-[var(--text-primary)]">{k.name}</Td>
                    <Td><Badge variant="outline">{k.kind.toUpperCase()}</Badge></Td>
                    <Td className="font-mono text-xs">{k.key_prefix}…</Td>
                    <Td>{k.env ?? <span className="text-[var(--text-muted)]">all</span>}</Td>
                    <Td>
                      {k.revoked_at || !k.enabled ? (
                        <Badge variant="danger">Revoked</Badge>
                      ) : (
                        <Badge variant="success">Active</Badge>
                      )}
                    </Td>
                    <Td className="text-[var(--text-muted)] text-xs">
                      {new Date(k.created_at).toLocaleDateString()}
                    </Td>
                    <Td className="text-right">
                      {!k.revoked_at && k.enabled && (
                        <Button variant="ghost" size="sm" onClick={() => setRevoking(k)}>
                          <Trash2 className="w-4 h-4 text-[var(--danger)]" />
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
        <p className="text-sm text-[var(--text-muted)]">
          Copy this key now — for security it is shown <strong>only once</strong> and cannot be retrieved again.
        </p>
        <div className="flex items-center gap-2 mt-3">
          <code className="flex-1 px-3 py-2 rounded-md bg-[var(--bg-tertiary)] text-[var(--text-primary)] font-mono text-sm break-all">
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
