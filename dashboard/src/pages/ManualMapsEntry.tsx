import { useSearchParams } from 'react-router-dom'
import { ManualMapsPage } from './ManualMapsPage'
import { MapEditorV2 } from '@/features/maps/MapEditorV2'

/* Route entry for /maps/manual. The new React-Flow editor is opt-in behind
 * ?v2 during rollout; without it the classic editor renders unchanged. */
export function ManualMapsEntry() {
  const [params] = useSearchParams()
  return params.get('v2') ? <MapEditorV2 /> : <ManualMapsPage />
}
