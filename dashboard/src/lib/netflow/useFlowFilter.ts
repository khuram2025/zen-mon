/**
 * The page filter as URL state (FE-02): the URL is the single source of truth, so reload, Back,
 * and shared links reproduce exactly what is applied. Invalid values found in the link stay in the
 * URL (and are reported as "not applied") until the user removes them or clears the filters —
 * changing another filter never silently discards them.
 */
import { useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import {
  FLOW_FILTER_KEYS,
  mergeFlowFilter,
  parseFlowFilterDetailed,
  removeFlowFilterKeys,
  writeFlowFilterKeepingInvalid,
  type FlowFilter,
  type FlowFilterKey,
} from './filters'

export interface UseFlowFilterOptions {
  /** Push a history entry per change instead of replacing the current one (search forms). */
  push?: boolean
}

export function useFlowFilter(opts: UseFlowFilterOptions = {}) {
  const [sp, setSp] = useSearchParams()
  const qs = sp.toString()
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const parsed = useMemo(() => parseFlowFilterDetailed(sp), [qs])

  const write = (next: FlowFilter, dropInvalid: readonly FlowFilterKey[] = []) => {
    setSp(writeFlowFilterKeepingInvalid(sp, next, parsed.invalid, dropInvalid), { replace: !opts.push })
  }

  return {
    filter: parsed.filter,
    errors: parsed.errors,
    invalid: parsed.invalid,
    /** Apply a patch; `undefined`/`null` values remove keys. */
    setFilter: (patch: Partial<Record<FlowFilterKey, unknown>>) => write(mergeFlowFilter(parsed.filter, patch)),
    /** Replace the whole filter (e.g. a submitted search form). */
    replaceFilter: (next: FlowFilter) => write(next, FLOW_FILTER_KEYS),
    removeKeys: (keys: FlowFilterKey[]) => write(removeFlowFilterKeys(parsed.filter, keys), keys),
    clearAll: () => write({}, FLOW_FILTER_KEYS),
  }
}
