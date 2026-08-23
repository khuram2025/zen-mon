import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, CheckCircle2, FilePlus2, Loader2, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage, cn } from '@/lib/utils'
import { Badge } from '@/components/ui/Badge'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { FormField } from '@/components/ui/FormField'
import { Input } from '@/components/ui/Input'
import { Skeleton } from '@/components/ui/Skeleton'
import { Textarea } from '@/components/ui/Textarea'
import { toast } from '@/components/ui/Toast'
import { useReportCatalog, type CatalogSection } from '@/hooks/useReportCatalog'

export default function ReportBuilder() {
  const [searchParams] = useSearchParams()
  const editId = searchParams.get('edit')
  const navigate = useNavigate()
  const qc = useQueryClient()
  const { data: catalog, isLoading, error } = useReportCatalog()

  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [confirmDelete, setConfirmDelete] = useState(false)
  const [prefilled, setPrefilled] = useState(false)

  const editing = editId ? catalog?.custom.find((c) => c.id === editId) : undefined

  // Load current values once when editing (catalog arrives async).
  useEffect(() => {
    if (!editId || !editing || prefilled) return
    setName(editing.name)
    setDescription(editing.description || '')
    setSelected(new Set(editing.sections))
    setPrefilled(true)
  }, [editId, editing, prefilled])

  // Group sections by category, keeping registry order within and across groups.
  const groups = useMemo(() => {
    const out: { category: string; sections: CatalogSection[] }[] = []
    for (const s of catalog?.sections ?? []) {
      const g = out.find((x) => x.category === s.category)
      if (g) g.sections.push(s)
      else out.push({ category: s.category, sections: [s] })
    }
    return out
  }, [catalog])

  const toggle = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  // Persist in registry order regardless of click order.
  const orderedSelection = useMemo(
    () => (catalog?.sections ?? []).filter((s) => selected.has(s.id)).map((s) => s.id),
    [catalog, selected],
  )

  const save = useMutation({
    mutationFn: async () => {
      const payload = {
        name: name.trim(),
        description: description.trim() || null,
        sections: orderedSelection,
      }
      if (editId) {
        await api.put(`/reports/custom/${editId}`, payload)
        return { id: editId }
      }
      return (await api.post('/reports/custom', payload)).data as { id: string }
    },
    onSuccess: (d) => {
      toast.success(editId ? 'Custom report updated' : 'Custom report created')
      qc.invalidateQueries({ queryKey: ['reports', 'catalog'] })
      navigate(`/reports/view/custom?custom_id=${d.id}`)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const del = useMutation({
    mutationFn: async () => api.delete(`/reports/custom/${editId}`),
    onSuccess: () => {
      toast.success('Custom report deleted')
      qc.invalidateQueries({ queryKey: ['reports', 'catalog'] })
      navigate('/reports')
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  const missingEdit = !!editId && !!catalog && !editing

  return (
    <div className="mx-auto max-w-4xl space-y-5">
      {/* Header */}
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <Link
            to="/reports"
            className="inline-flex items-center gap-1 text-xs font-medium text-muted transition-colors hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Report library
          </Link>
          <h1 className="mt-1 flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FilePlus2 className="h-6 w-6 text-primary" />
            {editId ? 'Edit custom report' : 'New custom report'}
          </h1>
          <p className="mt-1 text-sm text-muted">
            Pick the sections to include — view the result live, export it, or schedule delivery.
          </p>
        </div>
        <Badge variant={selected.size > 0 ? 'info' : 'outline'} className="self-start sm:self-auto">
          {selected.size} section{selected.size === 1 ? '' : 's'} selected
        </Badge>
      </div>

      {error && (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          Failed to load the report catalog: {apiErrorMessage(error)}
        </div>
      )}

      {missingEdit ? (
        <div className="rounded-lg border border-danger/40 bg-danger/10 px-4 py-3 text-sm text-danger">
          Custom report not found — it may have been deleted.{' '}
          <Link to="/reports" className="font-medium underline">
            Back to the report library
          </Link>
        </div>
      ) : isLoading ? (
        <div className="space-y-3">
          <Skeleton className="h-10 rounded-lg" />
          <Skeleton className="h-20 rounded-lg" />
          <Skeleton className="h-[320px] rounded-lg" />
        </div>
      ) : (
        <form
          className="space-y-5"
          onSubmit={(e) => {
            e.preventDefault()
            save.mutate()
          }}
        >
          <div className="rounded-lg border border-border bg-surface p-5 shadow-card dark:shadow-card-dark">
            <div className="space-y-4">
              <FormField label="Report name" required>
                <Input
                  required
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder="Weekly network & application review"
                />
              </FormField>
              <FormField label="Description" hint="Optional — shown on the report cover and library card">
                <Textarea
                  rows={2}
                  value={description}
                  onChange={(e) => setDescription(e.target.value)}
                  placeholder="Availability, traffic and APM highlights for the operations weekly."
                />
              </FormField>
            </div>
          </div>

          <div className="rounded-lg border border-border bg-surface p-5 shadow-card dark:shadow-card-dark">
            <div className="mb-3 flex items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold tracking-tight text-text">Sections</h2>
                <p className="mt-0.5 text-xs text-muted">Sections render in the order shown.</p>
              </div>
            </div>

            <div className="space-y-4">
              {groups.map((group) => (
                <div key={group.category}>
                  <p className="mb-2 text-[11px] font-semibold uppercase tracking-wider text-muted">{group.category}</p>
                  <div className="grid gap-2 sm:grid-cols-2">
                    {group.sections.map((s) => {
                      const checked = selected.has(s.id)
                      return (
                        <label
                          key={s.id}
                          className={cn(
                            'flex cursor-pointer items-start gap-3 rounded-lg border p-3 transition-colors',
                            checked
                              ? 'border-primary/60 bg-primary/5'
                              : 'border-border bg-surface hover:border-primary/40',
                          )}
                        >
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={() => toggle(s.id)}
                            className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                          />
                          <span className="min-w-0">
                            <span className={cn('block text-sm font-medium', checked ? 'text-text' : 'text-text2')}>
                              {s.title}
                            </span>
                            <span className="mt-0.5 block text-xs text-muted">{s.description}</span>
                          </span>
                        </label>
                      )
                    })}
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="flex items-center gap-2">
            <Button type="submit" disabled={save.isPending || !name.trim() || selected.size === 0}>
              {save.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : <CheckCircle2 className="h-4 w-4" />}
              {editId ? 'Save changes' : 'Create report'}
            </Button>
            <Button type="button" variant="outline" onClick={() => navigate('/reports')}>
              Cancel
            </Button>
            {editId && (
              <Button
                type="button"
                variant="ghost"
                className="ml-auto text-muted hover:text-danger"
                onClick={() => setConfirmDelete(true)}
              >
                <Trash2 className="h-4 w-4" /> Delete report
              </Button>
            )}
          </div>
        </form>
      )}

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title="Delete custom report"
        description={
          <>
            Delete <span className="font-semibold text-text">{editing?.name || 'this report'}</span>? Schedules
            pointing at it will stop delivering.
          </>
        }
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => del.mutate()}
      />
    </div>
  )
}
