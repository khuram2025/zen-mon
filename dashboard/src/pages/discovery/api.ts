import { api } from '@/lib/api'
import {
  DiscoveryProfile,
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
