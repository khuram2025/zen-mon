// Compile-time tests for the filter vocabulary (nothing here runs; `tsc -b` in `npm run build`
// checks it). Each `@ts-expect-error` must keep erroring: if the types are ever loosened so the line
// compiles, the directive becomes unused and the build fails. scripts/test-inventory-ui.mjs checks
// that the directives are still here.
import { DrillLink } from '@/components/inventory/DrillLink'
import { assetsHref, patchAssetFilters, type AssetFilters } from './filters'

// Accepted: every key and value of the vocabulary.
export const accepted: AssetFilters = {
  q: 'fortigate',
  kind: 'device',
  os_family: 'fortios',
  os_product: 'FortiOS',
  os_release: '7.2',
  os_version: '7.2.5',
  vendor: 'Fortinet',
  model: 'FortiGate-200F',
  site: 'dc1',
  tag: ['dc-east', 'pci'],
  source: 'snmp',
  freshness: 'stale_or_never',
  health: 'missing_serial',
  product: 'google-chrome',
  product_version: '152.0.8',
  cpu_model: 'Intel Xeon Gold 6338',
  memory: 'le64',
  disks: '3-4',
  virtualization: 'vmware',
  security_agent: 'missing',
  module_class: 'powerSupply',
  module_model: 'PWR-C1-715WAC',
}
// The Hardware page's component drill-downs and the sentinel for "unknown" (vendor, model and the OS levels).
export const components = assetsHref({ kind: 'device', module_class: 'fan', module_model: 'unknown' })
export const unknownVendor = assetsHref({ kind: 'device', vendor: 'unknown', freshness: 'not_applicable' })
export const drill = <DrillLink filters={{ os_family: 'windows', os_release: '2019' }} count={23}>23</DrillLink>
export const href = assetsHref({ health: 'missing_security_agent', kind: 'server' }, { sort: 'name', order: 'desc', page: 2, page_size: 100 })
export const patched = patchAssetFilters({}, { vendor: null, tag: [] })

// @ts-expect-error a key outside the vocabulary does not compile
export const unknownKey = <DrillLink filters={{ vendor_name: 'Fortinet' }}>x</DrillLink>
// @ts-expect-error kind is server or device
export const badKind = assetsHref({ kind: 'router' })
// @ts-expect-error health is a closed set of predicates
export const badHealth = assetsHref({ health: 'orphaned' })
// @ts-expect-error freshness is a closed set
export const badFreshness = assetsHref({ freshness: 'recent' })
// @ts-expect-error memory is one of the buckets
export const badMemory = assetsHref({ memory: '64gb' })
// @ts-expect-error disks is one of the buckets
export const badDisks = assetsHref({ disks: 4 })
// @ts-expect-error security_agent is present, missing or unknown
export const badAgent = assetsHref({ security_agent: 'none' })
// @ts-expect-error module_class is one of the ENTITY-MIB classes
export const badModuleClass = assetsHref({ module_class: 'transceiver' })
// @ts-expect-error module_model is text
export const badModuleModel = assetsHref({ module_model: 5 })
// @ts-expect-error tag is repeatable, so it is a list
export const badTag = assetsHref({ tag: 'dc-east' })
// @ts-expect-error the page size is a number
export const badPageSize = assetsHref({}, { page_size: '100' })
// @ts-expect-error sort is one of the sortable columns
export const badSort = assetsHref({}, { sort: 'colour' })
