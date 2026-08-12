import { api } from '@/lib/api'
import type {
  CompareResult, DistinctPath, HopLadderRow, NetPathEvent,
  PathGraphData, Probe, SnapshotSummary, Summary,
} from './types'

const base = '/netpath'

export const netpathApi = {
  async summary(): Promise<Summary> {
    return (await api.get(`${base}/summary`)).data
  },
  async probes(params?: Record<string, any>): Promise<{ data: Probe[] }> {
    return (await api.get(`${base}/probes`, { params })).data
  },
  async probe(id: string): Promise<Probe> {
    return (await api.get(`${base}/probes/${id}`)).data
  },
  async createProbe(body: Record<string, any>): Promise<Probe> {
    return (await api.post(`${base}/probes`, body)).data
  },
  async updateProbe(id: string, body: Record<string, any>): Promise<Probe> {
    return (await api.patch(`${base}/probes/${id}`, body)).data
  },
  async deleteProbe(id: string): Promise<void> {
    await api.delete(`${base}/probes/${id}`)
  },
  async runNow(id: string): Promise<void> {
    await api.post(`${base}/probes/${id}/run`)
  },
  async snapshots(id: string, hours = 24): Promise<{ data: SnapshotSummary[] }> {
    return (await api.get(`${base}/probes/${id}/snapshots`, { params: { hours } })).data
  },
  async path(id: string, opts?: { snapshot_id?: number; at?: string }): Promise<PathGraphData> {
    return (await api.get(`${base}/probes/${id}/path`, { params: opts })).data
  },
  async hops(id: string, hours = 24): Promise<{ times: string[]; ladder: HopLadderRow[] }> {
    return (await api.get(`${base}/probes/${id}/hops`, { params: { hours } })).data
  },
  async paths(id: string): Promise<{ data: DistinctPath[] }> {
    return (await api.get(`${base}/probes/${id}/paths`)).data
  },
  async events(id: string, limit = 100): Promise<{ data: NetPathEvent[] }> {
    return (await api.get(`${base}/probes/${id}/events`, { params: { limit } })).data
  },
  async compare(id: string, a: number, b: number): Promise<CompareResult> {
    return (await api.get(`${base}/probes/${id}/compare`, { params: { a, b } })).data
  },
}
