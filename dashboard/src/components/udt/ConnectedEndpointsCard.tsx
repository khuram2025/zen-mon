import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ScanSearch } from 'lucide-react'
import { Card, CardContent } from '@/components/ui/Card'
import { Badge } from '@/components/ui/Badge'
import { udtApi } from '@/pages/udt/api'
import { ENDPOINT_TYPE_META, EndpointTypeIcon, relTime } from '@/pages/udt/helpers'

// Compact "who is plugged into this switch" card for the device detail
// page. Renders nothing until the device actually has UDT port data, so
// it stays invisible on non-switch devices.
export function ConnectedEndpointsCard({ deviceId }: { deviceId: string }) {
  const ports = useQuery({
    queryKey: ['udt', 'device-ports', deviceId, 'card'],
    queryFn: () => udtApi.devicePorts(deviceId, false),
    refetchInterval: 30_000,
    retry: false,
  })

  const withEndpoints = (ports.data?.ports || []).filter((p) => (p.active_endpoints || 0) > 0 || p.is_uplink)
  const hasData = (ports.data?.ports || []).length > 0
  if (!hasData && !ports.isLoading) return null

  const totalEndpoints = (ports.data?.ports || []).reduce((n, p) => n + (p.active_endpoints || 0), 0)

  return (
    <Card>
      <CardContent className="p-4">
        <div className="mb-3 flex items-center justify-between">
          <h3 className="flex items-center gap-2 text-sm font-semibold">
            <ScanSearch className="h-4 w-4 text-primary" /> Connected endpoints
          </h3>
          <Link to={`/udt/ports?device=${deviceId}`} className="text-xs text-primary hover:underline">
            {totalEndpoints} on {withEndpoints.length} ports →
          </Link>
        </div>
        {ports.isLoading ? (
          <div className="text-xs text-muted">Loading…</div>
        ) : withEndpoints.length === 0 ? (
          <div className="py-4 text-center text-xs text-muted">No endpoints currently attached.</div>
        ) : (
          <div className="space-y-1.5">
            {withEndpoints.slice(0, 7).map((p) => (
              <div key={p.if_index} className="flex items-center justify-between text-xs">
                <span className="font-medium">{p.if_name || `if ${p.if_index}`}</span>
                <span className="flex items-center gap-2 text-muted">
                  {p.is_uplink ? (
                    <Badge variant="info">uplink</Badge>
                  ) : (
                    <span className="tabular-nums">{p.active_endpoints} {p.active_endpoints === 1 ? 'device' : 'devices'}</span>
                  )}
                  {p.last_endpoint_seen && <span className="text-[10px]">{relTime(p.last_endpoint_seen)}</span>}
                </span>
              </div>
            ))}
            {withEndpoints.length > 7 && (
              <Link to={`/udt/ports?device=${deviceId}`} className="block pt-1 text-xs text-primary hover:underline">
                View all {withEndpoints.length} ports
              </Link>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  )
}
