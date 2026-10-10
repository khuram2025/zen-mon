// Compile-time conformance of what the Operating Systems, Hardware and Changes pages read (the types
// re-exported by ./osHardwareChanges) to the backend's golden fixtures (./fixtures/*.json, written by
// server/tests/test_inventory_fixtures.py from the real API), and of the Changes page's filter shape to the API's
// own query parameters. Nothing here runs; `tsc -b` in `npm run build` checks it, so the strict build fails when the
// contract and the types the pages use drift apart. Every `@ts-expect-error` below must keep erroring: they prove the
// checks are not vacuous, and scripts/test-inventory-os-hardware-changes.mjs asserts they are still here.
//
// ./contract.typecheck.ts already holds the response types to the same fixtures; this file does it through the
// names the three pages import, so a re-export that quietly changed would fail here too.
import changesScoped from './fixtures/changes-scoped.json'
import changes from './fixtures/changes.json'
import hardware from './fixtures/hardware.json'
import os from './fixtures/os.json'
import type { ChangesQuery } from './api'
import { changesQuery } from '@/components/inventory/changesModel'
import type { ChangesFilters, ChangesResponse, HardwareResponse, OsResponse } from './osHardwareChanges'
import type { ChangeRow } from './types'

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
type AssertTrue<T extends true> = T

const conforms = <T,>() => <V extends Widen<T>>(value: V): V => value

export const checked = [
  conforms<OsResponse>()(os),
  conforms<HardwareResponse>()(hardware),
  conforms<ChangesResponse>()(changes),
  conforms<ChangesResponse>()(changesScoped),
  // One event row, the way the pairing rule reads it (every field it looks at exists, with the right kind).
  conforms<ChangeRow>()(changes.items[0]),
]

export type NoExtraOs = AssertNever<Extra<typeof os, Widen<OsResponse>>>
export type NoExtraHardware = AssertNever<Extra<typeof hardware, Widen<HardwareResponse>>>
export type NoExtraChanges = AssertNever<Extra<typeof changes, Widen<ChangesResponse>>>
export type NoExtraChangesScoped = AssertNever<Extra<typeof changesScoped, Widen<ChangesResponse>>>

// The filters of the Changes page ARE the API's parameters: the same keys, minus the paging and the zone the
// page adds itself (the API's `tz`, `page`, `page_size`).
type FilterKeys = keyof ChangesFilters
type QueryKeys = Exclude<keyof ChangesQuery, 'tz' | 'page' | 'page_size'>
export type SameKeys = AssertTrue<[FilterKeys] extends [QueryKeys] ? ([QueryKeys] extends [FilterKeys] ? true : false) : false>
// And a filter set becomes exactly a ChangesQuery.
export const request: ChangesQuery = changesQuery({ category: 'software', action: 'changed', label: 'upgrade' }, 1, 50, 'UTC')

// Negative controls: each of these must be rejected.
// @ts-expect-error a fixture without a field the type declares
export const dropped = conforms<OsResponse>()({ ...os, families: undefined })
// @ts-expect-error a number where the type says string
export const mistyped = conforms<ChangesResponse>()({ ...changes, generated_at: 5 })
// @ts-expect-error a filter outside the API's vocabulary does not compile
export const invented: ChangesFilters = { category: 'software', colour: 'red' }
const withExtra = { ...hardware, invented: 1 }
// @ts-expect-error a fixture field the type does not declare
export type Invented = AssertNever<Extra<typeof withExtra, Widen<HardwareResponse>>>
