import { Suspense, lazy } from 'react'
import { Lock } from 'lucide-react'
import { useInventoryAccess } from '@/components/inventory/InventoryAccess'
import { SettingsEditorSkeleton } from '@/components/inventory/SettingsEditorSkeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/Tabs'
import { useCan } from '@/stores/auth'

// The form is only for people who manage the module (and a read-only view for the rest), so it loads on demand.
const SettingsEditor = lazy(() => import('@/components/inventory/SettingsEditor'))

/**
 * /inventory/settings (doc 03 section 3.9). Phase 1 has one tab, Data & retention; the catalog rules, the
 * lifecycle overrides and the saved views arrive with later phases. The row, the tab and the route need only
 * `inventory.view` (the freshness targets drive every badge, so anyone who sees the badges may read them): a role
 * that also holds `inventory.manage` gets the form, every other role the same values read-only with a note naming
 * the permission. Changing them is refused by the server without `inventory.manage` in any case.
 */
export function InventorySettingsPage() {
  const can = useCan()
  const access = useInventoryAccess()
  const canManage = can('inventory.manage')

  const editor = (
    <Suspense fallback={<SettingsEditorSkeleton />}>
      <SettingsEditor canManage={canManage} enabled={access === 'allowed'} />
    </Suspense>
  )

  return (
    <section aria-label="Inventory settings" className="space-y-4" data-page="settings">
      {canManage ? (
        <Tabs value="retention">
          <TabsList aria-label="Settings sections">
            <TabsTrigger value="retention">Data &amp; retention</TabsTrigger>
          </TabsList>
          <TabsContent value="retention" className="mt-4">
            {/* The editor's sections are h3: this keeps the outline h1 > h2 > h3 (UI-22). */}
            <h2 className="sr-only">Data &amp; retention</h2>
            {editor}
          </TabsContent>
        </Tabs>
      ) : (
        <>
          <h2 className="text-lg font-semibold tracking-tight">Current settings</h2>
          <div role="status" data-read-only-notice="" className="flex items-start gap-3 rounded-lg border border-border bg-surface p-4 shadow-card">
            <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-full bg-warning/10 text-warning-fg"><Lock className="h-4 w-4" aria-hidden /></span>
            <div className="min-w-0 space-y-0.5">
              <p className="text-sm font-semibold text-text">You can read these settings but not change them</p>
              <p className="text-xs text-text2">
                Changing them needs <code className="rounded bg-surface2 px-1 py-0.5 font-mono text-[11px] font-semibold text-text">inventory.manage</code>. Ask an administrator to add it to your role.
              </p>
            </div>
          </div>
          {editor}
        </>
      )}
    </section>
  )
}
