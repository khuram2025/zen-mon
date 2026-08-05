import { api } from '@/lib/api'
import type {
  CapacityRow, DomainController, Endpoint, EndpointDetail, EndpointList,
  UdtEvent, UdtPort, UdtRule, UdtSummary,
} from './types'

const base = '/udt'

export const udtApi = {
  async summary(): Promise<UdtSummary> {
    return (await api.get(`${base}/summary`)).data
  },
  async endpoints(params: Record<string, any>): Promise<EndpointList> {
    return (await api.get(`${base}/endpoints`, { params })).data
  },
  async endpoint(id: string): Promise<EndpointDetail> {
    return (await api.get(`${base}/endpoints/${id}`)).data
  },
  async updateEndpoint(id: string, body: Record<string, any>) {
    return (await api.patch(`${base}/endpoints/${id}`, body)).data
  },
  async devicePorts(deviceId: string, includeEmpty = true): Promise<{ device: { id: string; hostname: string }; ports: UdtPort[] }> {
    return (await api.get(`${base}/devices/${deviceId}/ports`, { params: { include_empty: includeEmpty } })).data
  },
  async portEndpoints(deviceId: string, ifIndex: number): Promise<{ data: Endpoint[] }> {
    return (await api.get(`${base}/devices/${deviceId}/ports/${ifIndex}/endpoints`)).data
  },
  async updatePort(deviceId: string, ifIndex: number, body: Record<string, any>) {
    return (await api.patch(`${base}/ports/${deviceId}/${ifIndex}`, body)).data
  },
  async portAction(deviceId: string, ifIndex: number, action: string) {
    return (await api.post(`${base}/ports/${deviceId}/${ifIndex}/action`, { action })).data
  },
  async ports(params: Record<string, any>): Promise<{ data: any[]; meta: any }> {
    return (await api.get(`${base}/ports`, { params })).data
  },
  async capacity(): Promise<{ data: CapacityRow[] }> {
    return (await api.get(`${base}/capacity`)).data
  },
  async capacityTrend(deviceId: string, days = 30): Promise<{ data: any[] }> {
    return (await api.get(`${base}/capacity/${deviceId}/trend`, { params: { days } })).data
  },
  async vendors(): Promise<{ data: { vendor: string; count: number; types: Record<string, number> }[] }> {
    return (await api.get(`${base}/vendors`)).data
  },
  async rules(listType?: string): Promise<{ data: UdtRule[] }> {
    return (await api.get(`${base}/rules`, { params: listType ? { list_type: listType } : {} })).data
  },
  async createRule(body: Record<string, any>) {
    return (await api.post(`${base}/rules`, body)).data
  },
  async updateRule(id: string, body: Record<string, any>) {
    return (await api.patch(`${base}/rules/${id}`, body)).data
  },
  async deleteRule(id: string) {
    return (await api.delete(`${base}/rules/${id}`)).data
  },
  async rogues(params: Record<string, any> = {}): Promise<{ data: Endpoint[]; meta: any }> {
    return (await api.get(`${base}/rogues`, { params })).data
  },
  async users(params: Record<string, any> = {}): Promise<{ data: any[] }> {
    return (await api.get(`${base}/users`, { params })).data
  },
  async user(name: string): Promise<{ user: string; logins: any[]; endpoints: any[] }> {
    return (await api.get(`${base}/users/${encodeURIComponent(name)}`)).data
  },
  async events(params: Record<string, any> = {}): Promise<{ data: UdtEvent[] }> {
    return (await api.get(`${base}/events`, { params })).data
  },
  async domainControllers(): Promise<{ data: DomainController[] }> {
    return (await api.get(`${base}/domain-controllers`)).data
  },
  async createDC(body: Record<string, any>) {
    return (await api.post(`${base}/domain-controllers`, body)).data
  },
  async updateDC(id: string, body: Record<string, any>) {
    return (await api.patch(`${base}/domain-controllers/${id}`, body)).data
  },
  async deleteDC(id: string) {
    return (await api.delete(`${base}/domain-controllers/${id}`)).data
  },
  async pollDC(id: string) {
    return (await api.post(`${base}/domain-controllers/${id}/poll`)).data
  },
}
