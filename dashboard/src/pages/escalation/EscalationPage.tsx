/**
 * Alerting → Escalation & On-call (/escalation).
 *
 * One place for who is paged, when, and how warnings are grouped
 * (work/ESCALATION-PLAN.md "UI"; field report "Proposed escalation matrix").
 * The page needs alerts.view; every change needs alerts.manage, and a role
 * without it sees the same tabs read-only.
 */
import { useSearchParams } from 'react-router-dom'
import { CalendarClock, ChevronsUp, Grid3x3, Layers, ShieldCheck, Timer } from 'lucide-react'
import { useCan } from '@/stores/auth'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { ESCALATION_TABS, tabFromSearch, type EscalationTab } from '@/features/escalation/model'
import { MatrixTab } from './MatrixTab'
import { PoliciesTab } from './PoliciesTab'
import { OncallTab } from './OncallTab'
import { ResponseTargetsTab } from './ResponseTargetsTab'
import { GroupingTab } from './GroupingTab'
import { CoverageTab } from './CoverageTab'

const TAB_META: Record<EscalationTab, { label: string; icon: typeof Grid3x3 }> = {
  matrix: { label: 'Matrix', icon: Grid3x3 },
  policies: { label: 'Policies', icon: ChevronsUp },
  oncall: { label: 'On-call', icon: CalendarClock },
  targets: { label: 'Response targets', icon: Timer },
  grouping: { label: 'Grouping', icon: Layers },
  coverage: { label: 'Coverage', icon: ShieldCheck },
}

export function EscalationPage() {
  const can = useCan()
  const canManage = can('alerts.manage')
  const [params, setParams] = useSearchParams()
  const tab = tabFromSearch(params)

  const setTab = (v: string) => {
    const next = new URLSearchParams()
    if (v !== 'matrix') next.set('tab', v)
    setParams(next, { replace: true })
  }
  /** Open another tab, optionally focused on one record (?id=). */
  const go = (t: EscalationTab, id?: string | null) => {
    const next = new URLSearchParams()
    if (t !== 'matrix') next.set('tab', t)
    if (id) next.set('id', id)
    setParams(next)
  }
  const focusId = params.get('id')

  return (
    <div className="space-y-5">
      <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
        <div className="flex items-center gap-2">
          <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
            <ChevronsUp className="h-5 w-5" aria-hidden />
          </div>
          <div>
            <h1 className="text-xl font-semibold tracking-tight">Escalation &amp; On-call</h1>
            <p className="text-xs text-muted">Who is paged for each priority, when unacknowledged incidents escalate, who is on call, and how warnings are grouped.</p>
          </div>
        </div>
        {!canManage && (
          <p role="note" className="rounded-md border border-border bg-surface2/50 px-3 py-1.5 text-xs text-text2">
            Read-only: changing policies, schedules and targets needs the <span className="font-mono">alerts.manage</span> permission.
          </p>
        )}
      </div>

      <Tabs value={tab} onValueChange={setTab}>
        <TabsList className="h-auto flex-wrap gap-1 bg-surface2/50 p-1">
          {ESCALATION_TABS.map((t) => {
            const M = TAB_META[t]
            return (
              <TabsTrigger key={t} value={t} className="gap-1.5">
                <M.icon className="h-3.5 w-3.5" aria-hidden />
                <span>{M.label}</span>
              </TabsTrigger>
            )
          })}
        </TabsList>
        <TabsContent value="matrix"><MatrixTab canManage={canManage} go={go} /></TabsContent>
        <TabsContent value="policies"><PoliciesTab canManage={canManage} focusId={tab === 'policies' ? focusId : null} /></TabsContent>
        <TabsContent value="oncall"><OncallTab canManage={canManage} focusId={tab === 'oncall' ? focusId : null} /></TabsContent>
        <TabsContent value="targets"><ResponseTargetsTab canManage={canManage} /></TabsContent>
        <TabsContent value="grouping"><GroupingTab canManage={canManage} focusId={tab === 'grouping' ? focusId : null} /></TabsContent>
        <TabsContent value="coverage"><CoverageTab go={go} /></TabsContent>
      </Tabs>
    </div>
  )
}
