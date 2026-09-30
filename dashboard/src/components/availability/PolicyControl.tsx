/**
 * "Configured target" label with the appliance-wide availability policy
 * editor (GET/PUT /reports/availability-policy). Editing is offered to
 * operators and administrators; the server enforces the same rule.
 */
import { useEffect, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Loader2, Settings2, Target } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { hasPermission, useAuth } from '@/stores/auth'
import { useAvailabilityPolicy } from '@/hooks/useReports'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { FormField } from '@/components/ui/FormField'
import { toast } from '@/components/ui/Toast'
import { fmtTarget } from './format'

const POLICY_ROLES = new Set(['admin', 'owner', 'operator', 'editor'])

export function useCanEditAvailabilityPolicy(): boolean {
  const user = useAuth((s) => s.user)
  if (!user) return false
  return POLICY_ROLES.has(user.role) || hasPermission(user, 'settings.manage')
}

export function PolicyControl({
  effectiveTarget, label, className,
}: {
  effectiveTarget: number | null
  /** e.g. "per service (default 99.9%)". Falls back to the effective target. */
  label?: string
  className?: string
}) {
  const canEdit = useCanEditAvailabilityPolicy()
  const policy = useAvailabilityPolicy()
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState('')
  const qc = useQueryClient()
  const supported = !policy.isError
  const current = policy.data?.default_target_pct ?? null

  useEffect(() => {
    if (open) setDraft(current != null ? String(current) : effectiveTarget != null ? String(effectiveTarget) : '99.9')
  }, [open, current, effectiveTarget])

  const value = Number(draft)
  const invalid = !draft.trim() || !Number.isFinite(value) || value < 50 || value > 100

  const save = useMutation({
    mutationFn: async () => (await api.put('/reports/availability-policy', { default_target_pct: value })).data,
    onSuccess: () => {
      toast.success('Default availability target saved', `New target ${fmtTarget(value)} applies to services without their own target.`)
      qc.invalidateQueries({ queryKey: ['availability-policy'] })
      qc.invalidateQueries({ queryKey: ['report'] })
      setOpen(false)
    },
    onError: (e: any) => toast.error('Could not save the default target', apiErrorMessage(e)),
  })

  const text = label ?? (effectiveTarget != null ? fmtTarget(effectiveTarget) : 'not set')
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          'inline-flex min-h-[36px] items-center gap-1.5 rounded-lg border border-border bg-surface px-2.5 text-sm text-text2 transition hover:border-primary/40 hover:text-text',
          className,
        )}
        title={canEdit ? 'Change the default availability target' : 'View the availability policy'}
      >
        <Target aria-hidden className="h-4 w-4 text-muted" />
        Configured target {text}
        {canEdit && supported && <Settings2 aria-hidden className="h-4 w-4 text-muted" />}
      </button>
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Availability policy</DialogTitle>
            <DialogDescription>
              The default target applies to every service without its own target and to the device estate. It is a
              configured objective, not a contractual SLA.
            </DialogDescription>
          </DialogHeader>
          {!supported ? (
            <p className="text-base text-muted">
              This appliance version does not expose a configurable policy. Figures are judged against {fmtTarget(effectiveTarget)}.
            </p>
          ) : canEdit ? (
            <form
              id="availability-policy-form"
              onSubmit={(e) => { e.preventDefault(); if (!invalid) save.mutate() }}
              className="space-y-2"
            >
              <FormField
                label="Default target (%)"
                error={draft && invalid ? 'Enter a value from 50 to 100.' : null}
                hint="Per-service targets are set on each service check."
              >
                <Input
                  type="number"
                  inputMode="decimal"
                  min={50}
                  max={100}
                  step="any"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  autoFocus
                />
              </FormField>
            </form>
          ) : (
            <p className="text-base text-text2">
              Default target {policy.isLoading ? '…' : fmtTarget(current ?? effectiveTarget)}. Only operators and
              administrators can change it.
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setOpen(false)}>Close</Button>
            {supported && canEdit && (
              <Button type="submit" form="availability-policy-form" disabled={invalid || save.isPending}>
                {save.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
                Save
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
