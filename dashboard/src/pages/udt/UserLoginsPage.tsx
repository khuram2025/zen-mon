import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Search, UserRound } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { Skeleton } from '@/components/ui/Skeleton'
import { udtApi } from './api'
import { macCol, relTime } from './helpers'

export function UserLoginsPage() {
  const [q, setQ] = useState('')
  const [selected, setSelected] = useState<string | null>(null)

  const users = useQuery({
    queryKey: ['udt', 'users', q],
    queryFn: () => udtApi.users(q ? { q } : {}),
    refetchInterval: 30_000,
  })

  const detail = useQuery({
    queryKey: ['udt', 'user', selected],
    queryFn: () => udtApi.user(selected!),
    enabled: !!selected,
  })

  return (
    <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_380px]">
      <Card>
        <CardContent className="p-0">
          <div className="border-b border-border p-3">
            <div className="relative">
              <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-muted" />
              <Input className="pl-9" placeholder="Search users…" value={q} onChange={(e) => setQ(e.target.value)} />
            </div>
          </div>
          {users.isLoading ? (
            <div className="space-y-2 p-4">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-9 w-full" />)}</div>
          ) : (users.data?.data.length || 0) === 0 ? (
            <div className="py-16 text-center text-sm text-muted">
              No user logins recorded. Add a domain controller under <Link to="/udt/settings" className="text-primary hover:underline">Settings</Link> to correlate AD logons to endpoints.
            </div>
          ) : (
            <Table>
              <THead className="bg-surface2/40">
                <Tr><Th>User</Th><Th>Domain</Th><Th className="text-right">Endpoints</Th><Th className="text-right">Logins</Th><Th className="text-right">Last login</Th></Tr>
              </THead>
              <TBody>
                {users.data!.data.map((u: any) => (
                  <Tr key={u.user_name} className="cursor-pointer" onClick={() => setSelected(u.user_name)}>
                    <Td className="font-medium">
                      <span className="inline-flex items-center gap-2"><UserRound className="h-4 w-4 text-muted" />{u.user_name}</span>
                    </Td>
                    <Td className="text-xs text-muted">{u.domain || '—'}</Td>
                    <Td className="text-right tabular-nums">{u.endpoints}</Td>
                    <Td className="text-right tabular-nums">{u.logins}</Td>
                    <Td className="text-right text-xs text-muted">{relTime(u.last_login)}</Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          )}
        </CardContent>
      </Card>

      {/* Detail rail */}
      <Card>
        <CardContent className="p-0">
          <h3 className="border-b border-border px-4 py-3 text-sm font-semibold">
            {selected ? selected : 'Select a user'}
          </h3>
          {!selected ? (
            <div className="p-6 text-center text-xs text-muted">Pick a user to see their endpoints and login history.</div>
          ) : detail.isLoading ? (
            <div className="space-y-2 p-4">{Array.from({ length: 5 }).map((_, i) => <Skeleton key={i} className="h-8 w-full" />)}</div>
          ) : (
            <div className="divide-y divide-border">
              <div className="p-4">
                <div className="mb-2 text-[11px] uppercase tracking-wide text-muted">Endpoints used</div>
                {detail.data!.endpoints.length === 0 ? <div className="text-xs text-muted">None correlated.</div> : (
                  <ul className="space-y-1.5">
                    {detail.data!.endpoints.map((e: any) => (
                      <li key={e.id}>
                        <Link to={`/udt/endpoints/${e.id}`} className="flex items-center justify-between text-xs hover:text-primary">
                          <span>{e.hostname || e.ip || 'endpoint'} {macCol(e.mac)}</span>
                          <span className="text-muted">{relTime(e.last_login)}</span>
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
              <div className="p-4">
                <div className="mb-2 text-[11px] uppercase tracking-wide text-muted">Recent logins</div>
                <ul className="space-y-1.5">
                  {detail.data!.logins.slice(0, 25).map((l: any, i: number) => (
                    <li key={i} className="flex items-center justify-between text-xs">
                      <span className="text-muted">{l.hostname || l.ip || '—'} · evt {l.event_id ?? '?'}</span>
                      <span className="text-muted">{relTime(l.event_time)}</span>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
