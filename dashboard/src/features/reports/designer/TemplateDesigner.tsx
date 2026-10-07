/**
 * Template designer: section library · outline · inspector, with template
 * details above and an optional live preview.
 *
 * ≥1280px: three panes (with the preview on, the library moves into a sheet;
 * the inspector stays inline from 1536px and is a sheet below that).
 * Smaller screens: the library and inspector open as sheets and the preview
 * replaces the outline while it is on.
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import {
  AlertCircle, ArrowLeft, CalendarClock, ChevronRight, CopyPlus, Eye, Info, LayoutTemplate, Library, Loader2,
  MoreHorizontal, Plus, RotateCcw, Save, Sparkles, Trash2, X,
} from 'lucide-react'
import './designer.css'
import { cn, apiErrorMessage } from '@/lib/utils'
import { describeFailure } from '@/lib/access'
import type { Branding } from '@/lib/branding'
import { Button } from '@/components/ui/Button'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import {
  Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle,
} from '@/components/ui/Dialog'
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/DropdownMenu'
import { Input } from '@/components/ui/Input'
import { toast } from '@/components/ui/Toast'
import { useDeleteTemplate, useSaveTemplate } from '../api'
import { PERIODS } from '../periods'
import type { CatalogTemplate, CustomTemplate, PeriodId, ReportCatalog, SectionDef } from '../types'
import { idList } from './fields'
import { Inspector, optionFieldId, titleFieldId } from './Inspector'
import {
  addPosition, draftSignature, entriesFrom, insertEntry, MAX_SECTIONS, NAME_MAX, newUid, positionText, toTemplateBody,
  usageCounts, validateDraft, type DesignerEntry, type DesignerMode, type Draft, type Issue, type SectionIndex,
} from './model'
import { Outline, type OutlineHandle } from './Outline'
import { PreviewPane } from './PreviewPane'
import { SectionLibrary } from './SectionLibrary'
import { FIELD_IDS, TemplateSettings } from './TemplateSettings'
import { SegmentedRadio, Sheet, useAnnouncer, useMediaQuery } from './ui'
import { useUnsavedGuard } from './useUnsavedGuard'

const MODE_LABEL: Record<DesignerMode, string> = {
  new: 'New template',
  from: 'Customize a built-in template',
  edit: 'Edit template',
  clone: 'Copy of a template',
}

const PREVIEW_PREF = 'zp.reports.designer.preview'

function readPreviewPref(): boolean {
  try {
    return localStorage.getItem(PREVIEW_PREF) === '1'
  } catch {
    return false
  }
}

function writePreviewPref(on: boolean): void {
  try {
    localStorage.setItem(PREVIEW_PREF, on ? '1' : '0')
  } catch {
    /* preference only */
  }
}

const pane = 'flex min-h-0 flex-col overflow-hidden rounded-xl border border-border bg-surface shadow-card dark:shadow-card-dark'

export interface TemplateDesignerProps {
  mode: DesignerMode
  initial: Draft
  catalog: ReportCatalog
  branding: Branding
  /** The organisation template being edited (edit) or copied (clone). */
  template?: CustomTemplate | null
  /** The built-in template being customised (from). */
  builtin?: CatalogTemplate | null
}

type SaveOutcome = 'ok' | 'invalid' | 'error'

