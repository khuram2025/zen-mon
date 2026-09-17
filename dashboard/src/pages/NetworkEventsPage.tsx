import { useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'

const severityNames = ['Emergency', 'Alert', 'Critical', 'Error', 'Warning', 'Notice', 'Info', 'Debug']

export function NetworkEventsPage() {
  const [search, setSearch] = useState('')
  const [severity, setSeverity] = useState(7)
  const [name, setName] = useState('Network syslog event')
  const [channel, setChannel] = useState('')
  const events = useQuery<any>({ queryKey: ['network-events', search, severity],
    queryFn: async () => (await api.get('/network-events', { params: { search, severity } })).data,
    refetchInterval: 15000 })
  const channels = useQuery<any[]>({ queryKey: ['network-event-channels'],
    queryFn: async () => { const data = (await api.get('/settings/channels')).data; return data.data || data } })
  const rule = useMutation({ mutationFn: async () => api.post('/alert-rules', {
    name, metric: 'syslog', operator: '<=', threshold: severity, target: search || null,
    notify_channels: channel ? [channel] : [], severity: 'warning', cooldown: 300, recovery_alert: false,
  }) })
  return <div className="space-y-5">
    <div><h1 className="text-2xl font-semibold">Network syslog</h1><p className="text-sm text-muted">Received device events from the last 24 hours. Lower severity numbers are more urgent.</p></div>
    <Card><CardContent className="space-y-3 pt-5">
      <div className="flex flex-wrap gap-3">
        <input aria-label="Search syslog messages" placeholder="Message contains…" value={search} onChange={e => setSearch(e.target.value)} className="rounded border border-border bg-background p-2 text-sm" />
        <select aria-label="Maximum syslog severity" value={severity} onChange={e => setSeverity(Number(e.target.value))} className="rounded border border-border bg-background p-2 text-sm">
          {severityNames.map((n, i) => <option value={i} key={i}>{i}: {n} and more urgent</option>)}
        </select>
      </div>
      {events.isError && <p className="text-danger text-sm">Unable to load network events. Try again or contact your administrator.</p>}
      <div className="overflow-auto"><Table><THead><Tr><Th>Received</Th><Th>Source</Th><Th>Severity</Th><Th>Message</Th></Tr></THead><TBody>
        {(events.data?.data || []).map((e: any) => <Tr key={e.id}><Td className="text-xs whitespace-nowrap">{new Date(e.received_at).toLocaleString()}</Td><Td className="text-xs">{e.device_hostname || e.source_ip}</Td><Td>{severityNames[e.severity]}</Td><Td className="text-xs break-all">{e.message}{e.metadata?.suppressed && <span className="ml-2 text-muted">Alert suppressed</span>}</Td></Tr>)}
        {!events.isLoading && !events.data?.data?.length && <Tr><Td colSpan={4} className="text-center text-muted py-10">No matching syslog events received.</Td></Tr>}
      </TBody></Table></div>
    </CardContent></Card>
    <Card><CardHeader><CardTitle>Create an event alert</CardTitle><p className="text-sm text-muted">Uses the message and severity filters above. Repeated events are grouped for five minutes. Event alerts are acknowledged or resolved by an operator.</p></CardHeader><CardContent className="space-y-3">
      <input aria-label="Syslog rule name" value={name} onChange={e => setName(e.target.value)} className="rounded border border-border bg-background p-2 text-sm" />
      <select aria-label="Notification destination" value={channel} onChange={e => setChannel(e.target.value)} className="ml-3 rounded border border-border bg-background p-2 text-sm">
        <option value="">Record alert only</option>{(channels.data || []).map((c: any) => <option key={c.id} value={c.id}>{c.name}</option>)}
      </select>
      <div><Button disabled={rule.isPending || !name.trim()} onClick={() => rule.mutate()}>Create rule</Button></div>
      {rule.isSuccess && <p className="text-sm">Rule created. Manage its scope and destinations in Alert Rules.</p>}
      {rule.isError && <p className="text-sm text-danger">{(rule.error as any)?.response?.data?.detail || 'Could not create rule'}</p>}
    </CardContent></Card>
  </div>
}
