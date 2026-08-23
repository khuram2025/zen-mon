import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import { Factory, Pencil, Plus, Shapes, Trash2 } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Badge } from '@/components/ui/Badge'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Switch } from '@/components/ui/Switch'
import { Skeleton } from '@/components/ui/Skeleton'
import {
  Dialog, DialogContent, DialogHeader, DialogTitle, DialogFooter,
} from '@/components/ui/Dialog'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { toast } from '@/components/ui/Toast'
import { apiErrorMessage } from '@/lib/utils'
import { udtApi } from './api'
import type { UdtClassRule } from './types'
import { endpointTypeMeta } from './helpers'

const MATCH_TYPES = [
  { v: 'mac_prefix', l: 'MAC / OUI prefix' },
  { v: 'mac', l: 'MAC address' },
  { v: 'vendor', l: 'Vendor' },
  { v: 'hostname', l: 'Hostname (glob)' },
  { v: 'ip', l: 'IP address' },
  { v: 'ip_range', l: 'IP range (a - b)' },
  { v: 'subnet', l: 'Subnet (CIDR)' },
  { v: 'user', l: 'User (glob)' },
]

const CUSTOM = '__custom__'
const SLUG_RE = /^[a-z0-9][a-z0-9_-]{0,29}$/

function invalidateClassData(qc: ReturnType<typeof useQueryClient>) {
  qc.invalidateQueries({ queryKey: ['udt', 'class-rules'] })
  qc.invalidateQueries({ queryKey: ['udt', 'types'] })
  qc.invalidateQueries({ queryKey: ['udt', 'endpoints'] })
  qc.invalidateQueries({ queryKey: ['udt', 'vendors'] })
}

