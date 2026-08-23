import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Eye, Plus, ShieldAlert, ShieldCheck, SlashSquare, Trash2 } from 'lucide-react'
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
import type { Endpoint, UdtRule } from './types'
import { macCol, relTime } from './helpers'
import { KbLink } from '@/components/udt/KbLink'

const LIST_META = {
  allow: { label: 'Allow list', desc: 'Endpoints matching these rules are authorized. Anything else that appears becomes a rogue.', icon: ShieldCheck, tone: 'text-success' },
  watch: { label: 'Watch list', desc: 'Raise an alert whenever a matching endpoint appears on the network.', icon: Eye, tone: 'text-warning' },
  ignore: { label: 'Ignore list', desc: 'Matching endpoints are hidden from all UDT views and never flagged.', icon: SlashSquare, tone: 'text-muted' },
} as const

const MATCH_TYPES = [
  { v: 'mac', l: 'MAC address' },
  { v: 'mac_prefix', l: 'MAC / OUI prefix' },
  { v: 'ip', l: 'IP address' },
  { v: 'ip_range', l: 'IP range (a - b)' },
  { v: 'subnet', l: 'Subnet (CIDR)' },
  { v: 'hostname', l: 'Hostname (glob)' },
  { v: 'vendor', l: 'Vendor' },
  { v: 'user', l: 'User (glob)' },
]

