import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, FolderPlus, Pencil, Plus, Trash2 } from 'lucide-react'
import { api } from '@/lib/api'
import { apiErrorMessage } from '@/lib/utils'
import { Card, CardContent } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Input } from '@/components/ui/Input'
import { Table, TBody, THead, Td, Th, Tr } from '@/components/ui/Table'
import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/Dialog'
import { FormField } from '@/components/ui/FormField'
import { ConfirmDialog } from '@/components/ui/ConfirmDialog'
import { toast } from '@/components/ui/Toast'
import type { ServiceCheckGroup } from '@/types'

export function ServiceCheckGroupsPage() {
  const qc = useQueryClient()
  const [editing, setEditing] = useState<ServiceCheckGroup | null>(null)
  const [formOpen, setFormOpen] = useState(false)
  const [deleting, setDeleting] = useState<ServiceCheckGroup | null>(null)
  const [form, setForm] = useState<{ name: string; description: string; color: string }>({
    name: '',
    description: '',
    color: '#6366f1',
  })

  const { data: groups = [], isFetching } = useQuery<ServiceCheckGroup[]>({
    queryKey: ['service-check-groups'],
    queryFn: async () => (await api.get('/service-check-groups')).data,
    refetchInterval: 30_000,
  })

  const save = useMutation({
    mutationFn: async () => {
      const body = {
        name: form.name,
        description: form.description || null,
        color: form.color || null,
      }
      if (editing) {
        return (await api.put(`/service-check-groups/${editing.id}`, body)).data
      }
      return (await api.post('/service-check-groups', body)).data
    },
    onSuccess: () => {
      toast.success(editing ? 'Group updated' : 'Group created')
      qc.invalidateQueries({ queryKey: ['service-check-groups'] })
      setFormOpen(false)
      setEditing(null)
    },
    onError: (e: any) => toast.error('Save failed', apiErrorMessage(e)),
  })

  const del = useMutation({
    mutationFn: async (id: string) => api.delete(`/service-check-groups/${id}`),
    onSuccess: () => {
      toast.success('Group deleted')
      qc.invalidateQueries({ queryKey: ['service-check-groups'] })
      qc.invalidateQueries({ queryKey: ['service-checks'] })
      setDeleting(null)
    },
    onError: (e: any) => toast.error('Delete failed', apiErrorMessage(e)),
  })

  function openCreate() {
    setEditing(null)
    setForm({ name: '', description: '', color: '#6366f1' })
    setFormOpen(true)
  }

  function openEdit(g: ServiceCheckGroup) {
    setEditing(g)
    setForm({ name: g.name, description: g.description || '', color: g.color || '#6366f1' })
    setFormOpen(true)
  }

  return (
    <div className="space-y-4">
      <div className="flex items-start justify-between">
        <div>
          <Link
            to="/services"
            className="mb-1 inline-flex items-center gap-1 text-xs text-muted hover:text-text"
          >
            <ArrowLeft className="h-3 w-3" /> Back to services
          </Link>
          <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
            <FolderPlus className="h-5 w-5 text-primary" />
            Service check groups
          </h1>
          <p className="text-xs text-muted">
            Organise related checks for bulk filtering and status rollups.
          </p>
        </div>
        <Button onClick={openCreate}>
          <Plus className="h-4 w-4" /> New group
        </Button>
      </div>

      <Card>
        <CardContent className="pt-4">
          <div className="overflow-hidden rounded-md border border-border">
            <Table>
              <THead className="bg-surface2/50">
                <Tr>
                  <Th className="w-6"></Th>
                  <Th>Name</Th>
                  <Th>Description</Th>
                  <Th className="text-right">Checks</Th>
                  <Th className="w-24 text-right">Actions</Th>
                </Tr>
              </THead>
              <TBody>
                {groups.map((g) => (
                  <Tr key={g.id}>
                    <Td>
                      <span
                        className="inline-block h-3 w-3 rounded-full"
                        style={{ background: g.color || '#6b7280' }}
                      />
                    </Td>
                    <Td className="font-medium">
                      <Link to={`/services?group=${g.id}`} className="hover:underline">
                        {g.name}
                      </Link>
                    </Td>
                    <Td className="text-xs text-muted">{g.description || '—'}</Td>
                    <Td className="text-right font-mono text-xs">{g.check_count}</Td>
                    <Td>
                      <div className="flex justify-end gap-0.5">
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7"
                          title="Edit"
                          onClick={() => openEdit(g)}
                        >
                          <Pencil className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          className="h-7 w-7 text-muted hover:text-danger"
                          title="Delete"
                          onClick={() => setDeleting(g)}
                        >
                          <Trash2 className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </Td>
                  </Tr>
                ))}
                {!isFetching && groups.length === 0 && (
                  <Tr>
                    <Td colSpan={5} className="py-12 text-center text-muted">
                      No groups yet. Click "New group" to create one.
                    </Td>
                  </Tr>
                )}
              </TBody>
            </Table>
          </div>
        </CardContent>
      </Card>

      <Dialog open={formOpen} onOpenChange={setFormOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>{editing ? 'Edit group' : 'New group'}</DialogTitle>
          </DialogHeader>
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault()
              save.mutate()
            }}
          >
            <FormField label="Name" required>
              <Input
                autoFocus
                required
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Edge"
              />
            </FormField>
            <FormField label="Description">
              <Input
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Public-facing endpoints"
              />
            </FormField>
            <FormField label="Color">
              <div className="flex items-center gap-2">
                <input
                  type="color"
                  value={form.color}
                  onChange={(e) => setForm({ ...form, color: e.target.value })}
                  className="h-9 w-14 cursor-pointer rounded-md border border-border bg-surface2"
                />
                <Input
                  value={form.color}
                  onChange={(e) => setForm({ ...form, color: e.target.value })}
                  placeholder="#6366f1"
                  className="font-mono text-xs"
                />
              </div>
            </FormField>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setFormOpen(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={save.isPending || !form.name.trim()}>
                {editing ? 'Save changes' : 'Create group'}
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={!!deleting}
        onOpenChange={(o) => !o && setDeleting(null)}
        title={`Delete "${deleting?.name}"?`}
        description="Checks in this group will not be deleted — they'll become ungrouped."
        confirmText="Delete"
        destructive
        loading={del.isPending}
        onConfirm={() => deleting && del.mutate(deleting.id)}
      />
    </div>
  )
}
