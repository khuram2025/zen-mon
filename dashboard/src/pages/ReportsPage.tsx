import { FileText } from 'lucide-react'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card'

export function ReportsPage() {
  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <FileText className="h-6 w-6 text-primary" />
          Reports
        </h1>
        <p className="text-sm text-muted">PDF and CSV exports</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Reporting</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-sm text-muted">
            Reports are generated per-device from the Device Detail page. Click "PDF
            Report" on any device to export a 30-day uptime summary. Bulk report scheduling
            is planned for the next release.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}
