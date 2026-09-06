import { useEffect, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'

type Policy = { web_restricted: boolean; ssh_restricted: boolean; allowed_cidrs: string[]; current_ip: string | null; helper_installed: boolean }
export function ManagementAccessCard() {
  const qc = useQueryClient()
  const query = useQuery<Policy>({ queryKey: ['security', 'access'], queryFn: async () => (await api.get('/system/security/access')).data })
  const [web, setWeb] = useState(false)
  const [ssh, setSsh] = useState(false)
  const [ranges, setRanges] = useState('')
  useEffect(() => { if (query.data) { setWeb(query.data.web_restricted); setSsh(query.data.ssh_restricted); setRanges(query.data.allowed_cidrs.join('\n')) } }, [query.data])
  const save = useMutation({
    mutationFn: () => api.put('/system/security/access', { web_restricted: web, ssh_restricted: ssh, allowed_cidrs: ranges.split(/[\s,]+/).filter(Boolean) }),
    onSuccess: () => { qc.invalidateQueries({queryKey:['security','access']}); toast.success('Management access policy saved') },
    onError: e => toast.error('Could not save access policy', apiErrorMessage(e)),
  })
  return <Card><CardHeader><CardTitle>Management access</CardTitle></CardHeader><CardContent className="space-y-3">
    <p className="text-xs text-muted">Access is open by default. Add trusted administrator IPs or subnets before enabling restrictions. Probe and agent reporting use their own authentication.</p>
    {query.isError && <p className="text-xs text-danger">Could not load the access policy.</p>}
    <p className="text-xs">Your current source IP: <strong>{query.data?.current_ip || 'Loading…'}</strong></p>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={web} onChange={e=>setWeb(e.target.checked)} />Restrict web administration to the allowed addresses</label>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={ssh} onChange={e=>setSsh(e.target.checked)} />Restrict new SSH connections to the allowed addresses</label>
    <label className="block text-xs font-medium">Allowed IPs and subnets (one per line)<textarea className="mt-1 min-h-28 w-full rounded border border-border bg-surface p-2 font-mono text-xs" value={ranges} onChange={e=>setRanges(e.target.value)} placeholder={'192.168.8.0/24\n10.20.30.15/32\n2001:db8:1234::/48'} /></label>
    <p className="text-xs text-muted">Include your current source address before restricting access. Existing SSH sessions stay connected. Keep VM console access available for recovery.</p>
    <Button disabled={!query.data?.helper_installed || save.isPending} onClick={()=>save.mutate()}>{save.isPending ? 'Saving…' : 'Save access policy'}</Button>
  </CardContent></Card>
}
