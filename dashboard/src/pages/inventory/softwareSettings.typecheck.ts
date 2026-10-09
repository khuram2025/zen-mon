// Compile-time conformance of the response types in ./softwareSettings.ts to the backend's golden fixtures
// (./fixtures/software*.json and settings.json, written by server/tests/test_inventory_fixtures.py from the
// real API). Nothing here runs; `tsc -b` in `npm run build` checks it, so the strict build fails when the
// contract and the types drift apart: a field the fixture has and the type lacks, a field the type has and the
// fixture lacks, or a field of another kind (number against string, null against a value).
//
// Same technique as ./contract.typecheck.ts (a JSON import has no literal types, so a closed vocabulary is
// compared as `string`; scripts/test-inventory-software-settings.mjs checks the allowed values against the same
// fixtures at run time). Every `@ts-expect-error` below must keep erroring: they prove the checks are not
// vacuous, and the node test asserts they are still present.
import installations from './fixtures/software-installations.json'
import installationsScoped from './fixtures/software-installations-scoped.json'
import product from './fixtures/software-product.json'
import productScoped from './fixtures/software-product-scoped.json'
import publishers from './fixtures/software-publishers.json'
import software from './fixtures/software.json'
import softwareScoped from './fixtures/software-scoped.json'
import settings from './fixtures/settings.json'
import type {
  CatalogResponse, InstallationsResponse, ProductResponse, PublishersResponse, SettingsPatch, SettingsResponse,
} from './softwareSettings'

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
  conforms<CatalogResponse>()(software),
  conforms<CatalogResponse>()(softwareScoped),
  conforms<PublishersResponse>()(publishers),
  conforms<ProductResponse>()(product),
  conforms<ProductResponse>()(productScoped),
  conforms<InstallationsResponse>()(installations),
  conforms<InstallationsResponse>()(installationsScoped),
  conforms<SettingsResponse>()(settings),
]

// The other direction: no field in a fixture that the type does not declare.
export type NoExtraSoftware = AssertNever<Extra<typeof software, Widen<CatalogResponse>>>
export type NoExtraSoftwareScoped = AssertNever<Extra<typeof softwareScoped, Widen<CatalogResponse>>>
export type NoExtraPublishers = AssertNever<Extra<typeof publishers, Widen<PublishersResponse>>>
export type NoExtraProduct = AssertNever<Extra<typeof product, Widen<ProductResponse>>>
export type NoExtraProductScoped = AssertNever<Extra<typeof productScoped, Widen<ProductResponse>>>
export type NoExtraInstallations = AssertNever<Extra<typeof installations, Widen<InstallationsResponse>>>
export type NoExtraInstallationsScoped = AssertNever<Extra<typeof installationsScoped, Widen<InstallationsResponse>>>
export type NoExtraSettings = AssertNever<Extra<typeof settings, Widen<SettingsResponse>>>

// What the settings form sends is a subset of what the API returns: the stored settings are a valid patch.
export const roundTrip: SettingsPatch = settings.settings

// Negative controls: each of these must be rejected.
// @ts-expect-error a fixture without a field the type declares
export const dropped = conforms<CatalogResponse>()({ ...software, hidden_installs: undefined })
// @ts-expect-error a number where the type says string
export const mistyped = conforms<CatalogResponse>()({ ...software, generated_at: 5 })
// @ts-expect-error null where the type says a value
export const nulled = conforms<ProductResponse>()({ ...product, unversioned: { ...product.unversioned, hosts: null } })
const withExtra = { ...settings, invented: 1 }
// @ts-expect-error a fixture field the type does not declare
export type Invented = AssertNever<Extra<typeof withExtra, Widen<SettingsResponse>>>
// @ts-expect-error a setting the API does not know
export const unknownKey: SettingsPatch = { apple: 1 }
