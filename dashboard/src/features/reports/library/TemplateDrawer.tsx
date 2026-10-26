/**
 * Template details: a right-hand sheet with the purpose, the numbered section
 * outline with option summaries, defaults, audience and the data permissions
 * the template reads, plus Open / Schedule / Customize.
 */
import { useRef, type ReactNode } from 'react'
import * as DialogPrimitive from '@radix-ui/react-dialog'
import { Check, Info, Lock, X } from 'lucide-react'
import { cn } from '@/lib/utils'
import { formatDateTime, type Branding } from '@/lib/branding'
import { useCan } from '@/stores/auth'
import { QuickActions, TemplateMenu, useLibraryActions } from './actions'
import {
  entryTitle, FORMAT_LABEL, FREQUENCY_LABEL, optionSummary, periodLabel, permissionLabel, SCOPE_LABEL,
  type LibraryItem, type SectionMap,
} from './model'
import { CategoryIcon, FavoriteButton, OriginBadges } from './parts'

export function TemplateDrawer({ item, open, onOpenChange, sections, branding }: {
  item: LibraryItem | null
  open: boolean
  onOpenChange: (open: boolean) => void
  sections: SectionMap
  branding: Branding
}) {
  return (
    <DialogPrimitive.Root open={open && !!item} onOpenChange={onOpenChange}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-black/50 data-[state=open]:animate-fade-in" />
        {item && <DrawerContent item={item} sections={sections} branding={branding} />}
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  )
}

/**
 * The sheet itself. Mounted only while open, so it can remember what had
 * focus (the card or row that opened it) and return there on close — Radix
 * only does that for a <Dialog.Trigger>, and the drawer is opened from code.
 */
function DrawerContent({ item, sections, branding }: { item: LibraryItem; sections: SectionMap; branding: Branding }) {
  const opener = useRef<HTMLElement | null>(
    typeof document !== 'undefined' && document.activeElement instanceof HTMLElement && document.activeElement !== document.body
      ? document.activeElement
      : null,
  )
  return (
    <DialogPrimitive.Content
      className={cn(
        'fixed inset-y-0 right-0 z-50 flex w-full max-w-[460px] flex-col border-l border-border bg-surface shadow-xl',
        'focus:outline-none data-[state=open]:animate-fade-in',
      )}
      aria-describedby="report-template-purpose"
      onCloseAutoFocus={(e) => {
        const el = opener.current
        if (el && el.isConnected) {
          e.preventDefault()
          el.focus({ preventScroll: true })
        }
      }}
    >
      <DrawerBody item={item} sections={sections} branding={branding} />
    </DialogPrimitive.Content>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium uppercase tracking-wide text-muted">{label}</dt>
      <dd className="mt-0.5 text-sm text-text">{children}</dd>
    </div>
  )
}

