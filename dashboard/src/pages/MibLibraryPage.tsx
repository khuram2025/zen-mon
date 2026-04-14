import { ChangeEvent, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { File, Trash2, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { formatBytes, relativeTime } from '@/lib/utils'

export function MibLibraryPage() {
  const qc = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)

  const { data: mibs } = useQuery<any[]>({
    queryKey: ['mibs'],
    queryFn: async () => (await api.get('/snmp/mibs')).data,
  })

  const uploadMib = useMutation({
    mutationFn: async (file: File) => {
      const form = new FormData()
      form.append('file', file)
      return (
        await api.post('/snmp/mibs', form, {
          headers: { 'Content-Type': 'multipart/form-data' },
        })
      ).data
    },
    onSuccess: () => {
      setUploadError(null)
      if (fileInput.current) fileInput.current.value = ''
      qc.invalidateQueries({ queryKey: ['mibs'] })
    },
    onError: (e: any) => {
      setUploadError(e?.response?.data?.detail || 'Upload failed')
    },
  })

  const deleteMib = useMutation({
    mutationFn: async (id: string) => api.delete(`/snmp/mibs/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['mibs'] }),
  })

  function onPick(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0]
    if (file) uploadMib.mutate(file)
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Upload className="h-6 w-6 text-primary" />
          MIB Library
        </h1>
        <p className="text-sm text-muted">
          Upload vendor MIB files. Files are stored on disk; runtime compilation lands in a later update.
        </p>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Upload MIB</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
            <input
              ref={fileInput}
              type="file"
              onChange={onPick}
              accept=".mib,.txt,.my"
              className="block w-full text-sm text-muted file:mr-4 file:rounded-md file:border-0 file:bg-primary file:px-4 file:py-2 file:text-sm file:font-medium file:text-white file:hover:bg-primary/90"
            />
            {uploadMib.isPending && <span className="text-sm text-muted">Uploading…</span>}
          </div>
          {uploadError && (
            <div className="mt-3 rounded-md border border-danger/30 bg-danger/10 px-3 py-2 text-sm text-danger">
              {uploadError}
            </div>
          )}
          <p className="mt-3 text-xs text-muted">
            Max 4 MB. Filename can only contain letters, digits, and <code>._-</code>. Duplicates overwrite.
          </p>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Installed MIBs ({mibs?.length || 0})</CardTitle>
        </CardHeader>
        <CardContent>
          <Table>
            <THead>
              <Tr>
                <Th>Name</Th>
                <Th>Filename</Th>
                <Th>Size</Th>
                <Th>SHA-256</Th>
                <Th>Uploaded</Th>
                <Th></Th>
              </Tr>
            </THead>
            <TBody>
              {(mibs || []).map((m) => (
                <Tr key={m.id}>
                  <Td>
                    <div className="flex items-center gap-2 font-medium">
                      <File className="h-4 w-4 text-muted" />
                      {m.name}
                    </div>
                  </Td>
                  <Td className="font-mono text-xs text-muted">{m.filename}</Td>
                  <Td className="text-xs">{formatBytes(m.size_bytes)}</Td>
                  <Td className="truncate max-w-[160px] font-mono text-xs text-muted" title={m.sha256}>
                    {m.sha256.slice(0, 16)}…
                  </Td>
                  <Td className="text-xs text-muted">{relativeTime(m.uploaded_at)}</Td>
                  <Td>
                    <Button
                      variant="ghost"
                      size="icon"
                      title="Delete"
                      onClick={() => {
                        if (confirm(`Delete MIB "${m.name}"?`)) deleteMib.mutate(m.id)
                      }}
                    >
                      <Trash2 className="h-4 w-4" />
                    </Button>
                  </Td>
                </Tr>
              ))}
              {(!mibs || mibs.length === 0) && (
                <Tr>
                  <Td colSpan={6} className="py-12 text-center text-muted">
                    No MIBs uploaded yet
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>
        </CardContent>
      </Card>
    </div>
  )
}
