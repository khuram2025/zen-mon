import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clock3, KeyRound, Loader2, ShieldX } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Skeleton } from '@/components/ui/Skeleton'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { toast } from '@/components/ui/Toast'
import { EmptyState, QueryError, TableStateRow } from '@/components/servers/tables'

interface RolloutToken {
  id: string
  token_prefix: string
  platform: string
  hostname_hint: string | null
  tags: string[]
  expires_at: string
  max_uses: number
  uses: number
  remaining_uses?: number | null
  unlimited?: boolean
  active?: boolean
  revoked_at: string | null
  created_at: string
  created_by_name: string | null
  policy_name: string | null
  server_name: string | null
}

function tokenState(token: RolloutToken) {
  if (token.revoked_at) return 'revoked' as const
  if (Date.parse(token.expires_at) <= Date.now()) return 'expired' as const
  if (token.max_uses > 0 && token.uses >= token.max_uses) return 'exhausted' as const
  return 'active' as const
}

function StateBadge({ token }: { token: RolloutToken }) {
  const state = tokenState(token)
  if (state === 'active') return <Badge variant="success">Active</Badge>
  if (state === 'revoked') return <Badge variant="danger">Revoked</Badge>
  if (state === 'exhausted') return <Badge variant="warning">Exhausted</Badge>
  return <Badge variant="outline">Expired</Badge>
}

function usageLabel(token: RolloutToken) {
  const unlimited = token.unlimited ?? token.max_uses === 0
  if (unlimited) return `${token.uses.toLocaleString()} used / unlimited`
  const remaining = token.remaining_uses ?? Math.max(0, token.max_uses - token.uses)
  return `${token.uses.toLocaleString()} of ${token.max_uses.toLocaleString()} used (${remaining.toLocaleString()} left)`
}

function expiryLabel(value: string) {
  const expiresAt = Date.parse(value)
  if (!Number.isFinite(expiresAt)) return 'Unknown'
  const remainingMs = expiresAt - Date.now()
  if (remainingMs <= 0) return 'Expired'
  const minutes = Math.ceil(remainingMs / 60_000)
  if (minutes < 60) return `in ${minutes}m`
  const hours = Math.ceil(minutes / 60)
  if (hours < 48) return `in ${hours}h`
  return `in ${Math.ceil(hours / 24)}d`
}

export function RolloutTokensDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const qc = useQueryClient()
  const [showHistory, setShowHistory] = useState(false)
  const [revokeTarget, setRevokeTarget] = useState<RolloutToken | null>(null)

  const tokensQ = useQuery<{ items: RolloutToken[] }>({
    queryKey: ['enrollment-tokens'],
    queryFn: async () => (await api.get('/servers/enrollment-tokens/list', {
      params: { include_expired: true, limit: 100 },
    })).data,
    enabled: open,
    refetchInterval: open ? 15_000 : false,
  })

  const revoke = useMutation({
    mutationFn: async (id: string) =>
      (await api.post(`/servers/enrollment-tokens/${id}/revoke`)).data,
    onSuccess: () => {
      toast.success('Rollout token revoked')
      setRevokeTarget(null)
      qc.invalidateQueries({ queryKey: ['enrollment-tokens'] })
    },
    onError: (error) => toast.error('Could not revoke token', apiErrorMessage(error)),
  })

  const allTokens = tokensQ.data?.items ?? []
  const visibleTokens = useMemo(
    () => showHistory ? allTokens : allTokens.filter((token) => tokenState(token) === 'active'),
    [allTokens, showHistory],
  )

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[85vh] max-w-5xl overflow-y-auto">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <KeyRound className="h-4 w-4 text-primary" /> Rollout tokens
            </DialogTitle>
            <DialogDescription>
              Audit reusable and fixed-size deployment credentials. Raw tokens are never shown
              here; revoke a rollout immediately if its command was shared too broadly.
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="text-xs text-muted">
              {allTokens.filter((token) => tokenState(token) === 'active').length.toLocaleString()} active
              {' / '}{allTokens.length.toLocaleString()} recent
            </div>
            <Button size="sm" variant="outline" onClick={() => setShowHistory((value) => !value)}>
              <Clock3 className="h-3.5 w-3.5" />
              {showHistory ? 'Active only' : 'Show history'}
            </Button>
          </div>

          <div className="overflow-x-auto rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="pl-4">Token / rollout</Th>
                  <Th>Scope</Th>
                  <Th>Usage</Th>
                  <Th>Expires</Th>
                  <Th>Status</Th>
                  <Th className="pr-4 text-right">Action</Th>
                </Tr>
              </THead>
              <TBody>
                {tokensQ.isError ? (
                  <TableStateRow colSpan={6}>
                    <QueryError error={tokensQ.error} onRetry={() => tokensQ.refetch()} />
                  </TableStateRow>
                ) : tokensQ.isLoading ? (
                  <TableStateRow colSpan={6}><Skeleton className="h-24 w-full" /></TableStateRow>
                ) : visibleTokens.length === 0 ? (
                  <TableStateRow colSpan={6}>
                    <EmptyState
                      icon={<KeyRound className="h-7 w-7" />}
                      title={showHistory ? 'No rollout tokens issued' : 'No active rollout tokens'}
                      hint="Downloading an installer or generating a deployment token creates an auditable rollout here."
                    />
                  </TableStateRow>
                ) : visibleTokens.map((token, index) => (
                  <Tr key={token.id} className={index % 2 === 0 ? 'bg-surface2/10' : undefined}>
                    <Td className="py-2 pl-4">
                      <div className="font-mono text-xs">{token.token_prefix}...</div>
                      <div className="mt-0.5 max-w-[260px] truncate text-[11px] text-muted">
                        {token.hostname_hint || token.server_name || 'Fleet rollout'}
                        {token.policy_name ? ` / ${token.policy_name}` : ''}
                      </div>
                    </Td>
                    <Td className="text-xs capitalize text-muted">{token.platform || 'any'}</Td>
                    <Td className="text-xs tabular-nums">{usageLabel(token)}</Td>
                    <Td className="text-xs text-muted" title={new Date(token.expires_at).toLocaleString()}>
                      {expiryLabel(token.expires_at)}
                    </Td>
                    <Td><StateBadge token={token} /></Td>
                    <Td className="pr-4 text-right">
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={tokenState(token) !== 'active' || revoke.isPending}
                        onClick={() => setRevokeTarget(token)}
                      >
                        <ShieldX className="h-3.5 w-3.5" /> Revoke
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </TBody>
            </Table>
          </div>

          <DialogFooter>
            <Button variant="outline" onClick={() => onOpenChange(false)}>Close</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={Boolean(revokeTarget)}
        onOpenChange={(next) => { if (!next) setRevokeTarget(null) }}
        title="Revoke rollout token"
        description="New hosts will no longer be able to enroll with this rollout command. Already enrolled agents keep their own credentials."
        confirmText={revoke.isPending ? 'Revoking...' : 'Revoke token'}
        destructive
        loading={revoke.isPending}
        onConfirm={() => {
          if (revokeTarget) revoke.mutate(revokeTarget.id)
        }}
      />
    </>
  )
}
