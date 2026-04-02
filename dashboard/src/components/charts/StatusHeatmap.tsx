import { statusColors } from '@/lib/utils'
import type { Device, DeviceStatus } from '@/types'
import { useNavigate } from 'react-router-dom'

interface StatusHeatmapProps {
  devices: Device[]
}

export function StatusHeatmap({ devices }: StatusHeatmapProps) {
  const navigate = useNavigate()

  return (
    <div className="bg-[var(--bg-secondary)] rounded-xl border border-[var(--bg-elevated)] p-5">
      <h3 className="text-sm font-semibold text-[var(--text-primary)] mb-4">
        Device Status Overview
      </h3>
      <div className="flex flex-wrap gap-1">
        {devices.map((device) => (
          <button
            key={device.id}
            onClick={() => navigate(`/devices/${device.id}`)}
            className="w-5 h-5 rounded-sm transition-transform hover:scale-150 cursor-pointer"
            style={{ backgroundColor: statusColors[device.status as DeviceStatus] || '#6B7280' }}
            title={`${device.hostname} (${device.ip_address}) - ${device.status}`}
          />
        ))}
      </div>
      <div className="flex items-center gap-4 mt-4 text-xs text-[var(--text-muted)]">
        {(['up', 'degraded', 'down', 'unknown'] as DeviceStatus[]).map((s) => (
          <div key={s} className="flex items-center gap-1.5">
            <span
              className="w-3 h-3 rounded-sm"
              style={{ backgroundColor: statusColors[s] }}
            />
            <span className="capitalize">{s}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
