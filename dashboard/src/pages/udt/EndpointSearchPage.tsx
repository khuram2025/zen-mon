import { useState } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { AlertTriangle, Eye, RadioTower, ScanSearch, Search, ShieldAlert, Sparkles, Users } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Badge } from '@/components/ui/Badge'
import { Skeleton } from '@/components/ui/Skeleton'
import { udtApi } from './api'
import type { Endpoint } from './types'
import {
  AuthBadge, ENDPOINT_TYPE_META, EndpointTypeIcon, OnlineDot, macCol, portLabel, relTime,
} from './helpers'

const FLAG_TABS = [
  { key: '', label: 'All' },
  { key: 'active', label: 'Active' },
  { key: 'rogue', label: 'Rogue' },
  { key: 'watched', label: 'Watched' },
  { key: 'new', label: 'New (24h)' },
  { key: 'randomized', label: 'Random MAC' },
]

function KpiTile({ icon: Icon, label, value, tone, onClick, active }: {
  icon: React.ComponentType<{ className?: string }>; label: string; value: React.ReactNode
  tone?: string; onClick?: () => void; active?: boolean
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-3 rounded-lg border bg-surface px-4 py-3 text-left transition-colors ${
        active ? 'border-primary ring-1 ring-primary/40' : 'border-border hover:border-border-strong'
      }`}
    >
      <div className={`flex h-9 w-9 items-center justify-center rounded-md ${tone || 'bg-primary/10 text-primary'}`}>
        <Icon className="h-[18px] w-[18px]" />
      </div>
      <div>
        <div className="text-lg font-semibold leading-tight tabular-nums">{value}</div>
        <div className="text-[11px] text-muted">{label}</div>
      </div>
    </button>
  )
}

export function EndpointSearchPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const q = params.get('q') || ''
  const flag = params.get('flag') || ''
  const status = params.get('status') || ''
  const type = params.get('type') || ''
  const [qInput, setQInput] = useState(q)
  const [page, setPage] = useState(0)
  const limit = 50

  function patch(next: Record<string, string | null>) {
    const p = new URLSearchParams(params)
    for (const [k, v] of Object.entries(next)) {
      if (!v) p.delete(k)
      else p.set(k, v)
    }
    setParams(p, { replace: true })
    setPage(0)
  }

  const summary = useQuery({
    queryKey: ['udt', 'summary'],
    queryFn: () => udtApi.summary(),
    refetchInterval: 15_000,
  })

  const queryParams: Record<string, any> = { skip: page * limit, limit }
  if (q) queryParams.q = q
  if (type) queryParams.endpoint_type = type
  if (flag === 'active') queryParams.status = 'active'
  else if (flag) queryParams.flag = flag
  if (status) queryParams.status = status

  const list = useQuery({
    queryKey: ['udt', 'endpoints', queryParams],
    queryFn: () => udtApi.endpoints(queryParams),
    refetchInterval: 20_000,
    placeholderData: keepPreviousData,
  })

  const s = summary.data
  const rows = list.data?.data || []
  const total = list.data?.meta.total || 0

  return (
    <div className="space-y-4">
      {/* KPI row */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 xl:grid-cols-6">
        <KpiTile icon={ScanSearch} label="Endpoints" value={s?.total_endpoints ?? '—'}
          active={!flag} onClick={() => patch({ flag: null, status: null })} />
        <KpiTile icon={RadioTower} label="Active now" value={s?.active_endpoints ?? '—'} tone="bg-success/15 text-success"
          active={flag === 'active'} onClick={() => patch({ flag: 'active' })} />
        <KpiTile icon={ShieldAlert} label="Rogue" value={s?.rogue ?? '—'} tone="bg-danger/10 text-danger"
          active={flag === 'rogue'} onClick={() => patch({ flag: 'rogue' })} />
        <KpiTile icon={Eye} label="Watched" value={s?.watched ?? '—'} tone="bg-warning/15 text-warning"
          active={flag === 'watched'} onClick={() => patch({ flag: 'watched' })} />
        <KpiTile icon={Sparkles} label="New (24h)" value={s?.new_24h ?? '—'} tone="bg-info/10 text-info"
          active={flag === 'new'} onClick={() => patch({ flag: 'new' })} />
        <KpiTile icon={Users} label="Logins (24h)" value={s?.logins_24h ?? '—'} tone="bg-accent/10 text-accent"
          onClick={() => navigate('/udt/users')} />
      </div>

      {/* Search + filter row */}
      <Card>
        <CardContent className="flex flex-col gap-3 p-3 sm:flex-row sm:items-center">
          <form
            className="relative flex-1"
            onSubmit={(e) => { e.preventDefault(); patch({ q: qInput.trim() || null }) }}
          >
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
            <Input
              className="pl-9"
              placeholder="Search MAC, IP, hostname, user or vendor…"
              value={qInput}
              onChange={(e) => setQInput(e.target.value)}
            />
          </form>
          <div className="flex flex-wrap items-center gap-1">
            {FLAG_TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => patch({ flag: t.key || null, status: null })}
                className={`rounded-md px-2.5 py-1.5 text-xs font-medium transition-colors ${
                  flag === t.key ? 'bg-primary/12 text-primary' : 'text-muted hover:text-text'
                }`}
              >
                {t.label}
              </button>
            ))}
          </div>
        </CardContent>
      </Card>

      {/* Type filter chips */}
      <div className="flex flex-wrap items-center gap-1.5">
        <span className="text-xs text-muted">Type:</span>
        <button
          onClick={() => patch({ type: null })}
          className={`rounded-full border px-2.5 py-0.5 text-xs ${!type ? 'border-primary text-primary' : 'border-border text-muted hover:text-text'}`}
        >All</button>
        {Object.entries(ENDPOINT_TYPE_META).map(([k, m]) => (
          <button
            key={k}
            onClick={() => patch({ type: type === k ? null : k })}
            className={`inline-flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-xs ${
              type === k ? 'border-primary text-primary' : 'border-border text-muted hover:text-text'
            }`}
          >
            <m.icon className="h-3 w-3" /> {m.label}
          </button>
        ))}
      </div>

      {/* Results table */}
      <Card>
        <CardContent className="p-0">
          {list.isLoading ? (
            <div className="space-y-2 p-4">
              {Array.from({ length: 8 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}
            </div>
          ) : rows.length === 0 ? (
            <div className="py-16 text-center text-sm text-muted">
              No endpoints match. As switches are polled, discovered devices appear here.
            </div>
          ) : (
            <Table>
              <THead className="bg-surface2/40">
                <Tr>
                  <Th className="w-8"></Th>
                  <Th>Endpoint</Th>
                  <Th>MAC</Th>
                  <Th>IP address</Th>
                  <Th>Vendor</Th>
                  <Th>Connected to</Th>
                  <Th>VLAN</Th>
                  <Th>User</Th>
                  <Th className="text-right">Last seen</Th>
                  <Th></Th>
                </Tr>
              </THead>
              <TBody>
                {rows.map((e: Endpoint) => (
                  <Tr key={e.id} className="cursor-pointer" onClick={() => navigate(`/udt/endpoints/${e.id}`)}>
                    <Td><OnlineDot online={e.online} /></Td>
                    <Td>
                      <div className="flex items-center gap-2">
                        <EndpointTypeIcon type={e.endpoint_type} className="h-4 w-4 text-muted" />
                        <span className="font-medium">{e.hostname || ENDPOINT_TYPE_META[e.endpoint_type].label}</span>
                      </div>
                    </Td>
                    <Td>{macCol(e.mac)}</Td>
                    <Td className="font-mono text-xs tabular-nums">{e.ip || '—'}</Td>
                    <Td className="max-w-[180px] truncate text-xs text-muted" title={e.vendor || ''}>{e.vendor || '—'}</Td>
                    <Td className="text-xs">
                      {e.switch_hostname ? (
                        <span>
                          {e.switch_hostname}
                          <span className="text-muted"> · {portLabel(e.if_name, e.if_index)}</span>
                          {e.is_direct === false && <Badge variant="outline" className="ml-1">indirect</Badge>}
                        </span>
                      ) : '—'}
                    </Td>
                    <Td className="text-xs tabular-nums">{e.vlan_id ?? '—'}</Td>
                    <Td className="text-xs">{e.user_name || '—'}</Td>
                    <Td className="text-right text-xs text-muted">{relTime(e.last_seen)}</Td>
                    <Td><AuthBadge authorized={e.authorized} watched={e.is_watched} randomized={e.is_randomized} /></Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Pager */}
      {total > limit && (
        <div className="flex items-center justify-between text-xs text-muted">
          <span>
            Showing {page * limit + 1}–{Math.min((page + 1) * limit, total)} of {total.toLocaleString()}
          </span>
          <div className="flex gap-2">
            <button disabled={page === 0} onClick={() => setPage((p) => Math.max(0, p - 1))}
              className="rounded-md border border-border px-3 py-1 disabled:opacity-40">Prev</button>
            <button disabled={(page + 1) * limit >= total} onClick={() => setPage((p) => p + 1)}
              className="rounded-md border border-border px-3 py-1 disabled:opacity-40">Next</button>
          </div>
        </div>
      )}
    </div>
  )
}
