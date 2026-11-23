/**
 * The condition builder of a combined server rule (multi-condition rules phase 1, docs/multi-condition-rules.md):
 * conditions joined by All (AND) / Any (OR), with one level of groups.
 */
import { Plus, Trash2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { cn } from '@/lib/utils'
import {
  COMBINABLE_SERVER_METRICS, MAX_TREE_CONDITIONS, TREE_INSTANCE, isTreeGroup, newTreeCondition, serverMetric,
  serverMetricKind, serverOperators, serverTreeLabel, treeConditions,
  type ServerTree, type ServerTreeCondition, type ServerTreeGroup,
} from './serverAlertRule'

type Logic = 'AND' | 'OR'

function LogicSwitch({ value, onChange, label }: { value: Logic; onChange: (v: Logic) => void; label: string }) {
  return (
    <div className="flex rounded-md border border-border bg-surface2 p-0.5 text-xs" role="group" aria-label={label}>
      {(['AND', 'OR'] as const).map((l) => (
        <button key={l} type="button" aria-pressed={value === l} onClick={() => onChange(l)}
          className={cn('rounded px-2.5 py-1 font-medium', value === l ? 'bg-primary text-white' : 'text-muted')}>
          {l === 'AND' ? 'All (AND)' : 'Any (OR)'}
        </button>
      ))}
    </div>
  )
}

function ConditionRow({ c, onChange, onRemove, canRemove }: {
  c: ServerTreeCondition
  onChange: (c: ServerTreeCondition) => void
  onRemove: () => void
  canRemove: boolean
}) {
  const kind = serverMetricKind(c.metric)
  const spec = serverMetric(c.metric)
  const unit = spec?.unit?.trim()
  const instance = TREE_INSTANCE[c.metric]
  const ops = serverOperators(c.metric)
  return (
    <div className="flex flex-wrap items-end gap-2 rounded-md border border-border/60 bg-surface p-2">
      <label className="min-w-[200px] flex-1 space-y-1">
        <span className="text-2xs font-medium uppercase tracking-wider text-muted">Watch</span>
        <Select value={c.metric} onValueChange={(v) => onChange({ ...newTreeCondition(v), id: c.id })}>
          <SelectTrigger aria-label="Condition metric"><SelectValue /></SelectTrigger>
          <SelectContent>
            {COMBINABLE_SERVER_METRICS.map((m) => <SelectItem key={m.value} value={m.value}>{m.label}</SelectItem>)}
          </SelectContent>
        </Select>
      </label>
      {kind === 'pct' && (
        <>
          <label className="w-20 space-y-1">
            <span className="text-2xs font-medium uppercase tracking-wider text-muted">Operator</span>
            <Select value={c.operator} onValueChange={(v) => onChange({ ...c, operator: v })}>
              <SelectTrigger aria-label="Condition operator"><SelectValue /></SelectTrigger>
              <SelectContent>
                {[...ops, ...(ops.includes(c.operator) ? [] : [c.operator])].map((op) => <SelectItem key={op} value={op}>{op}</SelectItem>)}
              </SelectContent>
            </Select>
          </label>
          <label className="w-28 space-y-1">
            <span className="text-2xs font-medium uppercase tracking-wider text-muted">Limit{unit ? ` (${unit})` : ''}</span>
            <Input type="number" step="any" min={spec?.min} max={spec?.max} aria-label="Condition limit" value={c.threshold}
              onChange={(e) => onChange({ ...c, threshold: Number(e.target.value) })} />
          </label>
          <label className="w-28 space-y-1">
            <span className="text-2xs font-medium uppercase tracking-wider text-muted">Clear at</span>
            <Input type="number" step="any" aria-label="Condition clear level" placeholder="auto" value={c.reset_threshold ?? ''}
              onChange={(e) => onChange({ ...c, reset_threshold: e.target.value === '' ? null : Number(e.target.value) })} />
          </label>
        </>
      )}
      {(kind === 'svc' || kind === 'proc') && (
        <label className="min-w-[160px] flex-1 space-y-1">
          <span className="text-2xs font-medium uppercase tracking-wider text-muted">{kind === 'svc' ? 'Service name' : 'Process name'}</span>
          <Input value={c.target || ''} aria-label={kind === 'svc' ? 'Service name' : 'Process name'}
            placeholder={kind === 'svc' ? 'e.g. nginx' : 'e.g. nginx.exe'} onChange={(e) => onChange({ ...c, target: e.target.value })} />
        </label>
      )}
      {kind === 'auto' && (
        <label className="min-w-[160px] flex-1 space-y-1">
          <span className="text-2xs font-medium uppercase tracking-wider text-muted">Leave out (patterns)</span>
          <Input value={c.target || ''} aria-label="Services to leave out" placeholder="gupdate*, edgeupdate*"
            onChange={(e) => onChange({ ...c, target: e.target.value })} />
        </label>
      )}
      {instance && (
        <label className="w-36 space-y-1">
          <span className="text-2xs font-medium uppercase tracking-wider text-muted">Only this {instance.split(' ')[0]}</span>
          <Input value={c.target || ''} aria-label={`Only this ${instance}`} placeholder="any"
            onChange={(e) => onChange({ ...c, target: e.target.value })} />
        </label>
      )}
      <Button type="button" variant="ghost" size="icon" className="h-9 w-9 text-muted hover:text-danger" disabled={!canRemove}
        onClick={onRemove} aria-label="Remove condition" title="Remove condition">
        <Trash2 className="h-4 w-4" />
      </Button>
    </div>
  )
}

export function ServerConditionTree({ tree, onChange, onSingle }: {
  tree: ServerTree
  onChange: (tree: ServerTree) => void
  /** Back to a single-condition rule (keeps the first condition). */
  onSingle: () => void
}) {
  const count = treeConditions(tree).length
  const full = count >= MAX_TREE_CONDITIONS
  const setItem = (i: number, item: ServerTreeCondition | ServerTreeGroup) =>
    onChange({ ...tree, items: tree.items.map((x, idx) => (idx === i ? item : x)) })
  const removeItem = (i: number) => onChange({ ...tree, items: tree.items.filter((_, idx) => idx !== i) })

  return (
    <div className="space-y-3 rounded-lg border border-border/60 bg-surface2/30 p-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted">Alert when</span>
        <LogicSwitch value={tree.logic} onChange={(logic) => onChange({ ...tree, logic })} label="How the conditions combine" />
      </div>

      {tree.items.map((item, i) => isTreeGroup(item) ? (
        <div key={i} className="space-y-2 rounded-md border border-dashed border-border p-2">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span className="text-xs font-medium text-muted">Group</span>
            <div className="flex items-center gap-1">
              <LogicSwitch value={item.logic} onChange={(logic) => setItem(i, { ...item, logic })} label="How the group's conditions combine" />
              <Button type="button" variant="ghost" size="icon" className="h-8 w-8 text-muted hover:text-danger"
                onClick={() => removeItem(i)} aria-label="Remove group" title="Remove group">
                <Trash2 className="h-4 w-4" />
              </Button>
            </div>
          </div>
          {item.items.map((c, j) => (
            <ConditionRow key={j} c={c} canRemove={item.items.length > 2}
              onChange={(next) => setItem(i, { ...item, items: item.items.map((x, k) => (k === j ? next : x)) })}
              onRemove={() => setItem(i, { ...item, items: item.items.filter((_, k) => k !== j) })} />
          ))}
          <Button type="button" variant="ghost" size="sm" disabled={full}
            onClick={() => setItem(i, { ...item, items: [...item.items, newTreeCondition('host_memory_pct')] })}>
            <Plus className="h-3.5 w-3.5" /> Add to group
          </Button>
        </div>
      ) : (
        <ConditionRow key={i} c={item} canRemove={count > 2}
          onChange={(next) => setItem(i, next)} onRemove={() => removeItem(i)} />
      ))}

      <div className="flex flex-wrap items-center gap-2">
        <Button type="button" variant="outline" size="sm" disabled={full}
          onClick={() => onChange({ ...tree, items: [...tree.items, newTreeCondition('host_memory_pct')] })}>
          <Plus className="h-3.5 w-3.5" /> Add condition
        </Button>
        <Button type="button" variant="outline" size="sm" disabled={count + 2 > MAX_TREE_CONDITIONS}
          onClick={() => onChange({ ...tree, items: [...tree.items, {
            logic: tree.logic === 'AND' ? 'OR' : 'AND',
            items: [newTreeCondition('host_cpu_pct'), newTreeCondition('host_memory_pct')],
          }] })}>
          <Plus className="h-3.5 w-3.5" /> Add group
        </Button>
        <Button type="button" variant="ghost" size="sm" onClick={onSingle}>Use one condition</Button>
      </div>

      <p className="text-xs text-text2">
        <span className="font-medium">Reads:</span> {serverTreeLabel(tree)}
      </p>
      <p className="text-xs text-muted">
        One alert per server, naming the conditions that matched. Each condition is judged on its current reading once a
        minute; an unknown reading never raises or clears the alert. Restart, shutdown and agent offline need a rule of
        their own. Instance conditions (filesystem, disk, network interface) without a name are true when any instance is.
      </p>
    </div>
  )
}