export function TemplateDesigner({ mode, initial, catalog, branding, template, builtin }: TemplateDesignerProps) {
  const wide = useMediaQuery('(min-width: 1280px)')
  const ultra = useMediaQuery('(min-width: 1536px)')
  const roomy = useMediaQuery('(min-width: 640px)')
  const sections = useMemo<SectionIndex>(() => Object.fromEntries(catalog.sections.map((s) => [s.id, s])), [catalog.sections])
  const sectionMeta = sections as Record<string, SectionDef>
  const known = useMemo(() => ({ categories: catalog.categories.map((c) => c.id), periods: PERIODS.map((p) => p.id) }), [catalog.categories])

  const [draft, setDraft] = useState<Draft>(initial)
  const [baseline] = useState(() => draftSignature(initial, sections))
  const dirty = useMemo(() => draftSignature(draft, sections), [draft, sections]) !== baseline
  const validation = useMemo(() => validateDraft(draft, sections, known), [draft, sections, known])

  const editing = mode === 'edit'
  const canEdit = !editing || !!template?.can_edit
  const canChangeVisibility = !editing || !!template?.can_change_visibility

  const [selectedUid, setSelectedUid] = useState<string | null>(initial.entries[0]?.uid ?? null)
  const [attempted, setAttempted] = useState(false)
  const [nameTouched, setNameTouched] = useState(false)
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)
  const [detailsOpen, setDetailsOpen] = useState(mode === 'new')
  const [previewOn, setPreviewOn] = useState(readPreviewPref)
  const [libraryOpen, setLibraryOpen] = useState(false)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [previewPeriod, setPreviewPeriod] = useState<PeriodId>(initial.defaultPeriod ?? 'last_24h')
  const [periodTouched, setPeriodTouched] = useState(false)
  const [saveAsOpen, setSaveAsOpen] = useState(false)
  const [discardOpen, setDiscardOpen] = useState(false)
  const [deleteStep, setDeleteStep] = useState<null | 'confirm' | 'force'>(null)

  const { announce, region } = useAnnouncer()
  const outlineRef = useRef<OutlineHandle>(null)
  const summaryRef = useRef<HTMLDivElement>(null)
  const librarySearchRef = useRef<HTMLInputElement>(null)
  const save = useSaveTemplate()
  const del = useDeleteTemplate()
  const guard = useUnsavedGuard(dirty)

  useEffect(() => writePreviewPref(previewOn), [previewOn])
  useEffect(() => {
    if (!periodTouched) setPreviewPeriod(draft.defaultPeriod ?? 'last_24h')
  }, [draft.defaultPeriod, periodTouched])

  const libraryInline = wide && !previewOn
  const inspectorInline = wide && (!previewOn || ultra)
  const outlineDense = (wide && previewOn) || !roomy

  const n = draft.entries.length
  const full = n >= MAX_SECTIONS
  const selectedIndex = draft.entries.findIndex((e) => e.uid === selectedUid)
  const selectedEntry = selectedIndex >= 0 ? draft.entries[selectedIndex] : null
  const selectedDef = selectedEntry ? sections[selectedEntry.id] : undefined
  const usage = useMemo(() => usageCounts(draft.entries), [draft.entries])
  const blockingIssues = Object.values(validation.entries).filter((p) => p.unknown || Object.keys(p.options).length).length
  const categoryTitle = catalog.categories.find((c) => c.id === draft.category)?.title ?? null
  const builtinTemplates = useMemo(
    () => catalog.types.filter((t) => t.engine === 'sections' && (t.entries?.length || t.sections?.length)),
    [catalog.types],
  )
  const basedOn = draft.sourceKey ? catalog.types.find((t) => t.key === draft.sourceKey) ?? builtin ?? null : null

  /* ── Draft updates ────────────────────────────────────────────────── */
  const patch = (p: Partial<Draft>) => setDraft((d) => ({ ...d, ...p }))
  const setEntries = (entries: DesignerEntry[]) => setDraft((d) => ({ ...d, entries }))
  const updateEntry = (uid: string, p: Partial<Pick<DesignerEntry, 'title' | 'options'>>) =>
    setDraft((d) => ({ ...d, entries: d.entries.map((e) => (e.uid === uid ? { ...e, ...p } : e)) }))

  const select = (uid: string | null, opts?: { open?: boolean }) => {
    setSelectedUid(uid)
    if (opts?.open && uid && !inspectorInline) setInspectorOpen(true)
  }

  const addSection = (def: SectionDef, at?: number) => {
    if (!def.available) {
      // The server refuses sections the author may not view (shared templates
      // run under other people's permissions).
      announce(`${def.title} needs a permission your role does not have, so it cannot be added.`)
      return
    }
    if (draft.entries.length >= MAX_SECTIONS) {
      announce(`The template already has the maximum of ${MAX_SECTIONS} sections.`)
      return
    }
    const entry: DesignerEntry = { uid: newUid(), id: def.id, title: '', options: {} }
    const pos = at ?? addPosition(draft.entries, selectedUid)
    setDraft((d) => ({ ...d, entries: insertEntry(d.entries, entry, pos) }))
    setSelectedUid(entry.uid)
    announce(`Added ${def.title}. ${positionText(def.title, pos, draft.entries.length + 1)}`)
  }

  const loadBuiltin = (t: CatalogTemplate) => {
    const entries = entriesFrom(t.entries, t.sections).slice(0, MAX_SECTIONS)
    setDraft((d) => ({
      ...d,
      entries,
      name: d.name.trim() ? d.name : `${t.title} (custom)`.slice(0, NAME_MAX),
      description: d.description.trim() ? d.description : t.description,
      category: d.category ?? (t.category || null),
      defaultPeriod: t.default_period ?? d.defaultPeriod,
      sourceKey: d.sourceKey ?? t.key,
    }))
    setSelectedUid(entries[0]?.uid ?? null)
    announce(`Added ${entries.length} sections from ${t.title}.`)
  }

  const togglePreview = () => {
    // The preview needs the height: fold the details panel away when it opens.
    if (!previewOn) setDetailsOpen(false)
    setPreviewOn(!previewOn)
  }

  const openLibrary = () => {
    if (libraryInline) librarySearchRef.current?.focus()
    else setLibraryOpen(true)
  }

  /* ── Validation summary ───────────────────────────────────────────── */
  const focusLater = (id: string, fallback?: () => void) => {
    window.setTimeout(() => {
      const el = document.getElementById(id)
      if (el) {
        el.focus()
        el.scrollIntoView({ block: 'nearest' })
      } else {
        fallback?.()
      }
    }, 60)
  }

  const goToIssue = (issue: Issue) => {
    const t = issue.target
    if (t.kind === 'field') {
      if (t.field === 'name') focusLater(FIELD_IDS.name)
      else if (t.field === 'sections') {
        if (!wide) setPreviewOn(false)
        focusLater(FIELD_IDS.sections)
      } else {
        setDetailsOpen(true)
        focusLater(FIELD_IDS[t.field])
      }
      return
    }
    setSelectedUid(t.uid)
    if (!wide) setPreviewOn(false)
    if (!inspectorInline) setInspectorOpen(true)
    focusLater(t.option ? optionFieldId(t.uid, t.option) : titleFieldId(t.uid), () => outlineRef.current?.focusEntry(t.uid))
  }

  /* ── Save / save as / discard / delete ────────────────────────────── */
  const doSave = async (asNew?: { name: string; visibility: Draft['visibility'] }): Promise<SaveOutcome> => {
    setAttempted(true)
    setServerError(null)
    const target: Draft = asNew ? { ...draft, name: asNew.name, visibility: asNew.visibility } : draft
    const v = asNew ? validateDraft(target, sections, known) : validation
    if (!v.valid) {
      setSummaryOpen(true)
      window.setTimeout(() => summaryRef.current?.focus(), 30)
      return 'invalid'
    }
    const body = toTemplateBody(target, sections)
    const id = editing && !asNew ? template?.id ?? null : null
    try {
      const res = await save.mutateAsync({ id, body })
      const savedId = id ?? res.id
      if (!savedId) throw new Error('The appliance did not return the new template id')
      toast.success(id ? 'Template saved' : 'Template created', `“${body.name}” is ready to run, export or schedule.`)
      guard.release(`/reports/view/custom?custom_id=${encodeURIComponent(savedId)}`)
      return 'ok'
    } catch (e) {
      const msg = apiErrorMessage(e, 'The template could not be saved')
      setServerError(msg)
      setSummaryOpen(true)
      toast.error('Could not save the template', msg)
      return 'error'
    }
  }

  const saveRef = useRef(doSave)
  saveRef.current = doSave
  const primaryRef = useRef<() => void>(() => undefined)
  primaryRef.current = () => {
    if (save.isPending) return
    if (canEdit) {
      if (editing && !dirty) announce('No changes to save.')
      else void saveRef.current()
    }
    else openSaveAs()
  }
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 's') {
        e.preventDefault()
        primaryRef.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const openSaveAs = () => {
    setServerError(null)
    setSaveAsOpen(true)
  }

  const discard = () => {
    setDraft(initial)
    setSelectedUid(initial.entries[0]?.uid ?? null)
    setAttempted(false)
    setNameTouched(false)
    setSummaryOpen(false)
    setServerError(null)
    setDiscardOpen(false)
    announce('Changes discarded.')
  }

  const doDelete = async (force: boolean) => {
    if (!template) return
    try {
      await del.mutateAsync({ id: template.id, force })
      toast.success('Template deleted', `“${template.name}” was deleted.`)
      setDeleteStep(null)
      guard.release('/reports')
    } catch (e) {
      if (!force && describeFailure(e).kind === 'conflict') {
        setDeleteStep('force')
        return
      }
      setDeleteStep(null)
      toast.error('Could not delete the template', apiErrorMessage(e))
    }
  }

  /* ── Derived UI state ─────────────────────────────────────────────── */
  const nameError = (attempted || nameTouched) ? validation.fields.name : undefined
  const issueCount = validation.issues.length
  const showIssueChip = issueCount > 0 && (attempted || dirty)
  const summaryVisible = summaryOpen && (issueCount > 0 || !!serverError)
  const status = editing
    ? dirty ? { tone: 'bg-warning', text: 'Unsaved changes' } : { tone: 'bg-success', text: 'All changes saved' }
    : { tone: 'bg-info', text: dirty ? 'New template · not saved yet' : 'New template · not saved' }

  const gridCols = !wide ? 'grid-cols-1'
    : !previewOn ? 'grid-cols-[17rem_minmax(0,1fr)_20rem] 2xl:grid-cols-[19rem_minmax(0,1fr)_23rem]'
      : ultra ? 'grid-cols-[20rem_minmax(0,1fr)_22rem]'
        : 'grid-cols-[minmax(17rem,21rem)_minmax(0,1fr)]'

  const inspector = (
    <Inspector
      key={selectedEntry?.uid ?? 'none'}
      entry={selectedEntry}
      def={selectedDef}
      index={selectedIndex}
      total={n}
      problems={selectedEntry ? validation.entries[selectedEntry.uid] : undefined}
      full={full}
      onChange={(p) => selectedEntry && updateEntry(selectedEntry.uid, p)}
      onDuplicate={() => selectedEntry && outlineRef.current?.duplicate(selectedEntry.uid, { focus: inspectorInline })}
      onRemove={() => {
        if (!selectedEntry) return
        if (!inspectorInline) setInspectorOpen(false)
        outlineRef.current?.remove(selectedEntry.uid, { focus: inspectorInline })
      }}
    />
  )

  const library = (
    <SectionLibrary
      sections={catalog.sections}
      categories={catalog.categories}
      usage={usage}
      full={full}
      onAdd={(d) => addSection(d)}
      draggable={libraryInline}
      searchRef={librarySearchRef}
    />
  )

  const emptyOutline = (
    <div className="flex h-full min-h-[16rem] flex-col items-center justify-center gap-3 px-6 py-10 text-center">
      <span className="flex h-11 w-11 items-center justify-center rounded-full bg-primary/10 text-primary">
        <LayoutTemplate className="h-5 w-5" aria-hidden />
      </span>
      <div className="space-y-1">
        <p className="text-sm font-semibold text-text">Start building your report</p>
        <p className="mx-auto max-w-sm text-xs leading-relaxed text-muted">
          Add sections from the library{libraryInline ? ' on the left (click, or drag them here)' : ''}, or start from the
          sections of a built-in template. You can reorder, rename and tune each one.
        </p>
      </div>
      <div className="flex flex-wrap justify-center gap-2">
        <Button type="button" size="sm" onClick={openLibrary}>
          <Plus aria-hidden /> Add sections
        </Button>
        {builtinTemplates.length > 0 && (
          <StartFromMenu templates={builtinTemplates} categories={catalog.categories} onPick={loadBuiltin} />
        )}
      </div>
    </div>
  )

  return (
    <div className="zp-report-designer mx-auto flex w-full max-w-[1920px] flex-col gap-3 xl:h-[calc(100dvh-5.25rem)] xl:min-h-[40rem]">
      {region}

      {/* ── Header / action bar ─────────────────────────────────────── */}
      <header className="sticky top-11 z-20 rounded-xl border border-border bg-surface/95 px-4 py-3 shadow-card backdrop-blur dark:shadow-card-dark xl:static xl:bg-surface xl:backdrop-blur-none">
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2.5">
          <div className="min-w-0 flex-1 basis-[18rem]">
            <nav aria-label="Breadcrumb" className="flex items-center gap-1 text-xs text-muted">
              <Link to="/reports" className="inline-flex items-center gap-1 rounded font-medium transition-colors hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50">
                <ArrowLeft className="h-3 w-3" aria-hidden /> Report library
              </Link>
              <ChevronRight className="h-3 w-3" aria-hidden />
              <span aria-current="page" className="truncate">{MODE_LABEL[mode]}</span>
            </nav>
            <label htmlFor={FIELD_IDS.name} className="sr-only">Template name (required)</label>
            <input
              id={FIELD_IDS.name}
              value={draft.name}
              maxLength={NAME_MAX}
              onChange={(e) => patch({ name: e.target.value })}
              onBlur={() => setNameTouched(true)}
              placeholder="Name this template"
              aria-required
              aria-invalid={!!nameError || undefined}
              aria-describedby={idList(nameError && `${FIELD_IDS.name}-err`)}
              className={cn(
                '-ml-1.5 mt-1 w-full rounded-md border bg-transparent px-1.5 py-0.5 text-xl font-semibold tracking-tight text-text transition-colors placeholder:font-normal placeholder:text-muted',
                'hover:border-border focus:border-primary focus:outline-none focus:ring-2 focus:ring-primary/30',
                nameError ? 'border-danger/60' : 'border-transparent',
              )}
            />
            {nameError && <p id={`${FIELD_IDS.name}-err`} className="mt-0.5 text-[11px] font-medium text-danger-fg">{nameError}</p>}
            <div className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-muted">
              <span className="inline-flex items-center gap-1.5">
                <span aria-hidden className={cn('h-1.5 w-1.5 rounded-full', status.tone)} />
                {status.text}
              </span>
              {draft.name.length > NAME_MAX - 20 && <span className="tabular-nums">{draft.name.length}/{NAME_MAX}</span>}
              {basedOn && (
                <span className="inline-flex items-center gap-1">
                  <Sparkles className="h-3 w-3" aria-hidden />
                  Based on <Link to={`/reports/view/${encodeURIComponent(basedOn.key)}`} className="font-medium text-text2 hover:text-primary hover:underline">{basedOn.title}</Link>
                </span>
              )}
              {template && editing && <span>Owner: <span className="text-text2">{template.owner_name || 'unknown'}</span></span>}
              {template && mode === 'clone' && <span>Copied from <span className="text-text2">{template.name}</span></span>}
              {template && editing && template.schedules > 0 && (
                <span className="inline-flex items-center gap-1">
                  <CalendarClock className="h-3 w-3" aria-hidden />
                  {template.schedules} schedule{template.schedules === 1 ? '' : 's'} deliver this template
                </span>
              )}
            </div>
          </div>

          <div className="flex flex-wrap items-center gap-2">
            {showIssueChip && (
              <button
                type="button"
                onClick={() => setSummaryOpen((o) => !o)}
                aria-expanded={summaryVisible}
                aria-controls="dsn-issues"
                className="inline-flex h-8 items-center gap-1.5 rounded-full border border-danger/40 bg-danger/10 px-2.5 text-xs font-medium text-danger-fg transition-colors hover:bg-danger/15 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger/40"
              >
                <AlertCircle className="h-3.5 w-3.5" aria-hidden />
                <span className="tabular-nums">{issueCount}</span> {issueCount === 1 ? 'issue' : 'issues'}
              </button>
            )}
            <Button
              type="button"
              size="sm"
              variant="outline"
              aria-pressed={previewOn}
              onClick={togglePreview}
              title={previewOn ? 'Hide the live preview' : 'Show a live preview with real data'}
              className={cn(previewOn && 'border-primary/50 bg-primary/10 text-primary hover:bg-primary/15 hover:text-primary')}
            >
              <Eye aria-hidden /> Preview
            </Button>
            <Button type="button" size="sm" variant="ghost" disabled={!dirty || save.isPending} onClick={() => setDiscardOpen(true)}>
              <RotateCcw aria-hidden /> Discard
            </Button>
            {editing && canEdit && (
              <Button type="button" size="sm" variant="outline" disabled={save.isPending} onClick={openSaveAs}>
                <CopyPlus aria-hidden /> Save as new
              </Button>
            )}
            {canEdit ? (
              <Button
                type="button"
                size="sm"
                disabled={save.isPending || (editing && !dirty)}
                onClick={() => void doSave()}
                title="Save (Ctrl+S)"
              >
                {save.isPending ? <Loader2 className="animate-spin" aria-hidden /> : <Save aria-hidden />}
                {editing ? 'Save' : 'Create template'}
              </Button>
            ) : (
              <Button type="button" size="sm" disabled={save.isPending} onClick={openSaveAs}>
                <CopyPlus aria-hidden /> Save as new
              </Button>
            )}
            {editing && template?.can_delete && (
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button type="button" size="icon" variant="ghost" className="h-8 w-8" aria-label="More template actions">
                    <MoreHorizontal aria-hidden />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <DropdownMenuItem destructive onSelect={() => setDeleteStep('confirm')}>
                    <Trash2 aria-hidden /> Delete template
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>
            )}
          </div>
        </div>
      </header>

      {/* ── Notices ─────────────────────────────────────────────────── */}
      {editing && !canEdit && (
        <div role="note" className="flex items-start gap-2.5 rounded-xl border border-info/35 bg-info/5 px-4 py-2.5 text-xs text-text2">
          <Info className="mt-px h-4 w-4 shrink-0 text-info-fg" aria-hidden />
          <p>
            <span className="font-semibold text-text">You can't change this template.</span>{' '}
            It belongs to {template?.owner_name || 'another user'}
            {template?.visibility === 'private' ? ' and is private' : ''}. Make your changes here and use{' '}
            <span className="font-medium text-text">Save as new</span> to keep them as a template you own.
          </p>
        </div>
      )}

      {summaryVisible && (
        <div
          id="dsn-issues"
          ref={summaryRef}
          tabIndex={-1}
          role="alert"
          className="rounded-xl border border-danger/40 bg-danger/5 px-4 py-3 focus:outline-none focus-visible:ring-2 focus-visible:ring-danger/40"
        >
          <div className="flex items-start gap-2.5">
            <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-danger-fg" aria-hidden />
            <div className="min-w-0 flex-1">
              <p className="text-sm font-semibold text-text">
                {serverError ? 'The template was not saved' : `Fix ${issueCount} ${issueCount === 1 ? 'issue' : 'issues'} to save`}
              </p>
              {serverError && <p className="mt-0.5 text-xs text-text2">{serverError}</p>}
              {issueCount > 0 && (
                <ul className="mt-1.5 space-y-1 text-xs">
                  {validation.issues.map((issue) => (
                    <li key={issue.key} className="flex items-start gap-1.5">
                      <span aria-hidden className="mt-1.5 h-1 w-1 shrink-0 rounded-full bg-danger" />
                      <button
                        type="button"
                        onClick={() => goToIssue(issue)}
                        className="rounded text-left font-medium text-danger-fg underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-danger/40"
                      >
                        {issue.message}
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <button
              type="button"
              onClick={() => setSummaryOpen(false)}
              aria-label="Hide the list of issues"
              className="rounded p-1 text-muted hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
            >
              <X className="h-3.5 w-3.5" aria-hidden />
            </button>
          </div>
        </div>
      )}

      <TemplateSettings
        draft={draft}
        onChange={patch}
        categories={catalog.categories}
        errors={validation.fields}
        showErrors={attempted}
        canChangeVisibility={canChangeVisibility}
        ownerName={template?.owner_name}
        open={detailsOpen}
        onOpenChange={setDetailsOpen}
      />

      {/* ── Workspace ───────────────────────────────────────────────── */}
      <div className={cn('grid min-h-0 flex-1 gap-3 xl:min-h-[26rem]', gridCols)}>
        {libraryInline && (
          <aside aria-labelledby="dsn-library-heading" className={pane}>
            <div className="flex items-center gap-2 border-b border-border px-3 py-2">
              <Library className="h-4 w-4 text-primary" aria-hidden />
              <h2 id="dsn-library-heading" className="text-sm font-semibold text-text">Section library</h2>
            </div>
            {library}
          </aside>
        )}

        <div className={cn(pane, !wide && previewOn && 'hidden', !wide && 'min-h-[24rem]')}>
          <Outline
            ref={outlineRef}
            entries={draft.entries}
            sections={sections}
            problems={validation.entries}
            selectedUid={selectedUid}
            onSelect={select}
            onEntriesChange={setEntries}
            onDropSection={(sid, at) => {
              const def = sections[sid]
              if (def) addSection(def, at)
            }}
            announce={announce}
            dense={outlineDense}
            headingId={FIELD_IDS.sections}
            empty={emptyOutline}
            toolbar={!libraryInline && n > 0 ? (
              <Button type="button" size="sm" variant="outline" className="h-7 px-2.5 text-xs" onClick={openLibrary} disabled={full}>
                <Plus aria-hidden /> Add sections
              </Button>
            ) : n > 0 && !outlineDense ? (
              <span className="hidden text-[11px] text-muted 2xl:inline">Drag the grip or press Space on it to reorder</span>
            ) : null}
          />
        </div>

        {previewOn && (
          <PreviewPane
            draft={draft}
            sections={sections}
            sectionMeta={sectionMeta}
            branding={branding}
            categoryTitle={categoryTitle}
            blockingIssues={blockingIssues}
            period={previewPeriod}
            onPeriodChange={(p) => { setPreviewPeriod(p); setPeriodTouched(true) }}
            selectedIndex={wide ? selectedIndex : -1}
            className={cn(!wide && 'h-[calc(100dvh-10rem)] min-h-[28rem]')}
          />
        )}

        {inspectorInline && (
          <aside aria-labelledby="dsn-inspector-heading" className={pane}>
            <div className="flex items-center gap-2 border-b border-border px-3 py-2">
              <h2 id="dsn-inspector-heading" className="text-sm font-semibold text-text">Section settings</h2>
            </div>
            {inspector}
          </aside>
        )}
      </div>

      {/* ── Sheets (smaller screens, or while previewing) ───────────── */}
      {!libraryInline && (
        <Sheet
          open={libraryOpen}
          onOpenChange={setLibraryOpen}
          side="left"
          title="Section library"
          description="Click a section to add it after the selected one."
          onOpenAutoFocus={(e) => { e.preventDefault(); librarySearchRef.current?.focus() }}
          footer={(
            <Button type="button" className="w-full" onClick={() => setLibraryOpen(false)}>
              Done · <span className="tabular-nums">{n}</span> section{n === 1 ? '' : 's'}
            </Button>
          )}
        >
          {library}
        </Sheet>
      )}
      {!inspectorInline && (
        <Sheet
          open={inspectorOpen && !!selectedEntry}
          onOpenChange={setInspectorOpen}
          side="right"
          title="Section settings"
          description={selectedDef?.title ?? selectedEntry?.id}
          onCloseAutoFocus={(e) => { e.preventDefault(); outlineRef.current?.focusEntry(selectedUid) }}
        >
          {inspector}
        </Sheet>
      )}

      {/* ── Dialogs ─────────────────────────────────────────────────── */}
      <SaveAsDialog
        open={saveAsOpen}
        onOpenChange={setSaveAsOpen}
        defaultName={`${draft.name.trim() || template?.name || 'Untitled'} (copy)`.slice(0, NAME_MAX)}
        busy={save.isPending}
        error={saveAsOpen ? serverError : null}
        onSave={async (v) => {
          const outcome = await doSave(v)
          if (outcome !== 'error') setSaveAsOpen(false)
        }}
      />

      <ConfirmDialog
        open={discardOpen}
        onOpenChange={setDiscardOpen}
        title="Discard your changes?"
        description={editing
          ? 'The template goes back to how it was when you opened it.'
          : 'The designer goes back to how it was when you opened it.'}
        confirmText="Discard changes"
        destructive
        onConfirm={discard}
      />

      <ConfirmDialog
        open={!!guard.pending}
        onOpenChange={(o) => { if (!o) guard.cancel() }}
        title="Leave without saving?"
        description={<>Your changes to <span className="font-semibold text-text">{draft.name.trim() || 'this template'}</span> will be lost.</>}
        confirmText="Leave without saving"
        cancelText="Keep editing"
        destructive
        onConfirm={guard.confirm}
      />

      {template && (
        <ConfirmDialog
          open={deleteStep !== null}
          onOpenChange={(o) => { if (!o) setDeleteStep(null) }}
          title={deleteStep === 'force' ? 'Delete its schedules too?' : 'Delete this template?'}
          description={deleteStep === 'force' ? (
            <>Schedules still deliver <span className="font-semibold text-text">{template.name}</span>. Deleting the template also deletes those schedules.</>
          ) : template.schedules > 0 ? (
            <>
              <span className="font-semibold text-text">{template.name}</span> is delivered by {template.schedules} schedule
              {template.schedules === 1 ? '' : 's'}. Deleting the template also deletes {template.schedules === 1 ? 'it' : 'them'}.
              This cannot be undone.
            </>
          ) : (
            <>Delete <span className="font-semibold text-text">{template.name}</span>? This cannot be undone.</>
          )}
          confirmText={deleteStep === 'force' || template.schedules > 0 ? 'Delete template and schedules' : 'Delete template'}
          destructive
          loading={del.isPending}
          onConfirm={() => void doDelete(deleteStep === 'force' || template.schedules > 0)}
        />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Pieces                                                             */
/* ------------------------------------------------------------------ */

function StartFromMenu({ templates, categories, onPick }: {
  templates: CatalogTemplate[]
  categories: ReportCatalog['categories']
  onPick: (t: CatalogTemplate) => void
}) {
  const groups = useMemo(() => {
    const order = new Map(categories.map((c, i) => [c.id, i]))
    const byCat = new Map<string, CatalogTemplate[]>()
    for (const t of templates) {
      const list = byCat.get(t.category) ?? []
      list.push(t)
      byCat.set(t.category, list)
    }
    return [...byCat.entries()]
      .sort((a, b) => (order.get(a[0]) ?? 99) - (order.get(b[0]) ?? 99))
      .map(([cat, list]) => ({
        title: categories.find((c) => c.id === cat)?.title ?? list[0]?.category_title ?? cat,
        list: list.sort((a, b) => a.title.localeCompare(b.title)),
      }))
  }, [templates, categories])
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button type="button" size="sm" variant="outline">
          <Sparkles aria-hidden /> Start from a built-in
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="center" className="max-h-[22rem] w-72 overflow-y-auto">
        {groups.map((g, gi) => (
          <div key={g.title}>
            {gi > 0 && <DropdownMenuSeparator />}
            <DropdownMenuLabel>{g.title}</DropdownMenuLabel>
            {g.list.map((t) => {
              const count = t.entries?.length ?? t.sections?.length ?? 0
              return (
                <DropdownMenuItem key={t.key} onSelect={() => onPick(t)} className="items-start">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate">{t.title}</span>
                    <span className="block text-[11px] text-muted">{count} section{count === 1 ? '' : 's'}</span>
                  </span>
                </DropdownMenuItem>
              )
            })}
          </div>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function SaveAsDialog({ open, onOpenChange, defaultName, busy, error, onSave }: {
  open: boolean
  onOpenChange: (open: boolean) => void
  defaultName: string
  busy: boolean
  error: string | null
  onSave: (v: { name: string; visibility: Draft['visibility'] }) => Promise<void>
}) {
  const [name, setName] = useState(defaultName)
  const [visibility, setVisibility] = useState<Draft['visibility']>('private')
  const [touched, setTouched] = useState(false)
  useEffect(() => {
    if (!open) return
    setName(defaultName)
    setVisibility('private')
    setTouched(false)
    // Reset only when the dialog opens.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open])
  const nameError = !name.trim() ? 'Give the new template a name' : null
  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setTouched(true)
    if (nameError) return
    await onSave({ name: name.trim(), visibility })
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-md">
        <form onSubmit={submit} className="space-y-4">
          <DialogHeader>
            <DialogTitle>Save as a new template</DialogTitle>
            <DialogDescription>Creates a template that you own, with your changes. The original stays as it was.</DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <label htmlFor="dsn-saveas-name" className="text-xs font-medium text-text">Name</label>
            <Input
              id="dsn-saveas-name"
              value={name}
              maxLength={NAME_MAX}
              autoFocus
              onChange={(e) => setName(e.target.value)}
              aria-invalid={(touched && !!nameError) || undefined}
              aria-describedby={idList(touched && nameError && 'dsn-saveas-err')}
            />
            {touched && nameError && <p id="dsn-saveas-err" className="text-[11px] font-medium text-danger-fg">{nameError}</p>}
          </div>
          <div className="space-y-1.5">
            <span id="dsn-saveas-vis" className="block text-xs font-medium text-text">Who can see it</span>
            <SegmentedRadio
              labelledBy="dsn-saveas-vis"
              value={visibility}
              onChange={setVisibility}
              options={[{ value: 'private', label: 'Private' }, { value: 'shared', label: 'Shared' }]}
            />
          </div>
          {error && (
            <p role="alert" className="rounded-md border border-danger/40 bg-danger/5 px-3 py-2 text-xs text-text2">{error}</p>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => onOpenChange(false)} disabled={busy}>Cancel</Button>
            <Button type="submit" disabled={busy}>
              {busy ? <Loader2 className="animate-spin" aria-hidden /> : <CopyPlus aria-hidden />} Save as new
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}
