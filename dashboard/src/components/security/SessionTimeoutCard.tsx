import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Button } from '@/components/ui/Button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { QueryFailure } from '@/components/ui/QueryState'
import { toast } from '@/components/ui/Toast'

type Account = { id: string; username: string; minutes: number | null }
type Policy = { minutes: number; fixed_minutes: number; users: Account[] }
const path = '/system/security/session-timeouts'
const queryKey = ['security', 'session-timeouts']
const selectClass = 'rounded-md border border-border bg-surface px-3 py-2 text-sm'

function TimeoutEditor({ saved, user, fixed }: { saved: number | null; user?: string; fixed: number }) {
  const client = useQueryClient()
  const [mode, setMode] = useState(saved === null ? 'inherit' : saved === -1 ? 'fixed' : saved === 0 ? 'never' : 'idle')
  const [duration, setDuration] = useState(String(saved && saved > 0 ? saved : 30))
  const minutes = mode === 'inherit' ? null : mode === 'fixed' ? -1 : mode === 'never' ? 0 : Number(duration)
  const save = useMutation({
    mutationFn: () => api.put(user ? `${path}/users/${user}` : path, { minutes }),
    onSuccess: () => { void client.invalidateQueries({ queryKey }); toast.success('Session timeout saved') },
    onError: e => toast.error('Could not save session timeout', apiErrorMessage(e)),
  })
  const invalid = mode === 'idle' && (!Number.isInteger(minutes) || Number(minutes) < 1 || Number(minutes) > 1440)
  return <form className="flex flex-wrap items-end gap-3" onSubmit={event => {
    event.preventDefault()
    if (!invalid && minutes !== saved && !save.isPending) save.mutate()
  }}>
    <label className="grid gap-1 text-sm">{user ? 'Account timeout' : 'Appliance default'}
      <select className={selectClass} value={mode} onChange={e => setMode(e.target.value)}>
        {user && <option value="inherit">Use appliance default</option>}
        <option value="fixed">Fixed duration ({fixed} min from sign-in)</option>
        <option value="idle">After inactivity</option>
        <option value="never">No automatic timeout</option>
      </select>
    </label>
    {mode === 'idle' && <label className="grid gap-1 text-sm">Idle minutes
      <input className={selectClass + ' w-24'} type="number" required min={1} max={1440} step={1}
        value={duration} onChange={e => setDuration(e.target.value)} />
    </label>}
    <Button type="submit" disabled={save.isPending || invalid || minutes === saved}>{save.isPending ? 'Saving…' : 'Save timeout'}</Button>
  </form>
}

export function SessionTimeoutCard() {
  const policy = useQuery<Policy>({ queryKey, queryFn: async () => (await api.get(path)).data })
  const [id, setId] = useState('')
  const account = policy.data?.users.find(u => u.id === id)
  return <Card><CardHeader><CardTitle>Dashboard session timeout</CardTitle></CardHeader>
    <CardContent className="space-y-4">
      {policy.isError ? <QueryFailure compact what="session timeouts" error={policy.error} onRetry={() => void policy.refetch()} />
        : !policy.data ? <p>Loading session timeouts…</p> : <>
          <p className="text-sm text-muted">An idle timeout counts interaction with the dashboard. Automatic refreshes do not extend it. Changing an effective timeout signs the affected accounts out.</p>
          <TimeoutEditor key={`default-${policy.data.minutes}`} saved={policy.data.minutes} fixed={policy.data.fixed_minutes} />
          <label className="grid max-w-md gap-1 text-sm">Per-user override
            <select className={selectClass} value={id} onChange={e => setId(e.target.value)}>
              <option value="">Choose an account</option>
              {policy.data.users.map(u => <option key={u.id} value={u.id}>{u.username}{u.minutes !== null ? ' (override)' : ''}</option>)}
            </select>
          </label>
          {account && <TimeoutEditor key={`${account.id}-${account.minutes}`} user={account.id} saved={account.minutes} fixed={policy.data.fixed_minutes} />}
          <p className="text-sm text-muted">For an unattended NOC screen, select its account and choose No automatic timeout. Signing out, disabling the ZenPlus account or revoking its session ends access. Directory credentials are checked at sign-in.</p>
        </>}
    </CardContent></Card>
}
