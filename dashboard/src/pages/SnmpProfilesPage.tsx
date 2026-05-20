import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ChevronDown,
  ChevronRight,
  Copy,
  Key,
  Loader2,
  Pencil,
  Plus,
  Save,
  Shield,
  ShieldCheck,
  Star,
  Trash2,
  X,
} from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, relativeTime } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/Select'
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'

/* ── Types ─────────────────────────────────────────────────────── */

type Credential = {
  id: string
  name: string
  description: string | null
  snmp_version: string
  community: string | null
  v3_username: string | null
  v3_context: string | null
  v3_security_level: string | null
  v3_auth_protocol: string | null
  has_auth_passphrase: boolean
  v3_priv_protocol: string | null
  has_priv_passphrase: boolean
  port: number
  timeout_ms: number
  retries: number
  is_default: boolean
  device_count: number
  group_count: number
  created_at: string
  updated_at: string
}

type Usage = {
  devices: { id: string; hostname: string; ip_address: string }[]
  groups: { id: string; name: string }[]
}

/* ── Version label helper ────────────────────────────────────── */

function VersionBadge({ ver }: { ver: string }) {
  const colors: Record<string, string> = { '1': 'outline', '2c': 'info', '3': 'success' }
  return <Badge variant={(colors[ver] || 'outline') as any}>v{ver}</Badge>
}

function SecuritySummary({ cred }: { cred: Credential }) {
  if (cred.snmp_version !== '3') {
    return <span className="text-xs text-muted">{cred.community ? 'Community set' : 'No community'}</span>
  }
  const level = cred.v3_security_level || 'authPriv'
  const icons: Record<string, typeof Shield> = {
    noAuthNoPriv: Shield,
    authNoPriv: ShieldCheck,
    authPriv: ShieldCheck,
  }
  const Icon = icons[level] || Shield
  return (
    <div className="flex items-center gap-1.5">
      <Icon className="h-3.5 w-3.5 text-success" />
      <span className="text-xs">
        {cred.v3_username}
        {cred.v3_auth_protocol && <span className="text-muted"> · {cred.v3_auth_protocol}</span>}
        {cred.v3_priv_protocol && <span className="text-muted">/{cred.v3_priv_protocol}</span>}
      </span>
    </div>
  )
}

/* ── Main Page ───────────────────────────────────────────────── */

