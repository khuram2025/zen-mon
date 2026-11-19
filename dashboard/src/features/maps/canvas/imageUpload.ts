/* Image uploads for map icons and backgrounds. Images are stored inside the
 * map (as data URLs) so a map exports, versions and restores with its art and
 * the appliance never fetches anything from outside. Raster images are scaled
 * down in the browser first so a phone photo doesn't bloat the map. */

export type ImageKind = 'icon' | 'background'

const LIMITS: Record<ImageKind, { maxPx: number; maxInputBytes: number; maxOutputBytes: number }> = {
  // Device icons are drawn at most ~140 px on screen.
  icon: { maxPx: 256, maxInputBytes: 4 * 1024 * 1024, maxOutputBytes: 300 * 1024 },
  // Floor plans and site photos: enough for a 4K wall.
  background: { maxPx: 3200, maxInputBytes: 15 * 1024 * 1024, maxOutputBytes: 2.5 * 1024 * 1024 },
}

const ACCEPT = ['image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/svg+xml']
export const IMAGE_ACCEPT = ACCEPT.join(',')

function readAsDataUrl(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader()
    r.onload = () => resolve(String(r.result))
    r.onerror = () => reject(new Error('Could not read the file'))
    r.readAsDataURL(file)
  })
}

function loadImage(src: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image()
    img.onload = () => resolve(img)
    img.onerror = () => reject(new Error('The file is not a readable image'))
    img.src = src
  })
}

/** Read an uploaded image as a (possibly down-scaled) data URL. SVG is kept as
 *  vector when it is small enough; everything else is re-encoded (PNG for
 *  icons to keep transparency, JPEG/WebP for large backgrounds). */
export async function readImageFile(file: File, kind: ImageKind): Promise<string> {
  const lim = LIMITS[kind]
  if (!ACCEPT.includes(file.type)) throw new Error('Use a PNG, JPEG, WebP, GIF or SVG image')
  if (file.size > lim.maxInputBytes) throw new Error(`Image too large (max ${Math.round(lim.maxInputBytes / 1024 / 1024)} MB)`)
  const raw = await readAsDataUrl(file)
  if (file.type === 'image/svg+xml') {
    if (raw.length > lim.maxOutputBytes) throw new Error('SVG too large — simplify it or export it as PNG')
    return raw
  }
  const img = await loadImage(raw)
  const scale = Math.min(1, lim.maxPx / Math.max(img.naturalWidth || 1, img.naturalHeight || 1))
  const w = Math.max(1, Math.round(img.naturalWidth * scale))
  const h = Math.max(1, Math.round(img.naturalHeight * scale))
  const canvas = document.createElement('canvas')
  canvas.width = w
  canvas.height = h
  const ctx = canvas.getContext('2d')
  if (!ctx) return raw
  ctx.drawImage(img, 0, 0, w, h)
  const candidates = kind === 'icon' ? ['image/png', 'image/webp'] : ['image/webp', 'image/jpeg']
  for (const type of candidates) {
    for (const q of [0.9, 0.8, 0.7]) {
      const out = canvas.toDataURL(type, q)
      if (out.startsWith(`data:${type}`) && out.length <= lim.maxOutputBytes) return out
    }
  }
  throw new Error('Image is still too large after scaling — use a smaller image')
}

/** Ask the user for an image file. Resolves null when they cancel. */
export function pickImageFile(): Promise<File | null> {
  return new Promise((resolve) => {
    const input = document.createElement('input')
    input.type = 'file'
    input.accept = IMAGE_ACCEPT
    input.onchange = () => resolve(input.files?.[0] || null)
    input.click()
  })
}

/** Natural aspect ratio (height / width) of an image source, 0.6 if unknown. */
export async function imageAspect(src: string): Promise<number> {
  try {
    const img = await loadImage(src)
    return img.naturalWidth ? img.naturalHeight / img.naturalWidth : 0.6
  } catch {
    return 0.6
  }
}
