import { api } from '@/lib/api'
import {
  AssetAutomation,
  AssetKind,
  ConflictReport,
  InventoryApplyResult,
  InventoryPreview,
  InventoryRunRequest,
  UndoResult,
  DiscoveryProfile,
  DiscoveryRule,
  EngineRun,
  EngineRunItem,
  EvaluateScanCounts,
  PreviewRequest,
  PreviewResponse,
  RuleCatalog,
  RuleDraft,
  RuleEngineSettings,
  RuleMode,
  RuleTrigger,
  RuleVersion,
  DiscoveryResult,
  DiscoveryRun,
  DiscoverySchedule,
  IgnoredDevice,
  ImportBatch,
  ImportResponse,
} from './types'

const base = '/discovery-v2'

export const discoveryApi = {
  // Profiles
  async listProfiles(): Promise<DiscoveryProfile[]> {
    return (await api.get(`${base}/profiles`)).data
  },
  async getProfile(id: string): Promise<DiscoveryProfile> {
    return (await api.get(`${base}/profiles/${id}`)).data
  },
  async createProfile(payload: any): Promise<DiscoveryProfile> {
    return (await api.post(`${base}/profiles`, payload)).data
  },
  async updateProfile(id: string, payload: any): Promise<DiscoveryProfile> {
    return (await api.patch(`${base}/profiles/${id}`, payload)).data
  },
  async cloneProfile(id: string): Promise<DiscoveryProfile> {
    return (await api.post(`${base}/profiles/${id}/clone`)).data
  },
  async deleteProfile(id: string): Promise<void> {
    await api.delete(`${base}/profiles/${id}`)
  },
  async estimate(targets: string[], exclusions: string[] = []) {
    return (await api.post(`${base}/estimate`, { targets, exclusions })).data as {
      ip_count: number
      preview: string[]
      truncated: boolean
      warnings: string[]
    }
  },

  // Schedules
  async listSchedules(): Promise<DiscoverySchedule[]> {
    return (await api.get(`${base}/schedules`)).data
  },
  async upsertSchedule(profileId: string, payload: any): Promise<DiscoverySchedule> {
    return (await api.post(`${base}/profiles/${profileId}/schedule`, payload)).data
  },
  async pauseSchedule(id: string) {
    return (await api.post(`${base}/schedules/${id}/pause`)).data
  },
  async resumeSchedule(id: string) {
    return (await api.post(`${base}/schedules/${id}/resume`)).data
  },
  async deleteSchedule(id: string) {
    await api.delete(`${base}/schedules/${id}`)
  },

  // Runs
  async startRun(profileId: string): Promise<DiscoveryRun> {
    return (
      await api.post(`${base}/profiles/${profileId}/run`, { trigger_type: 'manual' })
    ).data
  },
  async listRuns(profileId?: string): Promise<DiscoveryRun[]> {
    const params = profileId ? { profile_id: profileId } : {}
    return (await api.get(`${base}/runs`, { params })).data
  },
  async getRun(id: string): Promise<DiscoveryRun> {
    return (await api.get(`${base}/runs/${id}`)).data
  },
  async cancelRun(id: string) {
    return (await api.post(`${base}/runs/${id}/cancel`)).data
  },
  async tickScheduler() {
    return (await api.post(`${base}/scheduler/tick`)).data as {
      triggered: string[]
      checked: number
    }
  },

  // Results
  async listResults(runId: string, status?: string): Promise<DiscoveryResult[]> {
    const params: Record<string, any> = {}
    if (status) params.status = status
    return (await api.get(`${base}/runs/${runId}/results`, { params })).data
  },

  // Import / ignore
  async importResults(runId: string, payload: any): Promise<ImportResponse> {
    return (await api.post(`${base}/runs/${runId}/import`, payload)).data
  },
  async ignoreResults(payload: { result_ids?: number[]; ip_address?: string; reason?: string }) {
    return (await api.post(`${base}/ignore`, payload)).data
  },
  async listIgnored(): Promise<IgnoredDevice[]> {
    return (await api.get(`${base}/ignored`)).data
  },
  async removeIgnored(id: string) {
    await api.delete(`${base}/ignored/${id}`)
  },

  // Import batches
  async listImports(opts: { run_id?: string; profile_id?: string } = {}): Promise<ImportBatch[]> {
    return (await api.get(`${base}/imports`, { params: opts })).data
  },
}

const rulesBase = `${base}/rules`