export function SnmpProfilesPage({ hideHeader = false }: { hideHeader?: boolean } = {}) {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<Credential | null>(null)
  const [deleting, setDeleting] = useState<Credential | null>(null)
  const [expandedId, setExpandedId] = useState<string | null>(null)

  const { data: credentials, isLoading } = useQuery<Credential[]>({
    queryKey: ['snmp-credentials'],
    queryFn: async () => (await api.get('/snmp-credentials')).data,
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/snmp-credentials/${id}`),
    onSuccess: () => {
      toast.success('Credential deleted')
      qc.invalidateQueries({ queryKey: ['snmp-credentials'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      {hideHeader ? (
        <div className="flex items-center justify-end">
          <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
            <Plus className="h-4 w-4" /> Add Credential
          </Button>
        </div>
      ) : (
        <div className="flex items-center justify-between">
          <div>
            <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
              <Key className="h-5 w-5 text-primary" /> SNMP Credentials
            </h1>
            <p className="text-xs text-muted">
              Manage reusable SNMP credentials for devices, groups, and discovery
              {credentials ? ` · ${credentials.length} credential${credentials.length !== 1 ? 's' : ''}` : ''}
            </p>
          </div>
          <Button onClick={() => { setEditing(null); setFormOpen(true) }}>
            <Plus className="h-4 w-4" /> Add Credential
          </Button>
        </div>
      )}

      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr>
                <Th className="w-8" />
                <Th>Name</Th>
                <Th>Version</Th>
                <Th>Authentication</Th>
                <Th>Port</Th>
                <Th>Assigned</Th>
                <Th className="w-32 text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {isLoading && (
                <Tr><Td colSpan={7} className="py-12 text-center text-muted"><Loader2 className="mx-auto h-5 w-5 animate-spin" /></Td></Tr>
              )}
              {credentials?.map((c) => {
                const isExpanded = expandedId === c.id
                const totalAssigned = c.device_count + c.group_count
                return (
                  <Tr key={c.id} className="group">
                    <Td className="w-8 pr-0">
                      <button onClick={() => setExpandedId(isExpanded ? null : c.id)} className="rounded p-0.5 text-muted hover:text-text">
                        {isExpanded ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
                      </button>
                    </Td>
                    <Td>
                      <div className="flex items-center gap-2">
                        <span className="font-medium">{c.name}</span>
                        {c.is_default && (
                          <span className="flex items-center gap-0.5 rounded bg-warning/10 px-1.5 py-0.5 text-[10px] font-semibold text-warning">
                            <Star className="h-3 w-3" /> DEFAULT
                          </span>
                        )}
                      </div>
                      {c.description && <div className="mt-0.5 max-w-xs truncate text-xs text-muted">{c.description}</div>}
                    </Td>
                    <Td><VersionBadge ver={c.snmp_version} /></Td>
                    <Td><SecuritySummary cred={c} /></Td>
                    <Td><span className="font-mono text-xs">{c.port}</span></Td>
                    <Td>
                      {totalAssigned > 0 ? (
                        <div className="flex gap-2 text-xs">
                          {c.device_count > 0 && <Badge variant="info">{c.device_count} device{c.device_count !== 1 ? 's' : ''}</Badge>}
                          {c.group_count > 0 && <Badge variant="outline">{c.group_count} group{c.group_count !== 1 ? 's' : ''}</Badge>}
                        </div>
                      ) : (
                        <span className="text-xs text-muted">—</span>
                      )}
                    </Td>
                    <Td>
                      <div className="flex justify-end gap-0.5 opacity-0 transition-opacity group-hover:opacity-100">
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Duplicate" onClick={() => { setEditing({ ...c, id: '', name: `${c.name} (copy)`, is_default: false } as any); setFormOpen(true) }}>
                          <Copy className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7" title="Edit" onClick={() => { setEditing(c); setFormOpen(true) }}>
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button variant="ghost" size="icon" className="h-7 w-7 text-muted hover:text-danger" title="Delete" onClick={() => setDeleting(c)}>
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                )
              })}
              {!isLoading && (!credentials || credentials.length === 0) && (
                <Tr>
                  <Td colSpan={7} className="py-12 text-center">
                    <Key className="mx-auto mb-2 h-8 w-8 text-muted/40" />
                    <div className="text-sm text-muted">No SNMP credentials</div>
                    <div className="text-xs text-muted">Add a credential to start monitoring devices via SNMP</div>
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>

          {expandedId && credentials && <ExpandedCredential cred={credentials.find((c) => c.id === expandedId)!} />}
        </CardContent>
      </Card>

      <CredentialFormDialog open={formOpen} onOpenChange={setFormOpen} credential={editing} />

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title="Delete credential"
        description={
          <>
            Delete <span className="font-semibold text-text">{deleting?.name}</span>?
            {((deleting?.device_count || 0) + (deleting?.group_count || 0)) > 0 && (
              <span className="mt-1 block text-xs text-warning">
                {deleting?.device_count || 0} device(s) and {deleting?.group_count || 0} group(s) will be unlinked.
              </span>
            )}
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => { if (deleting) del.mutate(deleting.id) }}
      />
    </div>
  )
}

/* ── Expanded row ────────────────────────────────────────────── */

function ExpandedCredential({ cred }: { cred: Credential }) {
  const { data: usage } = useQuery<Usage>({
    queryKey: ['snmp-credentials', cred.id, 'usage'],
    queryFn: async () => (await api.get(`/snmp-credentials/${cred.id}/usage`)).data,
    enabled: !!cred.id,
  })

  return (
    <div className="border-t border-border bg-surface2/30 px-6 py-4 animate-fade-in">
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-3">
        {/* Connection details */}
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Connection</h3>
          <div className="space-y-1 text-sm">
            <div><span className="text-muted">Port:</span> {cred.port}</div>
            <div><span className="text-muted">Timeout:</span> {cred.timeout_ms}ms</div>
            <div><span className="text-muted">Retries:</span> {cred.retries}</div>
          </div>
        </div>

        {/* Auth details */}
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Authentication</h3>
          <div className="space-y-1 text-sm">
            {cred.snmp_version !== '3' ? (
              <>
                <div><span className="text-muted">Community:</span> {cred.community ? '••••••••' : <span className="text-danger">Not set</span>}</div>
              </>
            ) : (
              <>
                <div><span className="text-muted">Username:</span> {cred.v3_username || '—'}</div>
                <div><span className="text-muted">Security:</span> {cred.v3_security_level || 'authPriv'}</div>
                <div><span className="text-muted">Auth:</span> {cred.v3_auth_protocol || '—'} {cred.has_auth_passphrase ? '(set)' : <span className="text-danger">(not set)</span>}</div>
                <div><span className="text-muted">Privacy:</span> {cred.v3_priv_protocol || '—'} {cred.has_priv_passphrase ? '(set)' : cred.v3_security_level === 'authPriv' ? <span className="text-danger">(not set)</span> : '—'}</div>
                {cred.v3_context && <div><span className="text-muted">Context:</span> {cred.v3_context}</div>}
              </>
            )}
          </div>
        </div>

        {/* Usage */}
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wider text-muted">Assigned To</h3>
          {!usage ? (
            <Loader2 className="h-4 w-4 animate-spin text-muted" />
          ) : (usage.devices.length + usage.groups.length) === 0 ? (
            <div className="text-xs text-muted">Not assigned to any device or group</div>
          ) : (
            <div className="space-y-1 text-sm">
              {usage.groups.map((g) => (
                <div key={g.id} className="flex items-center gap-1.5">
                  <Badge variant="outline" className="text-[10px]">GROUP</Badge> {g.name}
                </div>
              ))}
              {usage.devices.slice(0, 10).map((d) => (
                <div key={d.id} className="flex items-center gap-1.5 text-xs">
                  <span className="font-medium">{d.hostname}</span>
                  <span className="text-muted">{d.ip_address}</span>
                </div>
              ))}
              {usage.devices.length > 10 && (
                <div className="text-xs text-muted">...and {usage.devices.length - 10} more</div>
              )}
            </div>
          )}
        </div>
      </div>
      <div className="mt-3 text-xs text-muted">
        Created {relativeTime(cred.created_at)} · Updated {relativeTime(cred.updated_at)}
      </div>
    </div>
  )
}

/* ── Form dialog ─────────────────────────────────────────────── */

function CredentialFormDialog({
  open,
  onOpenChange,
  credential,
}: {
  open: boolean
  onOpenChange: (o: boolean) => void
  credential: Credential | null
}) {
  const isDuplicate = credential && !credential.id
  const isEdit = credential && !!credential.id
  const qc = useQueryClient()

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [version, setVersion] = useState('2c')
  const [community, setCommunity] = useState('')
  const [v3User, setV3User] = useState('')
  const [v3Context, setV3Context] = useState('')
  const [v3SecLevel, setV3SecLevel] = useState('authPriv')
  const [v3AuthProto, setV3AuthProto] = useState('')
  const [v3AuthPw, setV3AuthPw] = useState('')
  const [v3PrivProto, setV3PrivProto] = useState('')
  const [v3PrivPw, setV3PrivPw] = useState('')
  const [port, setPort] = useState(161)
  const [timeout, setTimeout_] = useState(2000)
  const [retries, setRetries] = useState(2)
  const [isDefault, setIsDefault] = useState(false)

  // PasswordInput owns its own reveal state per-field. We only track the
  // shared "currently fetching stored secrets" flag here so multiple
  // reveal clicks during the lazy-fetch all share one spinner.
  const [secretsFetched, setSecretsFetched] = useState(false)
  const [revealing, setRevealing] = useState(false)

  useEffect(() => {
    if (!open) return
    if (credential) {
      setName(credential.name || '')
      setDescription(credential.description || '')
      setVersion(credential.snmp_version || '2c')
      setCommunity(credential.community || '')
      setV3User(credential.v3_username || '')
      setV3Context(credential.v3_context || '')
      setV3SecLevel(credential.v3_security_level || 'authPriv')
      setV3AuthProto(credential.v3_auth_protocol || '')
      setV3AuthPw('')
      setV3PrivProto(credential.v3_priv_protocol || '')
      setV3PrivPw('')
      setPort(credential.port || 161)
      setTimeout_(credential.timeout_ms || 2000)
      setRetries(credential.retries || 2)
      setIsDefault(credential.is_default || false)
    } else {
      setName(''); setDescription(''); setVersion('2c'); setCommunity('public')
      setV3User(''); setV3Context(''); setV3SecLevel('authPriv')
      setV3AuthProto('SHA256'); setV3AuthPw(''); setV3PrivProto('AES128'); setV3PrivPw('')
      setPort(161); setTimeout_(2000); setRetries(2); setIsDefault(false)
    }
    // Reset stored-secret lazy-fetch state for each open
    setSecretsFetched(false)
  }, [open, credential])

  // Lazy-fetch stored plaintext secrets when the user first asks to reveal one
  // in edit mode. Populates the corresponding input(s) so the user can see and
  // tweak what was previously saved.
  const fetchSecrets = async () => {
    if (!isEdit || !credential?.id || secretsFetched) return
    try {
      setRevealing(true)
      const { data } = await api.get<{
        community: string | null
        v3_auth_passphrase: string | null
        v3_priv_passphrase: string | null
      }>(`/snmp-credentials/${credential.id}/secrets`)
      if (data.community != null) setCommunity(data.community)
      if (data.v3_auth_passphrase != null) setV3AuthPw(data.v3_auth_passphrase)
      if (data.v3_priv_passphrase != null) setV3PrivPw(data.v3_priv_passphrase)
      setSecretsFetched(true)
    } catch (e: any) {
      toast.error('Could not load secrets', apiErrorMessage(e))
    } finally {
      setRevealing(false)
    }
  }

  const save = useMutation({
    mutationFn: async () => {
      const payload: any = {
        name, description: description || null, snmp_version: version,
        port, timeout_ms: timeout, retries, is_default: isDefault,
      }
      if (version === '1' || version === '2c') {
        payload.community = community || null
      } else {
        payload.v3_username = v3User || null
        payload.v3_context = v3Context || null
        payload.v3_security_level = v3SecLevel
        payload.v3_auth_protocol = v3AuthProto || null
        if (v3AuthPw) payload.v3_auth_passphrase = v3AuthPw
        payload.v3_priv_protocol = v3PrivProto || null
        if (v3PrivPw) payload.v3_priv_passphrase = v3PrivPw
      }
      if (isEdit) return (await api.put(`/snmp-credentials/${credential!.id}`, payload)).data
      return (await api.post('/snmp-credentials', payload)).data
    },
    onSuccess: () => {
      toast.success(isEdit ? 'Credential updated' : 'Credential created')
      qc.invalidateQueries({ queryKey: ['snmp-credentials'] })
      onOpenChange(false)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-lg overflow-y-auto">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit Credential' : isDuplicate ? 'Duplicate Credential' : 'New SNMP Credential'}</DialogTitle>
        </DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-4">

          {/* Basic */}
          <FormField label="Credential name" required>
            <Input required value={name} onChange={(e) => setName(e.target.value)} placeholder="e.g. Core Switches - SNMPv3" />
          </FormField>
          <FormField label="Description">
            <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional description" />
          </FormField>

          {/* Version selector */}
          <FormField label="SNMP Version">
            <div className="grid grid-cols-3 gap-2">
              {(['1', '2c', '3'] as const).map((v) => (
                <button
                  key={v}
                  type="button"
                  onClick={() => setVersion(v)}
                  className={`rounded-lg border-2 px-3 py-2.5 text-center text-sm font-medium transition ${
                    version === v ? 'border-primary bg-primary/5 text-primary' : 'border-border text-text2 hover:border-border-strong'
                  }`}
                >
                  <div className="text-base font-bold">v{v}</div>
                  <div className="text-[10px] text-muted">
                    {v === '1' ? 'Legacy' : v === '2c' ? 'Community' : 'USM Auth'}
                  </div>
                </button>
              ))}
            </div>
          </FormField>

          {/* v1/v2c: Community */}
          {(version === '1' || version === '2c') && (
            <FormField label="Community string" required>
              <PasswordInput
                required
                value={community}
                onChange={(e) => setCommunity(e.target.value)}
                placeholder="public"
                hasStored={!!isEdit}
                revealing={revealing}
                onReveal={isEdit ? fetchSecrets : undefined}
              />
            </FormField>
          )}

          {/* v3: USM credentials */}
          {version === '3' && (
            <div className="space-y-3 rounded-lg border border-border p-4">
              <h3 className="text-sm font-semibold">SNMPv3 USM Credentials</h3>

              <FormField label="Username" required>
                <Input required value={v3User} onChange={(e) => setV3User(e.target.value)} placeholder="snmpuser" />
              </FormField>

              <FormField label="Security level">
                <Select value={v3SecLevel} onValueChange={setV3SecLevel}>
                  <SelectTrigger><SelectValue /></SelectTrigger>
                  <SelectContent>
                    <SelectItem value="noAuthNoPriv">noAuthNoPriv (no security)</SelectItem>
                    <SelectItem value="authNoPriv">authNoPriv (auth only)</SelectItem>
                    <SelectItem value="authPriv">authPriv (auth + encryption)</SelectItem>
                  </SelectContent>
                </Select>
              </FormField>

              {v3SecLevel !== 'noAuthNoPriv' && (
                <>
                  <div className="grid grid-cols-2 gap-3">
                    <FormField label="Auth protocol">
                      <Select value={v3AuthProto} onValueChange={setV3AuthProto}>
                        <SelectTrigger><SelectValue placeholder="Select..." /></SelectTrigger>
                        <SelectContent>
                          {['MD5', 'SHA', 'SHA224', 'SHA256', 'SHA384', 'SHA512'].map((p) => (
                            <SelectItem key={p} value={p}>{p}</SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </FormField>
                    <FormField label="Auth passphrase">
                      <PasswordInput
                        value={v3AuthPw}
                        onChange={(e) => setV3AuthPw(e.target.value)}
                        placeholder="Min 8 chars"
                        hasStored={!!isEdit && !!credential?.has_auth_passphrase}
                        revealing={revealing}
                        onReveal={isEdit && credential?.has_auth_passphrase ? fetchSecrets : undefined}
                      />
                    </FormField>
                  </div>
                </>
              )}

              {v3SecLevel === 'authPriv' && (
                <div className="grid grid-cols-2 gap-3">
                  <FormField label="Privacy protocol">
                    <Select value={v3PrivProto} onValueChange={setV3PrivProto}>
                      <SelectTrigger><SelectValue placeholder="Select..." /></SelectTrigger>
                      <SelectContent>
                        {['DES', '3DES', 'AES', 'AES128', 'AES192', 'AES256'].map((p) => (
                          <SelectItem key={p} value={p}>{p}</SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </FormField>
                  <FormField label="Privacy passphrase">
                    <PasswordInput
                      value={v3PrivPw}
                      onChange={(e) => setV3PrivPw(e.target.value)}
                      placeholder="Min 8 chars"
                      hasStored={!!isEdit && !!credential?.has_priv_passphrase}
                      revealing={revealing}
                      onReveal={isEdit && credential?.has_priv_passphrase ? fetchSecrets : undefined}
                    />
                  </FormField>
                </div>
              )}

              <FormField label="Context name" hint="Optional — leave blank for default context">
                <Input value={v3Context} onChange={(e) => setV3Context(e.target.value)} />
              </FormField>
            </div>
          )}

          {/* Connection settings */}
          <div className="rounded-lg border border-border p-4">
            <h3 className="mb-3 text-sm font-semibold">Connection Settings</h3>
            <div className="grid grid-cols-3 gap-3">
              <FormField label="Port">
                <Input type="number" value={port} onChange={(e) => setPort(Number(e.target.value) || 161)} min={1} max={65535} />
              </FormField>
              <FormField label="Timeout (ms)">
                <Input type="number" value={timeout} onChange={(e) => setTimeout_(Number(e.target.value) || 2000)} min={200} max={30000} step={100} />
              </FormField>
              <FormField label="Retries">
                <Input type="number" value={retries} onChange={(e) => setRetries(Number(e.target.value) || 0)} min={0} max={10} />
              </FormField>
            </div>
          </div>

          {/* Default toggle */}
          <div className="flex items-center justify-between rounded-md border border-border px-4 py-3">
            <div>
              <div className="text-sm font-medium">Set as default</div>
              <div className="text-xs text-muted">Auto-applied to new devices when no specific credential is set</div>
            </div>
            <Switch checked={isDefault} onCheckedChange={setIsDefault} />
          </div>

          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending || !name.trim()}>
              {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              <Save className="h-4 w-4" />
              {isEdit ? 'Save Changes' : 'Create Credential'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
