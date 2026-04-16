import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellRing, Loader2, Pencil, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { Switch } from '@/components/ui/Switch'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'

export function NotificationsPage() {
  const qc = useQueryClient()
  const [formOpen, setFormOpen] = useState(false)
  const [editing, setEditing] = useState<any>(null)
  const [deleting, setDeleting] = useState<any>(null)

  const { data: channels } = useQuery<any[]>({
    queryKey: ['settings', 'channels'],
    queryFn: async () => { const r = (await api.get('/settings/channels')).data; return Array.isArray(r) ? r : r?.data || [] },
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/settings/channels/${id}`),
    onSuccess: () => { toast.success('Channel deleted'); qc.invalidateQueries({ queryKey: ['settings', 'channels'] }); setDeleting(null) },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const test = useMutation({
    mutationFn: async (id: string) => api.post(`/settings/channels/${id}/test`),
    onSuccess: () => toast.success('Test sent'),
    onError: (e: any) => toast.error('Test failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <BellRing className="h-5 w-5 text-primary" /> Notification Channels
          </h1>
          <p className="text-xs text-muted">Configure how alerts are delivered</p>
        </div>
        <Button onClick={() => { setEditing(null); setFormOpen(true) }}><Plus className="h-4 w-4" /> New channel</Button>
      </div>

      <Card>
        <CardContent className="p-0">
          <Table>
            <THead className="bg-surface2/50">
              <Tr><Th>Name</Th><Th>Type</Th><Th>Enabled</Th><Th className="w-40 text-right">Actions</Th></Tr>
            </THead>
            <TBody>
              {(channels || []).map((c) => (
                <Tr key={c.id}>
                  <Td className="font-medium">{c.name}</Td>
                  <Td><Badge variant="outline">{c.type}</Badge></Td>
                  <Td><Badge variant={c.enabled ? 'success' : 'outline'}>{c.enabled ? 'yes' : 'no'}</Badge></Td>
                  <Td>
                    <div className="flex justify-end gap-1">
                      <Button size="sm" variant="outline" onClick={() => test.mutate(c.id)}>Test</Button>
                      <Button size="sm" variant="ghost" onClick={() => { setEditing(c); setFormOpen(true) }}><Pencil className="h-3.5 w-3.5" /></Button>
                      <Button size="sm" variant="ghost" className="text-muted hover:text-danger" onClick={() => setDeleting(c)}><Trash2 className="h-3.5 w-3.5" /></Button>
                    </div>
                  </Td>
                </Tr>
              ))}
              {(!channels || channels.length === 0) && <Tr><Td colSpan={4} className="py-8 text-center text-muted">No channels yet — add one to receive notifications</Td></Tr>}
            </TBody>
          </Table>
        </CardContent>
      </Card>

      <ChannelFormDialog open={formOpen} onOpenChange={setFormOpen} channel={editing} />
      <ConfirmDialog open={!!deleting} onOpenChange={(o) => !o && setDeleting(null)} title="Delete channel" description={<>Delete <span className="font-semibold text-text">{deleting?.name}</span>?</>} confirmText="Delete" destructive loading={del.isPending} onConfirm={() => { if (deleting) del.mutate(deleting.id) }} />
    </div>
  )
}

function ChannelFormDialog({ open, onOpenChange, channel }: { open: boolean; onOpenChange: (o: boolean) => void; channel?: any }) {
  const isEdit = !!channel?.id
  const qc = useQueryClient()
  const [name, setName] = useState('')
  const [type, setType] = useState('email')
  const [enabled, setEnabled] = useState(true)
  const [config, setConfig] = useState<Record<string, string>>({})

  useEffect(() => {
    if (!open) return
    if (channel) { setName(channel.name || ''); setType(channel.type || 'email'); setEnabled(channel.enabled ?? true); setConfig(channel.config || {}) }
    else { setName(''); setType('email'); setEnabled(true); setConfig({}) }
  }, [open, channel])

  const save = useMutation({
    mutationFn: async () => {
      const payload = { name, type, enabled, config }
      if (isEdit) return (await api.put(`/settings/channels/${channel.id}`, payload)).data
      return (await api.post('/settings/channels', payload)).data
    },
    onSuccess: () => { toast.success(isEdit ? 'Channel updated' : 'Channel created'); qc.invalidateQueries({ queryKey: ['settings', 'channels'] }); onOpenChange(false) },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <DialogHeader><DialogTitle>{isEdit ? 'Edit channel' : 'New notification channel'}</DialogTitle></DialogHeader>
        <form onSubmit={(e) => { e.preventDefault(); save.mutate() }} className="space-y-3">
          <FormField label="Name" required><Input required value={name} onChange={(e) => setName(e.target.value)} /></FormField>
          <FormField label="Type">
            <Select value={type} onValueChange={setType}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                <SelectItem value="email">Email</SelectItem>
                <SelectItem value="sms">SMS</SelectItem>
                <SelectItem value="webhook">Webhook</SelectItem>
                <SelectItem value="slack">Slack</SelectItem>
                <SelectItem value="telegram">Telegram</SelectItem>
              </SelectContent>
            </Select>
          </FormField>
          {type === 'email' && <FormField label="Recipients" hint="Comma-separated"><Input value={config.recipients || ''} onChange={(e) => setConfig({ ...config, recipients: e.target.value })} placeholder="ops@example.com" /></FormField>}
          {type === 'sms' && <FormField label="Phone numbers" hint="E.164 format"><Input value={config.numbers || ''} onChange={(e) => setConfig({ ...config, numbers: e.target.value })} placeholder="+966501234567" /></FormField>}
          {type === 'webhook' && <FormField label="URL" required><Input required value={config.url || ''} onChange={(e) => setConfig({ ...config, url: e.target.value })} placeholder="https://hooks.example.com/endpoint" /></FormField>}
          {type === 'slack' && <FormField label="Slack webhook URL" required><Input required value={config.webhook_url || ''} onChange={(e) => setConfig({ ...config, webhook_url: e.target.value })} placeholder="https://hooks.slack.com/services/..." /></FormField>}
          {type === 'telegram' && (<><FormField label="Bot token" required><Input required type="password" value={config.bot_token || ''} onChange={(e) => setConfig({ ...config, bot_token: e.target.value })} /></FormField><FormField label="Chat ID" required><Input required value={config.chat_id || ''} onChange={(e) => setConfig({ ...config, chat_id: e.target.value })} /></FormField></>)}
          <div className="flex items-center justify-between rounded-md border border-border px-3 py-2">
            <span className="text-xs font-medium uppercase tracking-wider text-muted">Enabled</span>
            <Switch checked={enabled} onCheckedChange={setEnabled} />
          </div>
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={save.isPending}>{save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}{isEdit ? 'Save' : 'Create'}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
