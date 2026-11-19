/* A template's interface roles (migrate-193, server/app/services/interface_roles.py): ordered rules, each
 * testing one interface field; the first rule that matches gives the interface its role. Roles select
 * interfaces for alert rules (alert_rules.if_role) and never change whether an interface is monitored.
 * Saved with the template's draft; a customised template with none of its own uses its parent's. */
import { useQuery } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, Copy, Plus, Trash2, Undo2 } from 'lucide-react'
import { Button } from '@/components/ui/Button'
import { Card, CardContent } from '@/components/ui/Card'
import { Input } from '@/components/ui/Input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/Select'
import { Table, TBody, Td, Th, THead, Tr } from '@/components/ui/Table'
import { INTERFACE_ROLES, ROLE_FIELDS, roleLabel, roleRuleProblems } from '@/components/alerts/thresholdModel'
import { type InterfaceRoleRule, templatesApi } from './api'

const fieldLabel = (f: string) => ROLE_FIELDS.find((x) => x.value === f)?.label || f
const fieldHint = (f: string) => ROLE_FIELDS.find((x) => x.value === f)?.hint || ''

export function InterfaceRolesEditor({ roles, onChange, readOnly, parentId, parentName }: {
  roles: InterfaceRoleRule[] | null | undefined
  onChange: (roles: InterfaceRoleRule[] | null) => void
  readOnly: boolean
  parentId: string | null
  parentName?: string | null
}) {
  const inherits = !!parentId && roles == null
  const parent = useQuery({
    queryKey: ['device-template', parentId],
    queryFn: () => templatesApi.get(parentId as string),
    enabled: !!parentId,
  })
  const parentRoles: InterfaceRoleRule[] = parent.data?.interface_roles || []
  const list: InterfaceRoleRule[] = roles || []
  const problems = readOnly || inherits ? [] : roleRuleProblems(list)
  const update = (i: number, patch: Partial<InterfaceRoleRule>) => onChange(list.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))
  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (j < 0 || j >= list.length) return
    const next = [...list]
    ;[next[i], next[j]] = [next[j], next[i]]
    onChange(next)
  }

  return (
    <Card><CardContent className="space-y-3 p-4">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold">Interface roles</h2>
          <p className="text-xs text-muted">
            First match wins; an operator&apos;s role on an interface wins over the template. Alert rules can watch
            only the interfaces of one role (for example uplinks); roles never change whether an interface is monitored.
          </p>
        </div>
        {!readOnly && parentId && !inherits && (
          <Button size="sm" variant="outline" onClick={() => onChange(null)} title="Drop this template's roles and follow the parent's again">
            <Undo2 className="h-3.5 w-3.5" /> Use the parent&apos;s roles
          </Button>
        )}
      </div>

      {inherits ? (
        <div className="space-y-2">
          <div className="flex flex-wrap items-center justify-between gap-2 rounded-md border border-border bg-surface2/40 px-3 py-2 text-sm">
            <span>
              Uses the parent template&apos;s roles
              {parentName ? <> ({parentName})</> : null}, and follows its updates.
            </span>
            {!readOnly && (
              <Button size="sm" variant="outline" disabled={parent.isLoading}
                onClick={() => onChange(JSON.parse(JSON.stringify(parentRoles)))}>
                <Copy className="h-3.5 w-3.5" /> Customise roles
              </Button>
            )}
          </div>
          <RolesTable rows={parentRoles} />
        </div>
      ) : readOnly ? (
        <RolesTable rows={list} />
      ) : (
        <>
          {list.length === 0 ? (
            <p className="text-sm text-muted">No role rules: interfaces have a role only when an operator sets one.</p>
          ) : (
            <ol className="space-y-2" aria-label="Interface role rules in order">
              {list.map((r, i) => (
                <li key={i} className="flex flex-wrap items-start gap-2 rounded-md border border-border/60 bg-surface2/30 p-2">
                  <span className="mt-2 w-5 text-right text-xs tabular-nums text-muted">{i + 1}</span>
                  <Select value={r.role} onValueChange={(v) => update(i, { role: v })}>
                    <SelectTrigger className="h-9 w-40" aria-label={`Role of rule ${i + 1}`}><SelectValue placeholder="Role" /></SelectTrigger>
                    <SelectContent>{INTERFACE_ROLES.map((x) => <SelectItem key={x} value={x}>{roleLabel(x)}</SelectItem>)}</SelectContent>
                  </Select>
                  <Select value={r.field} onValueChange={(v) => update(i, { field: v })}>
                    <SelectTrigger className="h-9 w-52" aria-label={`Field of rule ${i + 1}`}><SelectValue /></SelectTrigger>
                    <SelectContent>{ROLE_FIELDS.map((f) => <SelectItem key={f.value} value={f.value}>{f.label}</SelectItem>)}</SelectContent>
                  </Select>
                  <div className="min-w-[12rem] flex-1">
                    <Input className="h-9 font-mono text-xs" value={r.pattern} maxLength={200} aria-label={`Pattern of rule ${i + 1}`}
                      placeholder={r.field === 'if_type' ? '6,117' : '^(Gi|Te)1/0/4[89]$'}
                      onChange={(e) => update(i, { pattern: e.target.value })} />
                    <div className="mt-0.5 text-2xs text-muted">{fieldHint(r.field)}</div>
                  </div>
                  <div className="flex items-center gap-0.5">
                    <Button size="icon" variant="ghost" className="h-8 w-8" aria-label="Move up" disabled={i === 0} onClick={() => move(i, -1)}><ArrowUp className="h-3.5 w-3.5" /></Button>
                    <Button size="icon" variant="ghost" className="h-8 w-8" aria-label="Move down" disabled={i === list.length - 1} onClick={() => move(i, 1)}><ArrowDown className="h-3.5 w-3.5" /></Button>
                    <Button size="icon" variant="ghost" className="h-8 w-8 text-muted hover:text-danger" aria-label="Remove rule"
                      onClick={() => onChange(list.filter((_, idx) => idx !== i))}><Trash2 className="h-3.5 w-3.5" /></Button>
                  </div>
                </li>
              ))}
            </ol>
          )}
          <Button size="sm" variant="outline" disabled={list.length >= 50}
            onClick={() => onChange([...list, { role: 'uplink', field: 'if_name', pattern: '' }])}>
            <Plus className="h-3.5 w-3.5" /> Add role rule
          </Button>
          {problems.length > 0 && (
            <ul className="space-y-0.5 text-xs text-warning-fg">{problems.map((p) => <li key={p}>{p}</li>)}</ul>
          )}
        </>
      )}
    </CardContent></Card>
  )
}

function RolesTable({ rows }: { rows: InterfaceRoleRule[] }) {
  if (!rows.length) return <p className="text-sm text-muted">No role rules.</p>
  return (
    <div className="overflow-x-auto rounded-md border border-border">
      <Table className="text-sm">
        <THead className="bg-surface2/60"><Tr><Th className="w-8">#</Th><Th>Role</Th><Th>Field</Th><Th>Pattern</Th></Tr></THead>
        <TBody>
          {rows.map((r, i) => (
            <Tr key={i}>
              <Td className="tabular-nums text-muted">{i + 1}</Td>
              <Td>{roleLabel(r.role)}</Td>
              <Td className="text-xs text-muted">{fieldLabel(r.field)}</Td>
              <Td className="font-mono text-xs">{r.pattern}</Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  )
}
