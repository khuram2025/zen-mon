import { Navigate, useParams, useSearchParams } from 'react-router-dom'
import { interfacesTabUrl } from './device/interfaceModel'

/** The Interfaces page now lives inside the device shell as a tab. Old links
 *  (/devices/:id/interfaces?range=…) keep working: every search parameter is
 *  carried over except a stale `tab`, which the redirect sets. */
export function DeviceInterfacesPage() {
  const { id } = useParams<{ id: string }>()
  const [params] = useSearchParams()
  return <Navigate replace to={interfacesTabUrl(id || '', params)} />
}
