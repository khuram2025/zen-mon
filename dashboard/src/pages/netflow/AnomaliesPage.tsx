import { useSearchParams } from 'react-router-dom'
import { ShieldAlert } from 'lucide-react'
import { TimeRangePicker } from '@/components/TimeRangePicker'
import { NetflowPageHeader } from '@/components/netflow/NetflowPageHeader'
import { useNfWindow } from '@/lib/netflow/window'
import { cn } from '@/lib/utils'
import { FindingsTab } from './anomalies/FindingsTab'
import { SettingsTab } from './anomalies/SettingsTab'
import { ExceptionsTab } from './anomalies/ExceptionsTab'
import { ThreatIntelTab } from './anomalies/ThreatIntelTab'

const TABS = [
  { key: 'findings', label: 'Findings', short: 'Findings' },
  { key: 'settings', label: 'Detection settings', short: 'Settings' },
  { key: 'exceptions', label: 'Exceptions', short: 'Exceptions' },
  { key: 'intel', label: 'Threat intel', short: 'Intel' },
] as const
type TabKey = (typeof TABS)[number]['key']

/**
 * Anomalies (B5): findings recorded by the detection engine with their evidence and lifecycle,
 * an ad-hoc evaluation of any window, which detectors actually ran, their thresholds, approved
 * exceptions and threat-intelligence feeds.
 */
export function NetflowAnomaliesPage() {
  const [sp, setSp] = useSearchParams()
  const tabParam = sp.get('tab')
  const tab: TabKey = TABS.some((t) => t.key === tabParam) ? (tabParam as TabKey) : 'findings'
  const { window: w, rangeIdx, isCustom, setPreset, setCustom, range } = useNfWindow()
  const setTab = (t: TabKey) => {
    const next = new URLSearchParams(sp)
    if (t === 'findings') next.delete('tab')
    else next.set('tab', t)
    setSp(next, { replace: true })
  }
  return (
    <div className="space-y-4">
      <NetflowPageHeader
        title="Anomalies"
        icon={ShieldAlert}
        description="Flow-derived detections with their evidence: scans, floods, sensitive services reached on the internet, traffic outliers and threat-intel matches. Heuristic candidates, not confirmed incidents."
        actions={
          tab === 'findings' ? (
            <TimeRangePicker
              rangeIdx={rangeIdx}
              isCustom={isCustom}
              customFrom={range.fromISO}
              customTo={range.toISO}
              onPreset={setPreset}
              onCustom={setCustom}
            />
          ) : undefined
        }
      />
      <div role="tablist" aria-label="Anomalies sections" className="flex gap-1 overflow-x-auto border-b border-border">
        {TABS.map((t) => (
          <button
            key={t.key}
            id={`nf-anom-tab-${t.key}`}
            type="button"
            role="tab"
            aria-selected={tab === t.key}
            aria-controls={`nf-anom-panel-${t.key}`}
            tabIndex={tab === t.key ? 0 : -1}
            onClick={() => setTab(t.key)}
            onKeyDown={(e) => {
              const i = TABS.findIndex((x) => x.key === tab)
              if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
                e.preventDefault()
                const next = TABS[(i + (e.key === 'ArrowRight' ? 1 : TABS.length - 1)) % TABS.length]
                setTab(next.key)
                document.getElementById(`nf-anom-tab-${next.key}`)?.focus()
              }
            }}
            className={cn(
              '-mb-px whitespace-nowrap border-b-2 px-3 py-2 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50',
              tab === t.key ? 'border-primary text-text' : 'border-transparent text-text2 hover:text-text',
            )}
          >
            <span className="hidden min-[480px]:inline">{t.label}</span>
            <span className="min-[480px]:hidden" aria-hidden>{t.short}</span>
            <span className="sr-only min-[480px]:hidden">{t.label}</span>
          </button>
        ))}
      </div>
      <div role="tabpanel" id={`nf-anom-panel-${tab}`} aria-labelledby={`nf-anom-tab-${tab}`}>
        {tab === 'findings' && <FindingsTab w={w} />}
        {tab === 'settings' && <SettingsTab />}
        {tab === 'exceptions' && <ExceptionsTab tz={w.tz} />}
        {tab === 'intel' && <ThreatIntelTab tz={w.tz} />}
      </div>
    </div>
  )
}