/** The rule body the API takes for create / update (position or expected_revision added by the caller). */
export function ruleBody(draft: RuleDraft) {
  return {
    name: draft.name.trim(),
    description: draft.description?.trim() || null,
    mode: draft.mode,
    asset_kinds: draft.asset_kinds,
    profile_ids: draft.profile_ids && draft.profile_ids.length ? draft.profile_ids : null,
    condition: draft.condition,
    actions: draft.actions,
    stop_processing: draft.stop_processing,
    triggers: draft.triggers?.length ? draft.triggers : (['discovery'] as RuleTrigger[]),
    enforcement: draft.enforcement || ('set_once' as const),
  }
}

export const rulesApi = {
  async catalog(): Promise<RuleCatalog> {
    return (await api.get(`${rulesBase}/catalog`)).data
  },
  async getSettings(): Promise<RuleEngineSettings> {
    return (await api.get(`${rulesBase}/settings`)).data
  },
  async putSettings(payload: RuleEngineSettings): Promise<RuleEngineSettings> {
    return (await api.put(`${rulesBase}/settings`, payload)).data
  },
  async list(): Promise<DiscoveryRule[]> {
    return (await api.get(rulesBase)).data
  },
  async get(id: string): Promise<DiscoveryRule> {
    return (await api.get(`${rulesBase}/${id}`)).data
  },
  async create(draft: RuleDraft, position: 'first' | 'last' = 'last'): Promise<DiscoveryRule> {
    return (await api.post(rulesBase, { ...ruleBody(draft), position })).data
  },
  async update(id: string, draft: RuleDraft, expectedRevision: number | null): Promise<DiscoveryRule> {
    return (await api.put(`${rulesBase}/${id}`, { ...ruleBody(draft), expected_revision: expectedRevision })).data
  },
  async remove(id: string): Promise<void> {
    await api.delete(`${rulesBase}/${id}`)
  },
  async clone(id: string): Promise<DiscoveryRule> {
    return (await api.post(`${rulesBase}/${id}/clone`)).data
  },
  async setMode(id: string, mode: RuleMode): Promise<DiscoveryRule> {
    return (await api.post(`${rulesBase}/${id}/mode`, { mode })).data
  },
  async versions(id: string): Promise<RuleVersion[]> {
    return (await api.get(`${rulesBase}/${id}/versions`)).data
  },
  async reorder(ids: string[]): Promise<DiscoveryRule[]> {
    return (await api.post(`${rulesBase}/reorder`, { ids })).data
  },
  async preview(payload: PreviewRequest): Promise<PreviewResponse> {
    return (await api.post(`${rulesBase}/preview`, payload)).data
  },
  async evaluateScan(runId: string, apply: boolean): Promise<EvaluateScanCounts> {
    return (await api.post(`${rulesBase}/evaluate-scan/${runId}`, { apply })).data
  },
  async history(opts: { limit?: number; discovery_run_id?: string } = {}): Promise<EngineRun[]> {
    return (await api.get(`${rulesBase}/history`, { params: opts })).data
  },
  async historyItems(
    engineRunId: string,
    opts: { outcome?: string; rule_id?: string; limit?: number; offset?: number } = {},
  ): Promise<EngineRunItem[]> {
    return (await api.get(`${rulesBase}/history/${engineRunId}/items`, { params: opts })).data
  },

  // Phase 2: rules on devices and servers already in inventory.
  /** What the active (or chosen active) rules would change; writes nothing. */
  async inventoryPreview(payload: InventoryRunRequest): Promise<InventoryPreview> {
    return (await api.post(`${rulesBase}/inventory/preview`, payload)).data
  },
  /** Apply exactly the previewed changes; 409 `preview_stale` when anything changed since. */
  async inventoryApply(payload: InventoryRunRequest & { digest: string }): Promise<InventoryApplyResult> {
    return (await api.post(`${rulesBase}/inventory/apply`, payload)).data
  },
  async undo(engineRunId: string): Promise<UndoResult> {
    return (await api.post(`${rulesBase}/history/${engineRunId}/undo`)).data
  },
  /** Which rules set what on a device or server, hand edits kept, rules that match now. */
  async asset(kind: AssetKind, assetId: string): Promise<AssetAutomation> {
    return (await api.get(`${rulesBase}/assets/${kind}/${assetId}`)).data
  },
  async conflicts(): Promise<ConflictReport> {
    return (await api.get(`${rulesBase}/conflicts`)).data
  },
}
