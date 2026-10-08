/** Icon names used by the report catalog (categories and templates). */
import {
  Activity, AppWindow, BarChart3, Bell, Boxes, Briefcase, Cpu, FileCheck2, FileText, Gauge, Globe,
  HardDrive, Layers, MonitorSmartphone, MousePointerClick, Network, PieChart, Route, Server,
  Settings2, ShieldCheck, TrendingUp, Waypoints, Wrench, type LucideIcon,
} from 'lucide-react'

const ICONS: Record<string, LucideIcon> = {
  Activity, AppWindow, BarChart3, Bell, Boxes, Briefcase, Cpu, FileCheck2, FileText, Gauge, Globe,
  HardDrive, Layers, MonitorSmartphone, MousePointerClick, Network, PieChart, Route, Server,
  Settings2, ShieldCheck, TrendingUp, Waypoints, Wrench,
}

export function reportIcon(name?: string | null, fallback: LucideIcon = FileText): LucideIcon {
  return (name && ICONS[name]) || fallback
}
