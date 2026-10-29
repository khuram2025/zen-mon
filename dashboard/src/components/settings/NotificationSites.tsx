import { useEffect, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { useCan } from '@/stores/auth'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { PasswordInput } from '@/components/ui/PasswordInput'
import { Textarea } from '@/components/ui/Textarea'
import { FormField } from '@/components/ui/FormField'
import { toast } from '@/components/ui/Toast'

type Peer = { id: string; name: string; site_id: string; direction: string; enabled: boolean; url?: string; channel_map: Record<string, string> }
type Config = { enabled: boolean; site_id: string; peers: Peer[]; policies: { channel_id: string; senders: (string | null)[]; mode: 'strict_selected' | 'local_safety' }[] }
type Channel = { id: string; name: string }
const base = '/settings/notification-sites'
const selectStyle = 'w-full rounded-md border border-border bg-surface px-3 py-2 text-sm'

export function NotificationSites() {
  const can = useCan()
  const editable = can('settings.manage')
  const qc = useQueryClient()
  const { data, error } = useQuery<Config>({ queryKey: ['notification-sites'], queryFn: async () => (await api.get(base)).data })
  const { data: channels = [] } = useQuery<Channel[]>({ queryKey: ['settings', 'channels'], queryFn: async () => {
    const value = (await api.get('/settings/channels')).data
    return Array.isArray(value) ? value : value.data || []
  } })
  const [channel, setChannel] = useState('')
  const [order, setOrder] = useState<string[]>(['local'])
  const [mode, setMode] = useState<'strict_selected' | 'local_safety'>('strict_selected')
  const [candidate, setCandidate] = useState('local')
  const [form, setForm] = useState<'none' | 'trust' | 'sender'>('none')
  const [name, setName] = useState('')
  const [siteId, setSiteId] = useState('')
  const [remoteChannel, setRemoteChannel] = useState('')
  const [localChannel, setLocalChannel] = useState('')
  const [url, setUrl] = useState('')
  const [token, setToken] = useState('')
  const [ca, setCa] = useState('')
  const [issued, setIssued] = useState('')
  const [busy, setBusy] = useState(false)
  useEffect(() => {
    const policy = data?.policies.find(p => p.channel_id === channel)
    setOrder(policy?.senders.map(s => s ?? 'local') ?? ['local'])
    setMode(policy?.mode ?? 'strict_selected')
  }, [channel, data])
  if (error) return <p className="text-sm text-danger">Notification site settings could not be loaded.</p>
  if (!data?.enabled) return null
  const senders = data.peers.filter(p => p.direction === 'outbound' && p.enabled)
  const label = (id: string) => id === 'local' ? 'This site' : data.peers.find(p => p.id === id)?.name ?? 'Unavailable sender'
  async function work(action: () => Promise<void>) {
    setBusy(true)
    try { await action(); await qc.invalidateQueries({ queryKey: ['notification-sites'] }) }
    catch (e) { toast.error('Notification site update failed', apiErrorMessage(e)) }
    finally { setBusy(false) }
  }
  function move(index: number, change: number) {
    const values = [...order]; [values[index], values[index + change]] = [values[index + change], values[index]]; setOrder(values)
  }
  return <Card>
    <CardHeader><CardTitle>Notification sites</CardTitle></CardHeader>
    <CardContent className="space-y-5">
      <div className="space-y-2 text-sm">
        <p>Choose which site sends each channel’s incident notifications, in priority order. A confirmed delivery failure or an unreachable sender moves to the next site.</p>
        <p className="text-muted">Choose whether an unconfirmed remote delivery is held for review or followed by a labelled local safety copy. Each new notification starts with your preferred sender.</p>
        <p>This site ID: <span className="select-all font-mono text-xs">{data.site_id}</span></p>
      </div>
      <div className="space-y-3">
        <FormField label="Notification channel"><select className={selectStyle} value={channel} onChange={e => setChannel(e.target.value)}>
          <option value="">Select a channel</option>{channels.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}
        </select></FormField>
        {channel && <>
          <FormField label="Unconfirmed remote delivery"><select className={selectStyle} disabled={!editable || busy} value={mode} onChange={e => setMode(e.target.value as typeof mode)}>
            <option value="strict_selected">Hold for review</option><option value="local_safety">Send a local safety copy after the handoff wait</option>
          </select></FormField>
          {mode === 'local_safety' && <p className="text-sm text-warning">This site must be last in the order. If the remote result stays unknown after its grant and safety margin, this site sends a labelled copy. The recipient may receive both copies.</p>}
          <p className="text-xs text-muted">Channel ID for pairing: <span className="select-all font-mono">{channel}</span></p>
          <ol className="space-y-2">{order.map((id, index) => <li key={id} className="flex flex-wrap items-center gap-2 text-sm">
            <span className="min-w-40 flex-1">{index + 1}. {label(id)}</span>
            <Button size="sm" variant="outline" disabled={!editable || busy || index === 0} onClick={() => move(index, -1)}>Up</Button>
            <Button size="sm" variant="outline" disabled={!editable || busy || index === order.length - 1} onClick={() => move(index, 1)}>Down</Button>
            <Button size="sm" variant="outline" disabled={!editable || busy} onClick={() => setOrder(order.filter(v => v !== id))}>Remove</Button>
          </li>)}</ol>
          <div className="flex flex-wrap gap-2">
            <select aria-label="Add a notification sender" className={selectStyle + ' max-w-sm'} value={candidate} onChange={e => setCandidate(e.target.value)}>
              <option value="local">This site</option>{senders.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
            </select>
            <Button variant="outline" disabled={!editable || busy || order.includes(candidate) || order.length >= 8} onClick={() => setOrder([...order, candidate])}>Add sender</Button>
            <Button disabled={!editable || busy || !order.length || (mode === 'local_safety' && order.at(-1) !== 'local')} onClick={() => work(async () => {
              await api.put(`${base}/policies/${channel}`, { senders: order.map(v => v === 'local' ? null : v), mode }); toast.success('Sender order saved')
            })}>Save order</Button>
          </div>
        </>}
      </div>
      <div className="space-y-2">
        <h3 className="text-sm font-semibold">Paired sites</h3>
        {data.peers.length === 0 && <p className="text-sm text-muted">No sites paired yet.</p>}
        {data.peers.map(p => <div key={p.id} className="flex flex-wrap items-center justify-between gap-2 text-sm">
          <span>{p.name} · {p.direction === 'inbound' ? 'Allowed origin' : 'Sender'} · {p.enabled ? 'Enabled' : 'Disabled'}</span>
          <Button variant="outline" size="sm" disabled={!editable || busy} onClick={() => work(async () => {
            await api.post(`${base}/peers/${p.id}/${p.enabled ? 'disable' : 'enable'}`)
          })}>{p.enabled ? 'Disable' : 'Enable'}</Button>
        </div>)}
        <div className="flex flex-wrap gap-2">
          <Button variant="outline" disabled={!editable || busy} onClick={() => { setForm('trust'); setIssued(''); setToken('') }}>Allow another origin</Button>
          <Button variant="outline" disabled={!editable || busy} onClick={() => { setForm('sender'); setIssued(''); setToken('') }}>Add a sender site</Button>
        </div>
      </div>
      {form !== 'none' && <div className="space-y-3">
        <p className="text-sm">{form === 'trust' ? 'Allow a remote site to use a local channel. Its notifications remain owned by that origin. Reuse an origin site ID to add another channel mapping.' : 'First authorize this site on the sender. Enter the sender’s site ID, HTTPS address, and the credential it issued.'}</p>
        <FormField label="Site name"><Input value={name} onChange={e => setName(e.target.value)} maxLength={100} /></FormField>
        <FormField label={form === 'trust' ? 'Origin site ID' : 'Sender site ID'}><Input value={siteId} onChange={e => setSiteId(e.target.value)} /></FormField>
        {form === 'trust' ? <>
          <FormField label="Origin channel ID"><Input value={remoteChannel} onChange={e => setRemoteChannel(e.target.value)} /></FormField>
          <FormField label="Local delivery channel"><select className={selectStyle} value={localChannel} onChange={e => setLocalChannel(e.target.value)}><option value="">Select a channel</option>{channels.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select></FormField>
        </> : <>
          <FormField label="Sender HTTPS address"><Input value={url} placeholder="https://monitoring.example.com" onChange={e => setUrl(e.target.value)} /></FormField>
          <FormField label="Pairing credential"><PasswordInput value={token} onChange={e => setToken(e.target.value)} autoComplete="off" /></FormField>
          <FormField label="Private certificate authority in PEM format" hint="Leave blank when the sender uses a publicly trusted certificate."><Textarea value={ca} onChange={e => setCa(e.target.value)} rows={4} /></FormField>
        </>}
        <div className="flex gap-2">
          <Button disabled={busy || !name || !siteId || (form === 'trust' ? !remoteChannel || !localChannel : !url || !token)} onClick={() => work(async () => {
            if (form === 'trust') {
              const existing = data.peers.find(p => p.direction === 'inbound' && p.site_id === siteId)
              if (existing) { await api.put(`${base}/trust/${existing.id}/channels`, { channel_map: { ...existing.channel_map, [remoteChannel]: localChannel } }); setIssued(''); toast.success('Channel permission saved') }
              else { const result = await api.post(`${base}/trust`, { name, site_id: siteId, channel_map: { [remoteChannel]: localChannel } }); setIssued(result.data.token) }
            } else { await api.post(`${base}/peers`, { name, site_id: siteId, url, token, ca_pem: ca }); setToken(''); setForm('none'); toast.success('Sender identity verified and paired') }
          })}>{form === 'trust' ? 'Save origin permission' : 'Verify and pair'}</Button>
          <Button variant="outline" onClick={() => { setForm('none'); setIssued(''); setToken('') }}>Close</Button>
        </div>
        {issued && <FormField label="Pairing credential shown once" hint="Copy this into the origin site’s Add a sender site form. Closing clears this display."><PasswordInput value={issued} readOnly autoComplete="off" /></FormField>}
      </div>}
    </CardContent>
  </Card>
}
