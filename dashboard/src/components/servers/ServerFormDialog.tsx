/** Create / edit a server record (manual registration for agentless or
 *  pre-staged hosts; agents normally create their server on enrollment). */

import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Server } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Input } from '@/components/ui/Input'
import { Label } from '@/components/ui/Label'
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/Select'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import type { CollectionMode, OsType, ServerItem } from '@/types/servers'

const OS_OPTIONS: { value: OsType; label: string }[] = [
  { value: 'windows', label: 'Windows' },
  { value: 'linux', label: 'Linux' },
  { value: 'macos', label: 'macOS' },
  { value: 'bsd', label: 'BSD' },
  { value: 'other', label: 'Other' },
  { value: 'unknown', label: 'Unknown' },
]

const MODE_OPTIONS: { value: CollectionMode; label: string }[] = [
  { value: 'agent', label: 'Agent' },
  { value: 'agentless_winrm', label: 'Agentless — WinRM' },
  { value: 'agentless_wmi', label: 'Agentless — WMI' },
  { value: 'ssh', label: 'Agentless — SSH' },
  { value: 'snmp', label: 'SNMP' },
  { value: 'none', label: 'Not monitored' },
]

export function ServerFormDialog({
  open,
  onOpenChange,
  server,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  /** When provided, edits in place; otherwise creates. */
  server?: ServerItem | null
}) {
  const qc = useQueryClient()
  const editing = Boolean(server)

  const [displayName, setDisplayName] = useState('')
  const [hostname, setHostname] = useState('')
  const [primaryIp, setPrimaryIp] = useState('')
  const [osType, setOsType] = useState<OsType>('windows')
  const [mode, setMode] = useState<CollectionMode>('agent')
  const [environment, setEnvironment] = useState('')
  const [owner, setOwner] = useState('')
  const [tags, setTags] = useState('')
  const [description, setDescription] = useState('')

  useEffect(() => {
    if (open) {
      setDisplayName(server?.display_name || '')
      setHostname(server?.hostname || '')
      setPrimaryIp(server?.primary_ip || '')
      setOsType(server?.os_type || 'windows')
      setMode(server?.collection_mode || 'agent')
      setEnvironment(server?.environment || '')
      setOwner(server?.owner || '')
      setTags((server?.tags || []).join(', '))
      setDescription(server?.description || '')
    }
  }, [open, server])

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        display_name: displayName.trim(),
        hostname: hostname.trim() || null,
        primary_ip: primaryIp.trim() || null,
        os_type: osType,
        collection_mode: mode,
        environment: environment.trim() || null,
        owner: owner.trim() || null,
        tags: tags.split(',').map((t) => t.trim()).filter(Boolean),
        description: description.trim() || null,
      }
      if (editing && server) {
        return (await api.patch(`/servers/${server.id}`, body)).data
      }
      return (await api.post('/servers', body)).data
    },
    onSuccess: () => {
      toast.success(editing ? 'Server updated' : 'Server registered')
      qc.invalidateQueries({ queryKey: ['servers'] })
      onOpenChange(false)
    },
    onError: (e) => toast.error('Save failed', apiErrorMessage(e)),
  })

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            <Server className="h-4 w-4 text-primary" />
            {editing ? 'Edit server' : 'Register server'}
          </DialogTitle>
          <DialogDescription>
            {editing
              ? 'Update identity, ownership, and grouping for this server.'
              : 'Manually register a server (for agentless monitoring or pre-staging). Agent installs register automatically.'}
          </DialogDescription>
        </DialogHeader>

        <div className="grid grid-cols-2 gap-4">
          <div className="col-span-2 space-y-1.5">
            <Label>Display name *</Label>
            <Input value={displayName} onChange={(e) => setDisplayName(e.target.value)} placeholder="db-prod-01" />
          </div>
          <div className="space-y-1.5">
            <Label>Hostname</Label>
            <Input value={hostname} onChange={(e) => setHostname(e.target.value)} placeholder="db-prod-01.corp.local" />
          </div>
          <div className="space-y-1.5">
            <Label>Primary IP</Label>
            <Input value={primaryIp} onChange={(e) => setPrimaryIp(e.target.value)} placeholder="10.0.0.21" />
          </div>
          <div className="space-y-1.5">
            <Label>Operating system</Label>
            <Select value={osType} onValueChange={(v) => setOsType(v as OsType)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {OS_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Collection mode</Label>
            <Select value={mode} onValueChange={(v) => setMode(v as CollectionMode)}>
              <SelectTrigger><SelectValue /></SelectTrigger>
              <SelectContent>
                {MODE_OPTIONS.map((o) => <SelectItem key={o.value} value={o.value}>{o.label}</SelectItem>)}
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Environment</Label>
            <Input value={environment} onChange={(e) => setEnvironment(e.target.value)} placeholder="production" />
          </div>
          <div className="space-y-1.5">
            <Label>Owner</Label>
            <Input value={owner} onChange={(e) => setOwner(e.target.value)} placeholder="platform-team" />
          </div>
          <div className="col-span-2 space-y-1.5">
            <Label>Tags (comma-separated)</Label>
            <Input value={tags} onChange={(e) => setTags(e.target.value)} placeholder="prod, web-tier, dc1" />
          </div>
          <div className="col-span-2 space-y-1.5">
            <Label>Description</Label>
            <Textarea rows={2} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => save.mutate()} disabled={!displayName.trim() || save.isPending}>
            {save.isPending ? 'Saving…' : editing ? 'Save changes' : 'Register server'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
