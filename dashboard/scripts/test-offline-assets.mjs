// Offline appliances: the dashboard must load everything from the appliance itself. Many customer appliances have
// no internet access, so a CDN stylesheet, script, font or data file silently degrades the UI there (the Inter font
// from a CDN fell back to system fonts). Links a person clicks (vendor docs, the knowledge base) are fine.
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

const root = process.cwd()
const read = (rel) => fs.readFileSync(path.join(root, rel), 'utf8')
const EXTERNAL = /(?:https?:)?\/\/(?!localhost|127\.0\.0\.1)[a-z0-9.-]+\.[a-z]{2,}/i

// index.html: no external stylesheet, script, preconnect or font.
const html = read('index.html')
for (const m of html.matchAll(/<(link|script)\b[^>]*>/gi)) {
  const tag = m[0]
  const url = (tag.match(/\b(?:href|src)=["']([^"']+)["']/i) || [])[1] || ''
  assert(!EXTERNAL.test(url), `index.html loads ${url} from the internet: ${tag}`)
}

// Source: no external fetches, stylesheet imports, font faces, scripts, images or frames.
const files = []
const walk = (dir) => {
  for (const e of fs.readdirSync(path.join(root, dir), { withFileTypes: true })) {
    const rel = path.join(dir, e.name)
    if (e.isDirectory()) walk(rel)
    else if (/\.(tsx?|css)$/.test(e.name)) files.push(rel)
  }
}
walk('src')
const RUNTIME_LOADS = [
  /@import\s+(?:url\()?["']?https?:\/\//i,
  /url\(\s*["']?https?:\/\//i,
  /\bfetch\(\s*[`'"]https?:\/\//i,
  /\bimport\(\s*[`'"]https?:\/\//i,
  /<(?:script|link|img|iframe|source|video|audio)\b[^>]*\b(?:src|href)=["'{`]+https?:\/\//i,
  /\.src\s*=\s*[`'"]https?:\/\//i,
]
const offenders = []
for (const rel of files) {
  const src = read(rel)
  for (const re of RUNTIME_LOADS) {
    const m = src.match(re)
    if (m) offenders.push(`${rel}: ${m[0]}`)
  }
}
assert.deepEqual(offenders, [], `assets loaded from the internet:\n  ${offenders.join('\n  ')}`)

// The UI fonts ship with the dashboard.
const main = read('src/main.tsx')
assert(main.includes("import '@fontsource-variable/inter'"), 'Inter is bundled (main.tsx)')
assert(main.includes("import '@fontsource-variable/jetbrains-mono'"), 'JetBrains Mono is bundled (main.tsx)')
const tw = read('tailwind.config.js')
assert(/sans:\s*\['"Inter Variable"'/.test(tw), 'the sans stack starts with the bundled Inter Variable')
assert(!/rsms\.me|fonts\.googleapis|fonts\.gstatic|cdnjs|jsdelivr|unpkg/i.test(html + read('src/styles/globals.css') + read('src/styles/fonts.css')), 'no font or library CDN')

// A built bundle (when present) references no external stylesheet, script or font either.
const dist = path.join(root, 'dist')
if (fs.existsSync(path.join(dist, 'index.html'))) {
  const built = fs.readFileSync(path.join(dist, 'index.html'), 'utf8')
  for (const m of built.matchAll(/<(link|script)\b[^>]*>/gi)) {
    const url = (m[0].match(/\b(?:href|src)=["']([^"']+)["']/i) || [])[1] || ''
    assert(!EXTERNAL.test(url), `dist/index.html loads ${url}`)
  }
  for (const f of fs.readdirSync(path.join(dist, 'assets')).filter((n) => n.endsWith('.css'))) {
    const css = fs.readFileSync(path.join(dist, 'assets', f), 'utf8')
    assert(!/@import\s+(?:url\()?["']?https?:|url\(\s*["']?https?:/i.test(css), `dist/assets/${f} loads from the internet`)
  }
  assert(fs.readdirSync(path.join(dist, 'assets')).some((n) => /^inter-latin-wght-normal.*\.woff2$/.test(n)), 'the Inter font file is in the bundle')
}

console.log(`Offline assets: index.html, ${files.length} source files${fs.existsSync(dist) ? ' and the built bundle' : ''} load nothing from the internet; Inter and JetBrains Mono are bundled.`)
