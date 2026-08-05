import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Card, CardContent } from '@/components/ui/Card'
import { Skeleton } from '@/components/ui/Skeleton'
import { udtApi } from './api'
import type { UdtEvent } from './types'
import { EventBadge, macCol, relTime } from './helpers'

const FILTERS = [
  { key: '', label: 'All' },
  { key: 'new_endpoint', label: 'New' },
  { key: 'rogue_detected', label: 'Rogue' },
  { key: 'endpoint_moved', label: 'Moves' },
  { key: 'watch_seen', label: 'Watch' },
  { key: 'user_login', label: 'Logins' },
  { key: 'port_admin', label: 'Port actions' },
]

function eventText(ev: UdtEvent): string {
  const d = ev.details || {}
  switch (ev.event_type) {
    case 'endpoint_moved': return `moved from if ${d.from_if_index} to if ${d.to_if_index}`
    case 'user_login': return `${d.user || 'user'} signed in`
    case 'port_admin': return `${d.action} by ${d.by || 'operator'}`
    case 'rogue_detected': return 'appeared outside the allow list'
    case 'watch_seen': return 'watched endpoint seen on the network'
    case 'new_endpoint': return d.vlan ? `first seen on VLAN ${d.vlan}` : 'first seen on the network'
    default: return ''
  }
}

export function ActivityPage() {
  const [filter, setFilter] = useState('')
  const [hours, setHours] = useState(24)

  const events = useQuery({
    queryKey: ['udt', 'events', filter, hours],
    queryFn: () => udtApi.events({ hours, limit: 200, ...(filter ? { event_type: filter } : {}) }),
    refetchInterval: 15_000,
  })

  const rows = events.data?.data || []

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-wrap items-center gap-1">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => setFilter(f.key)}
              className={`rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors ${
                filter === f.key ? 'bg-primary/12 text-primary' : 'text-muted hover:text-text'
              }`}>{f.label}</button>
          ))}
        </div>
        <div className="flex items-center gap-1 text-xs">
          {[24, 168, 720].map((h) => (
            <button key={h} onClick={() => setHours(h)}
              className={`rounded-md px-2 py-1 ${hours === h ? 'bg-surface2 text-text' : 'text-muted hover:text-text'}`}>
              {h === 24 ? '24h' : h === 168 ? '7d' : '30d'}
            </button>
          ))}
        </div>
      </div>

      <Card>
        <CardContent className="p-0">
          {events.isLoading ? (
            <div className="space-y-2 p-4">{Array.from({ length: 10 }).map((_, i) => <Skeleton key={i} className="h-10 w-full" />)}</div>
          ) : rows.length === 0 ? (
            <div className="py-16 text-center text-sm text-muted">No activity in this window.</div>
          ) : (
            <ul className="divide-y divide-border">
              {rows.map((ev: UdtEvent) => (
                <li key={ev.id} className="flex items-center gap-3 px-4 py-3">
                  <EventBadge type={ev.event_type} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2 text-sm">
                      {ev.endpoint_id ? (
                        <Link to={`/udt/endpoints/${ev.endpoint_id}`} className="font-medium hover:text-primary">
                          {ev.hostname || (ev.mac ? ev.mac : 'endpoint')}
                        </Link>
                      ) : <span className="font-medium">{ev.hostname || 'endpoint'}</span>}
                      {ev.mac && !ev.hostname && macCol(ev.mac)}
                      <span className="text-muted">{eventText(ev)}</span>
                    </div>
                    {ev.switch && (
                      <div className="text-xs text-muted">
                        {ev.switch}{ev.if_index ? ` · if ${ev.if_index}` : ''}
                      </div>
                    )}
                  </div>
                  <span className="whitespace-nowrap text-xs text-muted">{relTime(ev.created_at)}</span>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