function RuleDialog({ rule, preset, onClose }: {
  rule?: UdtClassRule
  preset?: { match_type: string; pattern: string }
  onClose: () => void
}) {
  const qc = useQueryClient()
  const types = useQuery({ queryKey: ['udt', 'types'], queryFn: () => udtApi.types() })
  const [matchType, setMatchType] = useState(rule?.match_type || preset?.match_type || 'mac_prefix')
  const [pattern, setPattern] = useState(rule?.pattern || preset?.pattern || '')
  const knownTypes = (types.data?.data || []).map((t) => t.type)
  const initialSet = rule?.set_type || ''
  const [setType, setSetType] = useState(initialSet && knownTypes.length && !knownTypes.includes(initialSet) ? CUSTOM : initialSet)
  const [customType, setCustomType] = useState(initialSet)
  const [priority, setPriority] = useState(String(rule?.priority ?? 100))
  const [description, setDescription] = useState(rule?.description || '')

  const target = setType === CUSTOM ? customType.trim() : setType
  const prio = Number(priority)
  const valid = pattern.trim() && target && SLUG_RE.test(target) && Number.isInteger(prio) && prio >= 1 && prio <= 10000

  const save = useMutation({
    mutationFn: () => {
      const body = {
        match_type: matchType, pattern: pattern.trim(), set_type: target,
        priority: prio, description: description || null,
      }
      return rule ? udtApi.updateClassRule(rule.id, body) : udtApi.createClassRule(body)
    },
    onSuccess: (res: any) => {
      invalidateClassData(qc)
      toast.success(rule ? 'Rule updated' : 'Rule added',
        res?.endpoints_updated ? `${res.endpoints_updated} endpoint(s) reclassified` : undefined)
      onClose()
    },
    onError: (e: any) => toast.error('Could not save rule', apiErrorMessage(e)),
  })

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>{rule ? 'Edit' : 'Add'} classification rule</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="mb-1 block text-xs text-muted">Match on</label>
              <Select value={matchType} onValueChange={setMatchType}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {MATCH_TYPES.map((m) => <SelectItem key={m.v} value={m.v}>{m.l}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted">Priority (lower wins)</label>
              <Input type="number" min={1} max={10000} value={priority} onChange={(e) => setPriority(e.target.value)} />
            </div>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Pattern</label>
            <Input value={pattern} onChange={(e) => setPattern(e.target.value)}
              placeholder={
                matchType === 'mac_prefix' ? '00:17:23 or 001723'
                  : matchType === 'subnet' ? '192.168.10.0/24'
                  : matchType === 'ip_range' ? '10.0.0.10 - 10.0.0.50'
                  : matchType === 'vendor' ? 'Suprema*'
                  : matchType === 'hostname' ? 'cam-*' : ''
              } />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Assign to group / type</label>
            <Select value={setType} onValueChange={setSetType}>
              <SelectTrigger><SelectValue placeholder="Choose a group…" /></SelectTrigger>
              <SelectContent>
                {(types.data?.data || []).filter((t) => t.type !== 'unknown').map((t) => (
                  <SelectItem key={t.type} value={t.type}>{endpointTypeMeta(t.type).label}</SelectItem>
                ))}
                <SelectItem value={CUSTOM}>New custom group…</SelectItem>
              </SelectContent>
            </Select>
            {setType === CUSTOM && (
              <Input className="mt-2" value={customType} onChange={(e) => setCustomType(e.target.value.toLowerCase())}
                placeholder="e.g. access-control (lowercase, digits, - _)" />
            )}
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Description (optional)</label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="e.g. Biometric door readers" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!valid || save.isPending} onClick={() => save.mutate()}>
            {rule ? 'Save' : 'Add rule'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function GroupsStrip() {
  const navigate = useNavigate()
  const types = useQuery({ queryKey: ['udt', 'types'], queryFn: () => udtApi.types() })
  const rows = (types.data?.data || []).filter((t) => t.count > 0)
  return (
    <div className="flex flex-wrap gap-2">
      {rows.map((t) => {
        const m = endpointTypeMeta(t.type)
        return (
          <button key={t.type}
            onClick={() => navigate(`/udt?type=${encodeURIComponent(t.type)}`)}
            className="flex items-center gap-2 rounded-lg border border-border bg-surface px-3 py-2 text-left transition-colors hover:border-border-strong">
            <m.icon className="h-4 w-4 text-primary" />
            <span className="text-sm">{m.label}</span>
            <span className="text-sm font-semibold tabular-nums">{t.count}</span>
            {!t.builtin && <Badge variant="outline">custom</Badge>}
          </button>
        )
      })}
    </div>
  )
}

function RulesCard() {
  const qc = useQueryClient()
  const [dialog, setDialog] = useState<null | { rule?: UdtClassRule }>(null)
  const rules = useQuery({ queryKey: ['udt', 'class-rules'], queryFn: () => udtApi.classRules() })

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => udtApi.updateClassRule(id, { enabled }),
    onSuccess: () => invalidateClassData(qc),
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })
  const del = useMutation({
    mutationFn: (id: string) => udtApi.deleteClassRule(id),
    onSuccess: () => { invalidateClassData(qc); toast.success('Rule removed') },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const data = rules.data?.data || []
  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-start justify-between gap-4 border-b border-border p-4">
          <div>
            <h3 className="flex items-center gap-2 text-sm font-semibold">
              <Shapes className="h-4 w-4 text-primary" /> Classification rules
            </h3>
            <p className="mt-0.5 text-xs text-muted">
              Override the automatic vendor/hostname classification. The lowest-priority matching
              rule assigns the endpoint's group; manually pinned endpoints are never changed.
            </p>
          </div>
          <Button size="sm" variant="outline" onClick={() => setDialog({})}><Plus className="mr-1 h-4 w-4" />Add rule</Button>
        </div>
        {rules.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 2 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
        ) : data.length === 0 ? (
          <div className="py-8 text-center text-xs text-muted">
            No rules yet. Add one, e.g. “MAC prefix 00:17:23 → access-control”.
          </div>
        ) : (
          <Table>
            <THead className="bg-surface2/40">
              <Tr>
                <Th className="w-16">Priority</Th><Th>Match</Th><Th>Pattern</Th><Th>Group</Th>
                <Th className="text-right">Matches</Th><Th>Description</Th><Th>Enabled</Th>
                <Th className="text-right">Actions</Th>
              </Tr>
            </THead>
            <TBody>
              {data.map((r) => {
                const m = endpointTypeMeta(r.set_type)
                return (
                  <Tr key={r.id}>
                    <Td className="text-xs tabular-nums text-muted">{r.priority}</Td>
                    <Td className="text-xs text-muted">{MATCH_TYPES.find((t) => t.v === r.match_type)?.l || r.match_type}</Td>
                    <Td className="font-mono text-xs">{r.pattern}</Td>
                    <Td><span className="inline-flex items-center gap-1 text-xs"><m.icon className="h-3.5 w-3.5 text-primary" />{m.label}</span></Td>
                    <Td className="text-right text-xs tabular-nums">{r.match_count}</Td>
                    <Td className="max-w-[200px] truncate text-xs text-muted">{r.description || '—'}</Td>
                    <Td><Switch checked={r.enabled} onCheckedChange={(v) => toggle.mutate({ id: r.id, enabled: v })} /></Td>
                    <Td className="text-right">
                      <button className="rounded p-1 text-muted hover:bg-surface2 hover:text-text" onClick={() => setDialog({ rule: r })}>
                        <Pencil className="h-4 w-4" />
                      </button>
                      <button className="rounded p-1 text-danger hover:bg-danger/10" onClick={() => del.mutate(r.id)}>
                        <Trash2 className="h-4 w-4" />
                      </button>
                    </Td>
                  </Tr>
                )
              })}
            </TBody>
          </Table>
        )}
      </CardContent>
      {dialog && <RuleDialog rule={dialog.rule} onClose={() => setDialog(null)} />}
    </Card>
  )
}

function VendorsCard() {
  const navigate = useNavigate()
  const [preset, setPreset] = useState<null | { match_type: string; pattern: string }>(null)
  const vendors = useQuery({ queryKey: ['udt', 'vendors'], queryFn: () => udtApi.vendors() })
  const data = vendors.data?.data || []
  return (
    <Card>
      <CardContent className="p-0">
        <div className="border-b border-border p-4">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <Factory className="h-4 w-4 text-primary" /> NIC vendors (OUI)
          </h3>
          <p className="mt-0.5 text-xs text-muted">
            MAC-derived hardware vendor per endpoint. Use “Rule” to group everything from a vendor.
          </p>
        </div>
        {vendors.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
        ) : (
          <Table>
            <THead className="bg-surface2/40">
              <Tr><Th>Vendor</Th><Th className="text-right">Endpoints</Th><Th>Current groups</Th><Th className="text-right">Actions</Th></Tr>
            </THead>
            <TBody>
              {data.slice(0, 50).map((v) => (
                <Tr key={v.vendor}>
                  <Td className="max-w-[260px] truncate text-sm" title={v.vendor}>
                    <button className="hover:text-primary hover:underline"
                      onClick={() => navigate(`/udt?q=${encodeURIComponent(v.vendor)}`)}
                      disabled={v.vendor === 'Unknown'}>
                      {v.vendor}
                    </button>
                  </Td>
                  <Td className="text-right text-sm tabular-nums">{v.count}</Td>
                  <Td>
                    <span className="flex flex-wrap gap-1">
                      {Object.entries(v.types).sort((a, b) => b[1] - a[1]).slice(0, 4).map(([t, n]) => (
                        <Badge key={t} variant={t === 'unknown' ? 'outline' : 'default'}>
                          {endpointTypeMeta(t).label} · {n}
                        </Badge>
                      ))}
                    </span>
                  </Td>
                  <Td className="text-right">
                    {v.vendor !== 'Unknown' && (
                      <Button size="sm" variant="ghost"
                        onClick={() => setPreset({ match_type: 'vendor', pattern: v.vendor })}>
                        <Plus className="mr-1 h-3.5 w-3.5" />Rule
                      </Button>
                    )}
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
      {preset && <RuleDialog preset={preset} onClose={() => setPreset(null)} />}
    </Card>
  )
}

export function ClassificationPage() {
  return (
    <div className="space-y-4">
      <GroupsStrip />
      <RulesCard />
      <VendorsCard />
    </div>
  )
}
