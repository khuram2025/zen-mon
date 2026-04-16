import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CreditCard, RotateCw } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { toast } from '@/components/ui/Toast'

export function SubscriptionPage() {
  const qc = useQueryClient()
  const { data } = useQuery<any>({
    queryKey: ['subscription'],
    queryFn: async () => (await api.get('/subscription')).data,
  })
  const refresh = useMutation({
    mutationFn: async () => (await api.post('/subscription/refresh-subscription')).data,
    onSuccess: () => { toast.success('Subscription refreshed'); qc.invalidateQueries({ queryKey: ['subscription'] }) },
    onError: (e: any) => toast.error('Refresh failed', apiErrorMessage(e)),
  })

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="flex items-center gap-2 text-xl font-semibold tracking-tight">
            <CreditCard className="h-5 w-5 text-primary" /> Subscription
          </h1>
          <p className="text-xs text-muted">Plan details and usage limits</p>
        </div>
        <Button variant="outline" onClick={() => refresh.mutate()} disabled={refresh.isPending}>
          <RotateCw className={`h-4 w-4 ${refresh.isPending ? 'animate-spin' : ''}`} /> Refresh
        </Button>
      </div>

      <Card>
        <CardContent className="space-y-3 pt-6 text-sm">
          {[
            ['Plan', data?.plan || 'trial'],
            ['Status', data?.status || '—'],
            ['Started', data?.started_at || '—'],
            ['Expires', data?.expires_at || '—'],
            ['Max devices', String(data?.max_devices ?? '—')],
            ['Max service checks', String(data?.max_service_checks ?? '—')],
            ['Max users', String(data?.max_users ?? '—')],
          ].map(([k, v]) => (
            <div key={k} className="flex items-baseline justify-between gap-4 border-b border-border pb-2 last:border-0 last:pb-0">
              <span className="text-xs uppercase tracking-wider text-muted">{k}</span>
              <span>{v}</span>
            </div>
          ))}
        </CardContent>
      </Card>
    </div>
  )
}
