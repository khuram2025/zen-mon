import { ChangeEvent, useRef, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { File, Trash2, Upload } from 'lucide-react'
import { api } from '@/lib/api'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'
import { Button } from '@/components/ui/Button'
import { Table, THead, TBody, Tr, Th, Td } from '@/components/ui/Table'
import { formatBytes, relativeTime } from '@/lib/utils'

export function MibsTabContent() {
  const qc = useQueryClient()
  const fileInput = useRef<HTMLInputElement>(null)
  const [uploadError, setUploadError] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [browse, setBrowse] = useState(false)
  const [compileResult, setCompileResult] = useState<any>(null)

  const compile = useMutation({
    mutationFn: async () => (await api.post('/snmp/mibs/compile')).data,
    onSuccess: (result) => { setCompileResult(result); setBrowse(true); qc.invalidateQueries({ queryKey: ['mib-objects'] }) },
    onError: (e: any) => setUploadError(e?.response?.data?.detail || 'Compilation failed'),
  })
  const objects = useQuery<any>({
    queryKey: ['mib-objects', search], enabled: browse,
    queryFn: async () => (await api.get('/snmp/mibs/objects', { params: { search } })).data,
  })

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
    <div className="space-y-5">
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Upload className="h-4 w-4 text-primary" />
            Upload MIB
          </CardTitle>
          <p className="text-xs text-muted">
            Upload vendor MIB files and their imported dependencies, then compile the library to browse numeric OIDs for monitoring templates.
          </p>
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
        <CardHeader><CardTitle>MIB symbols</CardTitle></CardHeader>
        <CardContent className="space-y-3">
          <div className="flex gap-3">
            <Button onClick={() => compile.mutate()} disabled={compile.isPending}>{compile.isPending ? 'Compiling…' : 'Compile library'}</Button>
            <Button variant="ghost" onClick={() => setBrowse(true)}>Browse compiled symbols</Button>
          </div>
          {compileResult && <p className="text-sm">{compileResult.compiled_modules.length} modules compiled; {compileResult.object_count} symbols.</p>}
          {Object.entries(compileResult?.errors || {}).map(([file, error]) => <p key={file} className="text-sm text-danger">{file}: {String(error)}</p>)}
          {browse && <>
            <input aria-label="Search MIB symbols" value={search} onChange={e => setSearch(e.target.value)} placeholder="Search module, symbol or OID" className="w-full rounded border border-border bg-background p-2 text-sm" />
            {objects.isError && <p className="text-sm text-danger">{(objects.error as any)?.response?.data?.detail || 'Unable to load symbols'}</p>}
            <div className="max-h-96 overflow-auto"><Table><THead><Tr><Th>Symbol</Th><Th>Numeric OID</Th></Tr></THead><TBody>
              {(objects.data?.data || []).map((o: any) => <Tr key={`${o.module}::${o.symbol}`}><Td className="text-xs">{o.module}::{o.symbol}</Td><Td className="font-mono text-xs select-all">{o.oid}</Td></Tr>)}
            </TBody></Table></div>
            <p className="text-xs text-muted">Showing up to 500 matches. Scalar polling usually requires a .0 instance suffix; table columns use their row index.</p>
          </>}
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
