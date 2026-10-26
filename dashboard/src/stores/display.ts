import { create } from 'zustand'

/**
 * Display density and the NOC wall (kiosk) mode, per browser.
 *
 * Density sets the root font size (styles/globals.css), so type, spacing and controls scale together:
 * compact 14px (laptops), comfortable 15px (desktop default), large 17px (1440p+ monitors), wall 20px (a NOC video
 * wall read from across the room). Wall mode also hides the sidebar and header so the page fills the screen.
 *
 * Kiosk screens can be set up by URL alone: `?density=wall&kiosk=1` (the URL wins over the stored choice, and is
 * not saved, so an engineer's own browser keeps its setting). The density is stored as a plain string in
 * `zp-density` because index.html applies it before React loads.
 */
export type Density = 'compact' | 'comfortable' | 'large' | 'wall'

export const DENSITIES: { value: Density; label: string; hint: string; rootPx: number }[] = [
  { value: 'compact', label: 'Compact', hint: 'More on screen, for laptops', rootPx: 14 },
  { value: 'comfortable', label: 'Comfortable', hint: 'Default for desktop monitors', rootPx: 15 },
  { value: 'large', label: 'Large', hint: '1440p and 4K monitors', rootPx: 17 },
  { value: 'wall', label: 'NOC wall', hint: 'Video walls read from a distance', rootPx: 20 },
]

const KEY = 'zp-density'

function isDensity(v: unknown): v is Density {
  return v === 'compact' || v === 'comfortable' || v === 'large' || v === 'wall'
}

function urlParams(): URLSearchParams {
  try { return new URLSearchParams(window.location.search) } catch { return new URLSearchParams() }
}

function initialDensity(): Density {
  const q = urlParams().get('density')
  if (isDensity(q)) return q
  try {
    const v = localStorage.getItem(KEY)
    if (isDensity(v)) return v
  } catch { /* private mode */ }
  return 'comfortable'
}

function initialKiosk(): boolean {
  const k = urlParams().get('kiosk')
  return k === '1' || k === 'true'
}

export function applyDensity(d: Density) {
  if (typeof document === 'undefined') return
  if (d === 'comfortable') document.documentElement.removeAttribute('data-density')
  else document.documentElement.setAttribute('data-density', d)
}

type DisplayState = {
  density: Density
  /** Sidebar and header hidden; the page fills the screen (NOC wall). */
  kiosk: boolean
  setDensity: (d: Density) => void
  setKiosk: (on: boolean) => void
}

export const useDisplay = create<DisplayState>()((set) => ({
  density: initialDensity(),
  kiosk: initialKiosk(),
  setDensity: (d) => {
    applyDensity(d)
    try { localStorage.setItem(KEY, d) } catch { /* private mode */ }
    set({ density: d })
  },
  setKiosk: (on) => {
    set({ kiosk: on })
    // Full screen is best effort: browsers only allow it from a click, and a wall browser may already be full screen.
    try {
      if (on && !document.fullscreenElement) void document.documentElement.requestFullscreen?.().catch(() => {})
      if (!on && document.fullscreenElement) void document.exitFullscreen?.().catch(() => {})
    } catch { /* unsupported */ }
  },
}))

applyDensity(useDisplay.getState().density)
