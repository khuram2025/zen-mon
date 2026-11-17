import { SelectItem } from '@/components/ui/Select'
import { useDeviceTypes } from '@/hooks/useDeviceTypes'

/** Select options for every type in the device type catalogue (migrate-189). */
export function DeviceTypeItems() {
  const { types } = useDeviceTypes()
  return (
    <>
      {types.map((t) => (
        <SelectItem key={t.key} value={t.key}>{t.label}</SelectItem>
      ))}
    </>
  )
}
