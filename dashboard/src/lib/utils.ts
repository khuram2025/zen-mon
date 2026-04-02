import { clsx, type ClassValue } from 'clsx'
import { twMerge } from 'tailwind-merge'
import type { DeviceStatus } from '@/types'

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

export const statusColors: Record<DeviceStatus, string> = {
  up: '#22C55E',
  down: '#EF4444',
  degraded: '#EAB308',
  unknown: '#6B7280',
  maintenance: '#3B82F6',
}

export const statusLabels: Record<DeviceStatus, string> = {
  up: 'Online',
  down: 'Offline',
  degraded: 'Degraded',
  unknown: 'Unknown',
  maintenance: 'Maintenance',
}

export const severityColors: Record<string, string> = {
  critical: '#EF4444',
  warning: '#EAB308',
  info: '#3B82F6',
}

export function formatRTT(ms: number | null): string {
  if (ms === null || ms === undefined) return '--'
  if (ms < 1) return `${(ms * 1000).toFixed(0)}us`
  if (ms < 100) return `${ms.toFixed(1)}ms`
  return `${ms.toFixed(0)}ms`
}

export function formatUptime(pct: number): string {
  return `${(pct * 100).toFixed(2)}%`
}

export function timeAgo(date: string | null): string {
  if (!date) return 'Never'
  const seconds = Math.floor((Date.now() - new Date(date).getTime()) / 1000)
  if (seconds < 10) return 'Just now'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`
  return `${Math.floor(seconds / 86400)}d ago`
}
