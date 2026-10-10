// Compile-time conformance of the response types in ./types.ts to the backend's golden fixtures
// (./fixtures/*.json, written by server/tests/test_inventory_fixtures.py from the real API). Nothing here
// runs; `tsc -b` in `npm run build` checks it, so the strict build fails when the contract and the types
// drift apart: a field the fixture has and the type lacks, a field the type has and the fixture lacks, or a
// field of another kind (number against string, null against a value).
//
// A JSON import has no literal types ("fresh" is a string), so a closed vocabulary (Freshness, the health
// predicates, asset kinds) is compared as `string` here; scripts/test-inventory-ui.mjs section (g) checks the
// allowed values against the same fixtures at run time. Every `@ts-expect-error` below must keep erroring:
// they prove the checks are not vacuous, and the node test asserts they are still present.
import assetDeviceHa from './fixtures/asset-device-ha.json'
import assetDevice from './fixtures/asset-device.json'
import assetServer from './fixtures/asset-server.json'
import assetsScoped from './fixtures/assets-scoped.json'
import assets from './fixtures/assets.json'
import changesScoped from './fixtures/changes-scoped.json'
import changes from './fixtures/changes.json'
import facets from './fixtures/facets.json'
import hardware from './fixtures/hardware.json'
import os from './fixtures/os.json'
import summaryScoped from './fixtures/summary-scoped.json'
import summary from './fixtures/summary.json'
import trendsScoped from './fixtures/trends-scoped.json'
import trends from './fixtures/trends.json'
import type {
  AssetDetail, AssetList, ChangesResponse, FacetsResponse, HardwareResponse, InventorySummary, OsResponse, TrendsResponse,
} from './types'

/** The shape a JSON import can have: literal unions become their primitive. */
type Widen<T> = T extends string
  ? string
  : T extends number
    ? number
    : T extends boolean
      ? boolean
      : T extends readonly (infer U)[]
        ? Widen<U>[]
        : T extends object
          ? { [K in keyof T]: Widen<T[K]> }
          : T

/** The keys the fixture has and the type does not (never when there are none). */
type Extra<A, E> = A extends readonly (infer AU)[]
  ? E extends readonly (infer EU)[]
    ? Extra<AU, EU>
    : never
  : A extends object
    ? E extends object
      ? { [K in keyof A]-?: K extends keyof E ? Extra<A[K], E[K]> : K }[keyof A]
      : never
    : never

type AssertNever<T extends never> = T

/** `value` has every field of T with the right kind (a missing or mistyped field is an error here). */
const conforms = <T,>() => <V extends Widen<T>>(value: V): V => value

export const checked = [
  conforms<InventorySummary>()(summary),
  conforms<InventorySummary>()(summaryScoped),
  conforms<AssetList>()(assets),
  conforms<AssetList>()(assetsScoped),
  conforms<FacetsResponse>()(facets),
  conforms<AssetDetail>()(assetServer),
  conforms<AssetDetail>()(assetDevice),
  conforms<AssetDetail>()(assetDeviceHa),
  conforms<OsResponse>()(os),
  conforms<HardwareResponse>()(hardware),
  conforms<ChangesResponse>()(changes),
  conforms<ChangesResponse>()(changesScoped),
  conforms<TrendsResponse>()(trends),
  conforms<TrendsResponse>()(trendsScoped),
]

// The other direction: no field in a fixture that the type does not declare.
export type NoExtraSummary = AssertNever<Extra<typeof summary, Widen<InventorySummary>>>
export type NoExtraSummaryScoped = AssertNever<Extra<typeof summaryScoped, Widen<InventorySummary>>>
export type NoExtraAssets = AssertNever<Extra<typeof assets, Widen<AssetList>>>
export type NoExtraAssetsScoped = AssertNever<Extra<typeof assetsScoped, Widen<AssetList>>>
export type NoExtraFacets = AssertNever<Extra<typeof facets, Widen<FacetsResponse>>>
export type NoExtraAssetServer = AssertNever<Extra<typeof assetServer, Widen<AssetDetail>>>
export type NoExtraAssetDevice = AssertNever<Extra<typeof assetDevice, Widen<AssetDetail>>>
export type NoExtraAssetDeviceHa = AssertNever<Extra<typeof assetDeviceHa, Widen<AssetDetail>>>
export type NoExtraOs = AssertNever<Extra<typeof os, Widen<OsResponse>>>
export type NoExtraHardware = AssertNever<Extra<typeof hardware, Widen<HardwareResponse>>>
export type NoExtraChanges = AssertNever<Extra<typeof changes, Widen<ChangesResponse>>>
export type NoExtraChangesScoped = AssertNever<Extra<typeof changesScoped, Widen<ChangesResponse>>>
export type NoExtraTrends = AssertNever<Extra<typeof trends, Widen<TrendsResponse>>>
export type NoExtraTrendsScoped = AssertNever<Extra<typeof trendsScoped, Widen<TrendsResponse>>>

// Negative controls: each of these must be rejected.
// @ts-expect-error a fixture without a field the type declares
export const dropped = conforms<InventorySummary>()({ ...summary, coverage: undefined })
// @ts-expect-error a number where the type says string
export const mistyped = conforms<InventorySummary>()({ ...summary, generated_at: 5 })
// @ts-expect-error null where the type says a value
export const nulled = conforms<InventorySummary>()({ ...summary, assets: { ...summary.assets, total: null } })
const withExtra = { ...summary, invented: 1 }
// @ts-expect-error a fixture field the type does not declare
export type Invented = AssertNever<Extra<typeof withExtra, Widen<InventorySummary>>>
