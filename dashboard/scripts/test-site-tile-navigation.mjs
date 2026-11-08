import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { createRequire } from 'node:module'
import { build } from 'esbuild'
import { JSDOM } from 'jsdom'

// Render the actual component and exercise bubbling events. These controls
// share a clickable card, so checking handler source alone misses regressions.
const dom = new JSDOM('<div id="root"></div>', { url: 'https://zenplus.test' })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.HTMLElement = dom.window.HTMLElement
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const React = await import('react')
const { createRoot } = await import('react-dom/client')
const { act } = React
const dir = fs.mkdtempSync(path.join(process.cwd(), '.site-tile-test-'))
const root = createRoot(document.getElementById('root'))

try {
  const outfile = path.join(dir, 'SiteTile.cjs')
  await build({
    entryPoints: ['src/features/maps/canvas/SiteTile.tsx'], outfile,
    bundle: true, format: 'cjs', platform: 'node', jsx: 'automatic',
    external: ['react', 'react/jsx-runtime'],
    alias: { '@': path.resolve('src') },
    define: { 'process.env.NODE_ENV': '"test"' },
  })
  const { SiteTile } = createRequire(import.meta.url)(outfile)
  const shape = {
    id: 'site', kind: 'rectangle', text: 'Al Aridh',
    metadata: { widget: 'site', site: { map_id: 'detail', expanded: false } },
  }
  const member = { kind: 'device', id: 'firewall', hostname: 'Gateway', ip_address: '192.0.2.1', status: 'up', alerts: { active: 0, critical: 0, warning: 0 } }
  const live = {
    total: 1, counts: { up: 1, down: 0, degraded: 0, unknown: 0, maintenance: 0 },
    alerts: { active: 0, critical: 0, warning: 0 }, members: [member], worst: 'up',
    map_id: 'detail', map_name: 'Al Aridh detail',
  }
  let drills = [], members = [], ancestorClicks = 0
  const render = async (extra = {}) => {
    await act(async () => root.render(React.createElement('div', { onClick: () => ancestorClicks++ },
      React.createElement(SiteTile, { key: JSON.stringify(extra), shape, live, w: 400, h: 180,
        designMode: false, onDrill: id => drills.push(id), onOpenMember: m => members.push(m.id), ...extra }))))
  }
  const click = async (el) => {
    assert(el, 'The target must exist')
    await act(async () => el.dispatchEvent(new window.MouseEvent('click', { bubbles: true })))
  }
  const key = async (el, value) => {
    const e = new window.KeyboardEvent('keydown', { key: value, bubbles: true, cancelable: true })
    await act(async () => el.dispatchEvent(e))
    return e
  }
  const button = label => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === label)
  const reset = () => { drills = []; members = []; ancestorClicks = 0 }

  await render()
  const card = document.querySelector('[role="link"]')
  assert.equal(card.getAttribute('aria-label'), 'Open Al Aridh site map')
  assert.equal(card.tabIndex, 0)
  for (const target of [card, card.querySelector('[title="Al Aridh"]'), card.querySelector('svg')]) {
    reset(); await click(target)
    assert.deepEqual(drills, ['detail'], 'Card, title and health ring each navigate exactly once')
    assert.equal(ancestorClicks, 0, 'Navigation does not bubble into the canvas')
  }

  reset(); await click(button('Open'))
  assert.deepEqual(drills, ['detail'], 'Existing Open button still navigates once')
  reset(); await click(button('Devices'))
  assert.deepEqual(drills, [], 'Devices expands without opening the map')
  assert(button('Hide'))
  const memberCell = document.querySelector('button.nm-site-cell')
  assert(memberCell, 'Expanded device grid is present')
  await click(memberCell)
  assert.deepEqual(members, ['firewall'], 'Member retains its own device action')
  assert.deepEqual(drills, [], 'Member click does not open the site')
  await click(button('Hide'))
  assert(button('Devices'))
  assert.equal(document.querySelector('button.nm-site-cell'), null)
  assert.deepEqual(drills, [])

  reset()
  assert((await key(card, 'Enter')).defaultPrevented)
  assert((await key(card, ' ')).defaultPrevented)
  assert(!(await key(card, 'Escape')).defaultPrevented)
  assert.deepEqual(drills, ['detail', 'detail'], 'Keyboard activation follows the clickable tile')
  reset(); await key(button('Devices'), 'Enter'); await key(button('Open'), ' ')
  assert.deepEqual(drills, [], 'Child keyboard events never activate the parent tile')

  const problem = { ...member, status: 'down', alerts: { active: 1, critical: 1, warning: 0 } }
  await render({ live: { ...live, members: [problem] } })
  reset(); await click(button('Devices'))
  await click([...document.querySelectorAll('button')].find(b => b.textContent.includes('Gateway')))
  assert.deepEqual(members, ['firewall'], 'Problem device keeps its independent action')
  assert.deepEqual(drills, [])

  await render({ designMode: true })
  reset()
  assert.equal(document.querySelector('[role="link"]'), null)
  await click(document.querySelector('[title="Al Aridh"]'))
  assert.deepEqual(drills, [], 'Design-mode tile clicks do not navigate')
  await click(button('Devices'))
  assert(button('Hide'), 'Design-mode grid toggle still works')
  assert.deepEqual(drills, [])
  await click(button('Open'))
  assert.deepEqual(drills, ['detail'], 'Existing explicit Open action is retained in Design mode')

  for (const extra of [{ live: { ...live, map_id: null } }, { live: undefined }, { onDrill: undefined }]) {
    await render(extra); reset()
    assert.equal(document.querySelector('[role="link"]'), null)
    await click(document.querySelector('[title="Al Aridh"]'))
    assert.deepEqual(drills, [], 'Missing or inaccessible map stays inert, even if shape metadata names a map')
  }
  console.log('Site tile navigation passed: card/title/ring, single dispatch, keyboard, Open, Devices, member actions, Design mode and inaccessible maps.')
} finally {
  await act(async () => root.unmount())
  dom.window.close()
  fs.rmSync(dir, { recursive: true, force: true })
}
