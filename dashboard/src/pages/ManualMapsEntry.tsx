import { MapEditorV2 } from '@/features/maps/MapEditorV2'

/* Route entry for /maps/manual. The React-Flow editor (v2) is now the default
 * and only editor. The legacy editor (ManualMapsPage.tsx) has been retired. */
export function ManualMapsEntry() {
  return <MapEditorV2 />
}