function DrawerBody({ item, sections, branding }: { item: LibraryItem; sections: SectionMap; branding: Branding }) {
  const a = useLibraryActions()
  const can = useCan()
  const hidden = item.entries.filter((e) => e.available === false || sections.get(e.id)?.available === false).length
  return (
    <>
      <header className="flex items-start gap-3 border-b border-border px-5 py-4">
        <CategoryIcon icon={item.icon} category={item.category} size="lg" />
        <div className="min-w-0 flex-1">
          <p className="text-xs font-medium uppercase tracking-wide text-muted">{item.categoryTitle}</p>
          <DialogPrimitive.Title className="mt-0.5 break-words text-base font-semibold leading-snug text-text">
            {item.title}
          </DialogPrimitive.Title>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <OriginBadges item={item} />
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-0.5">
          <FavoriteButton item={item} pressed={a.isFavorite(item)} onToggle={() => a.toggleFavorite(item)} />
          <DialogPrimitive.Close
            className="inline-flex h-7 w-7 items-center justify-center rounded-md text-muted hover:bg-surface2 hover:text-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/50"
            aria-label="Close details"
          >
            <X className="h-4 w-4" aria-hidden />
          </DialogPrimitive.Close>
        </div>
      </header>

      <div className="min-h-0 flex-1 space-y-5 overflow-y-auto overscroll-contain px-5 py-4">
        <section aria-labelledby="rt-purpose-h">
          <h3 id="rt-purpose-h" className="sr-only">Purpose</h3>
          <DialogPrimitive.Description id="report-template-purpose" className="text-sm leading-5 text-text2">
            {item.description || 'No description.'}
          </DialogPrimitive.Description>
        </section>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-3 rounded-lg border border-border bg-surface2/40 p-3">
          <Fact label="Default period">{periodLabel(item.defaultPeriod) || 'Last 24 hours'}</Fact>
          <Fact label="Suggested delivery">{item.frequency ? FREQUENCY_LABEL[item.frequency] : '—'}</Fact>
          <Fact label="Audience">{item.audience.length ? item.audience.join(', ') : '—'}</Fact>
          <Fact label="Formats">{item.formats.map((f) => FORMAT_LABEL[f] || f.toUpperCase()).join(', ') || '—'}</Fact>
          {item.filterable.length > 0 && (
            <Fact label="Can be scoped to">{item.filterable.map((f) => SCOPE_LABEL[f] || f).join(', ')}</Fact>
          )}
          {item.origin === 'custom' && (
            <>
              <Fact label="Schedules">
                <span className="tabular-nums">{item.schedules}</span>
              </Fact>
              {item.sourceTitle && <Fact label="Based on">{item.sourceTitle}</Fact>}
              {item.updatedAt && (
                <Fact label="Last updated">
                  <span className="tabular-nums">{formatDateTime(item.updatedAt, branding, { zone: true })}</span>
                </Fact>
              )}
            </>
          )}
        </dl>

        {item.requires.length > 0 && (
          <section aria-labelledby="rt-data-h">
            <h3 id="rt-data-h" className="text-xs font-semibold text-text">Data it reads</h3>
            <p className="mt-0.5 text-xs text-muted">Each section needs the matching permission; sections your role lacks are left out.</p>
            <ul className="mt-2 flex flex-wrap gap-1.5" role="list">
              {item.requires.map((p) => {
                const ok = can(p)
                return (
                  <li
                    key={p}
                    className={cn(
                      'inline-flex items-center gap-1 rounded border px-1.5 py-px text-xs font-medium',
                      ok ? 'border-success/30 bg-success/10 text-success-fg' : 'border-border bg-surface2 text-muted',
                    )}
                    title={p}
                  >
                    {ok ? <Check className="h-3 w-3" aria-hidden /> : <Lock className="h-3 w-3" aria-hidden />}
                    {permissionLabel(p)}
                    <span className="sr-only">{ok ? '(your role has access)' : '(not in your role)'}</span>
                  </li>
                )
              })}
            </ul>
          </section>
        )}

        <section aria-labelledby="rt-sections-h">
          <div className="flex items-baseline justify-between gap-2">
            <h3 id="rt-sections-h" className="text-xs font-semibold text-text">
              Sections <span className="font-normal tabular-nums text-muted">({item.entries.length})</span>
            </h3>
            {hidden > 0 && <span className="text-xs text-warning-fg">{hidden} hidden for your role</span>}
          </div>
          <ol className="mt-2 space-y-1.5">
            {item.entries.map((entry, i) => {
              const def = sections.get(entry.id)
              const unavailable = entry.available === false || def?.available === false
              const summary = optionSummary(entry, def)
              return (
                <li
                  key={`${entry.id}-${i}`}
                  className={cn('flex gap-2.5 rounded-md border border-border/70 px-2.5 py-2', unavailable ? 'bg-surface2/40' : 'bg-surface')}
                >
                  <span
                    className={cn(
                      'mt-px flex h-5 min-w-5 shrink-0 items-center justify-center rounded px-1 text-xs font-semibold tabular-nums',
                      unavailable ? 'bg-surface3 text-muted' : 'bg-primary/10 text-primary',
                    )}
                    aria-hidden
                  >
                    {i + 1}
                  </span>
                  <div className="min-w-0 flex-1">
                    <p className={cn('text-sm font-medium leading-5', unavailable ? 'text-muted' : 'text-text')}>
                      <span className="sr-only">{i + 1}. </span>
                      {entryTitle(entry, sections)}
                    </p>
                    {summary ? (
                      <p className="truncate text-xs text-muted" title={summary}>{summary}</p>
                    ) : def?.description ? (
                      <p className="line-clamp-1 text-xs text-muted" title={def.description}>{def.description}</p>
                    ) : null}
                    {unavailable && (
                      <p className="mt-0.5 inline-flex items-center gap-1 text-xs text-warning-fg">
                        <Lock className="h-3 w-3" aria-hidden />
                        Hidden for your role{def?.requires?.length ? ` — needs ${def.requires.map(permissionLabel).join(', ')}` : ''}
                      </p>
                    )}
                  </div>
                </li>
              )
            })}
          </ol>
        </section>

        {a.scheduleNote && (
          <p className="flex items-start gap-1.5 rounded-md border border-border bg-surface2/50 px-3 py-2 text-xs text-text2">
            <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-info-fg" aria-hidden />
            {a.scheduleNote}
          </p>
        )}
      </div>

      <footer className="flex items-center gap-2 border-t border-border bg-surface px-5 py-3">
        <QuickActions item={item} compact={false} className="min-w-0 flex-1" />
        <TemplateMenu item={item} withDetails={false} className="h-8 w-8" />
      </footer>
    </>
  )
}