function AddRuleDialog({ listType, onClose }: { listType: 'allow' | 'watch' | 'ignore'; onClose: () => void }) {
  const qc = useQueryClient()
  const [matchType, setMatchType] = useState('mac')
  const [pattern, setPattern] = useState('')
  const [description, setDescription] = useState('')

  const create = useMutation({
    mutationFn: () => udtApi.createRule({ list_type: listType, match_type: matchType, pattern, description }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['udt', 'rules'] })
      qc.invalidateQueries({ queryKey: ['udt', 'summary'] })
      toast.success('Rule added')
      onClose()
    },
    onError: (e: any) => toast.error('Could not add rule', apiErrorMessage(e)),
  })

  return (
    <Dialog open onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader><DialogTitle>Add {LIST_META[listType].label} rule</DialogTitle></DialogHeader>
        <div className="space-y-3 py-2">
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
            <label className="mb-1 block text-xs text-muted">Pattern</label>
            <Input value={pattern} onChange={(e) => setPattern(e.target.value)}
              placeholder={
                matchType === 'subnet' ? '192.168.10.0/24'
                  : matchType === 'ip_range' ? '10.0.0.10 - 10.0.0.50'
                  : matchType === 'mac_prefix' ? 'f8:bc:12 or f8bc12'
                  : matchType === 'hostname' ? 'ws-*' : ''
              } />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">Description (optional)</label>
            <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="e.g. Corporate laptops" />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>Cancel</Button>
          <Button disabled={!pattern.trim() || create.isPending} onClick={() => create.mutate()}>Add rule</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function RuleTable({ listType }: { listType: 'allow' | 'watch' | 'ignore' }) {
  const qc = useQueryClient()
  const [adding, setAdding] = useState(false)
  const rules = useQuery({ queryKey: ['udt', 'rules', listType], queryFn: () => udtApi.rules(listType) })
  const meta = LIST_META[listType]

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) => udtApi.updateRule(id, { enabled }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['udt', 'rules'] }),
    onError: (e: any) => toast.error('Update failed', apiErrorMessage(e)),
  })
  const del = useMutation({
    mutationFn: (id: string) => udtApi.deleteRule(id),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['udt', 'rules'] }); qc.invalidateQueries({ queryKey: ['udt', 'summary'] }); toast.success('Rule removed') },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  return (
    <Card>
      <CardContent className="p-0">
        <div className="flex items-start justify-between gap-4 border-b border-border p-4">
          <div>
            <h3 className={`flex items-center gap-2 text-sm font-semibold`}>
              <meta.icon className={`h-4 w-4 ${meta.tone}`} /> {meta.label}
            </h3>
            <p className="mt-0.5 text-xs text-muted">{meta.desc}</p>
          </div>
          <Button size="sm" variant="outline" onClick={() => setAdding(true)}><Plus className="mr-1 h-4 w-4" />Add rule</Button>
        </div>
        {rules.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 2 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
        ) : (rules.data?.data.length || 0) === 0 ? (
          <div className="py-8 text-center text-xs text-muted">No rules yet.</div>
        ) : (
          <Table>
            <THead className="bg-surface2/40">
              <Tr><Th>Match</Th><Th>Pattern</Th><Th>Description</Th><Th>Enabled</Th><Th className="text-right">Actions</Th></Tr>
            </THead>
            <TBody>
              {rules.data!.data.map((r: UdtRule) => (
                <Tr key={r.id}>
                  <Td className="text-xs text-muted">{MATCH_TYPES.find((m) => m.v === r.match_type)?.l || r.match_type}</Td>
                  <Td className="font-mono text-xs">{r.pattern}</Td>
                  <Td className="text-xs text-muted">{r.description || '—'}</Td>
                  <Td><Switch checked={r.enabled} onCheckedChange={(v) => toggle.mutate({ id: r.id, enabled: v })} /></Td>
                  <Td className="text-right">
                    <button className="rounded p-1 text-danger hover:bg-danger/10" onClick={() => del.mutate(r.id)}>
                      <Trash2 className="h-4 w-4" />
                    </button>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
      {adding && <AddRuleDialog listType={listType} onClose={() => setAdding(false)} />}
    </Card>
  )
}

function RoguesCard() {
  const rogues = useQuery({ queryKey: ['udt', 'rogues'], queryFn: () => udtApi.rogues(), refetchInterval: 20_000 })
  const data = rogues.data?.data || []
  return (
    <Card>
      <CardContent className="p-0">
        <h3 className="flex items-center gap-2 border-b border-border px-4 py-3 text-sm font-semibold">
          <ShieldAlert className="h-4 w-4 text-danger" /> Rogue endpoints
          {data.length > 0 && <Badge variant="danger">{data.length}</Badge>}
        </h3>
        {rogues.isLoading ? (
          <div className="space-y-2 p-4">{Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
        ) : data.length === 0 ? (
          <div className="flex flex-col items-center gap-2 py-8 text-center text-xs text-muted">
            <span>No rogue endpoints. Configure an allow list to start detecting.</span>
            <KbLink article="watch-lists" variant="inline" label="How rogue detection works" />
          </div>
        ) : (
          <Table>
            <THead className="bg-surface2/40">
              <Tr><Th>Endpoint</Th><Th>MAC</Th><Th>IP</Th><Th>Vendor</Th><Th>Location</Th><Th className="text-right">First seen</Th></Tr>
            </THead>
            <TBody>
              {data.map((e: Endpoint) => (
                <Tr key={e.id} className="cursor-pointer">
                  <Td><Link to={`/udt/endpoints/${e.id}`} className="font-medium text-primary hover:underline">{e.hostname || e.ip || 'unknown'}</Link></Td>
                  <Td>{macCol(e.mac)}</Td>
                  <Td className="font-mono text-xs">{e.ip || '—'}</Td>
                  <Td className="max-w-[160px] truncate text-xs text-muted">{e.vendor || '—'}</Td>
                  <Td className="text-xs">{(e as any).switch ? `${(e as any).switch} · ${e.if_name || `if ${e.if_index}`}` : '—'}</Td>
                  <Td className="text-right text-xs text-muted">{relTime(e.first_seen)}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </CardContent>
    </Card>
  )
}

export function WatchListsPage() {
  return (
    <div className="space-y-4">
      <RoguesCard />
      <RuleTable listType="allow" />
      <RuleTable listType="watch" />
      <RuleTable listType="ignore" />
    </div>
  )
}
