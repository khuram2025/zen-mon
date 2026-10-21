// Network map export/import formats (src/features/maps/export/mapFormats.ts):
//   (a) draw.io XML: well-formed, one vertex per node/shape, edges reference existing cells, interface labels,
//       dashed vpn/wireless, amber fiber, escaping, percent → 4000×2400 coordinates, dangling cables skipped,
//   (b) JSON backup round trip + parseMapJson's readable errors (also accepts a raw version snapshot),
//   (c) toBulkPayload: client ids = backup ids, nodes whose device/server/check is gone are dropped with their
//       links and annotation links, positions clamped to the API range, annotation_links leave map metadata.
// Run from dashboard/: node scripts/test-map-export.mjs
import assert from 'node:assert/strict'
import fs from 'node:fs'
import { transformWithEsbuild } from 'vite'

const file = 'src/features/maps/export/mapFormats.ts'
const source = fs.readFileSync(file, 'utf8')
assert(!/^import (?!type )/m.test(source), 'mapFormats.ts imports types only, so it runs without the app')
const compiled = await transformWithEsbuild(source, 'mapFormats.ts', { loader: 'ts', format: 'esm' })
const F = await import(`data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`)

// The duplicated canvas constants must match core.ts.
const core = fs.readFileSync('src/features/maps/core.ts', 'utf8')
assert.equal(core.match(/export const LOGICAL_W = (\d+)/)[1], source.match(/const CANVAS_W = (\d+)/)[1])
assert.equal(core.match(/export const LOGICAL_H = (\d+)/)[1], source.match(/const CANVAS_H = (\d+)/)[1])

/* ── fixture ─────────────────────────────────────────────────────────────── */
const node = (id, extra) => ({
  id, map_id: 'm1', kind: 'device', device_id: `dev-${id}`, server_id: null, service_check_id: null,
  label: '', icon: 'auto', x_pct: 50, y_pct: 50, hostname: `host-${id}`, ip_address: `10.0.0.${id.length}`,
  device_type: 'switch', status: 'up', metadata: null, ...extra,
})
const detail = {
  id: 'm1', name: 'Al Aridh <Core> & "DC"', description: 'test', node_count: 4, link_count: 3, status_counts: {},
  summary: { nodes: 4, links: 3, status_counts: {}, generated_at: '2026-10-21T00:00:00Z' },
  nodes: [
    node('a', { label: 'FW-01 & edge', device_type: 'firewall', status: 'down', x_pct: 50, y_pct: 50 }),
    node('b', { x_pct: 25, y_pct: 75, status: 'degraded' }),
    node('c', { x_pct: 120, y_pct: -5 }), // out of range on purpose → clamped on import
    { ...node('srv'), kind: 'server', device_id: null, server_id: 'server-1', hostname: 'esx-01', device_type: 'server' },
  ],
  links: [
    { id: 'l1', map_id: 'm1', source_node_id: 'a', target_node_id: 'b', label: null, link_type: 'fiber',
      metadata: { kind: 'fiber', src_interface: 'Gi0/1', dst_interface: 'Te1/1', shape: 'orthogonal',
        waypoints: [{ x_pct: 10, y_pct: 20 }], members: [{ src_interface: 'Gi0/1', dst_interface: 'Te1/1' }, { src_interface: 'Gi0/2', dst_interface: 'Te1/2' }] } },
    { id: 'l2', map_id: 'm1', source_node_id: 'b', target_node_id: 'c', label: 'IPsec', link_type: 'vpn', metadata: { kind: 'vpn' } },
    { id: 'l3', map_id: 'm1', source_node_id: 'b', target_node_id: 'ghost', label: null, link_type: 'ethernet', metadata: null },
  ],
  shapes: [
    { id: 's-zone', map_id: 'm1', kind: 'rectangle', x_pct: 5, y_pct: 5, w_pct: 40, h_pct: 30, text: 'Zone A', fill: 'rgba(59,130,246,0.06)',
      stroke: '#3b82f6', z_index: 0, metadata: { rounded: true, dash: 'dashed', align: 'left', fontSize: 22, bold: true } },
    { id: 's-icon', map_id: 'm1', kind: 'image', x_pct: 60, y_pct: 60, w_pct: 3, h_pct: 5, text: null, fill: null, stroke: null, z_index: 2,
      metadata: { icon: 'switch', unmanaged: { hostname: 'SEP-unmanaged', chassis_id: 'aa:bb', port_id: 'Gi1/0/48' } } },
    { id: 's-line', map_id: 'm1', kind: 'arrow', x_pct: 70, y_pct: 10, w_pct: 10, h_pct: 2, text: null, fill: null, stroke: '#ff0000', z_index: 1, metadata: {} },
  ],
  metadata: {
    background: 'grid',
    annotation_links: [
      { id: 'al1', source: 'a', target: 's-icon', source_type: 'node', target_type: 'shape', label: null, link_type: 'wireless',
        metadata: { kind: 'wireless', src_interface: 'Gi0/48', dst_interface: 'Gi1/0/48' } },
      { id: 'al2', source: 'c', target: 's-icon', source_type: 'node', target_type: 'shape', metadata: {} },
      { id: 'al3', source: 'missing', target: 's-icon', source_type: 'shape', target_type: 'shape', metadata: {} },
    ],
  },
}

/* ── (a) draw.io ─────────────────────────────────────────────────────────── */
function assertWellFormed(xml) {
  const body = xml.replace(/^<\?xml[^>]*\?>/, '')
  const stack = []
  for (const m of body.matchAll(/<(\/?)([A-Za-z][\w:-]*)((?:\s+[\w:-]+="[^"<]*")*)\s*(\/?)>/g)) {
    const [, close, name, , self] = m
    if (self) continue
    if (close) assert.equal(stack.pop(), name, `mismatched </${name}>`)
    else stack.push(name)
  }
  assert.equal(stack.length, 0, `unclosed tags: ${stack.join(',')}`)
  // No raw specials outside tags / inside attributes.
  const stripped = body.replace(/<[^>]*>/g, '')
  assert(!/[<>]/.test(stripped), 'stray angle bracket in text')
  for (const a of body.matchAll(/="([^"]*)"/g)) assert(!/&(?!amp;|lt;|gt;|quot;|apos;|#xa;)/.test(a[1]), `unescaped & in ${a[1]}`)
}
const attrs = (tag) => Object.fromEntries([...tag.matchAll(/([\w:-]+)="([^"]*)"/g)].map((m) => [m[1], m[2]]))
{
  const xml = F.buildDrawioXml(detail)
  assertWellFormed(xml)
  assert.match(xml, /^<\?xml version="1.0" encoding="UTF-8"\?>\n<mxfile /)
  assert.match(xml, /<diagram id="m1" name="Al Aridh &lt;Core&gt; &amp; &quot;DC&quot;">/)
  const cells = [...xml.matchAll(/<mxCell [^>]*>/g)].map((m) => attrs(m[0]))
  const ids = new Set(cells.map((c) => c.id))
  assert.equal(ids.size, cells.length, 'cell ids are unique')
  const vertices = cells.filter((c) => c.vertex === '1')
  const edges = cells.filter((c) => c.edge === '1')
  assert.equal(vertices.length, 4 + 2, '4 node vertices + zone + icon (the arrow is an edge)')
  // l1, l2, al1, al2 + the free arrow; l3 (ghost endpoint) and al3 (missing shape) skipped
  assert.equal(edges.length, 5)
  for (const e of edges.filter((x) => x.source)) {
    assert(ids.has(e.source) && ids.has(e.target), `edge ${e.id} references existing cells`)
  }
  assert(!ids.has('l-l3') && !ids.has('a-al3'))

  const fw = cells.find((c) => c.id === 'n-a')
  assert.equal(fw.value, 'FW-01 &amp; edge&#xa;10.0.0.1')
  assert.match(fw.style, /fillColor=#f8cecc/, 'down → red fill')
  assert.match(cells.find((c) => c.id === 'n-b').style, /fillColor=#fff2cc/, 'degraded → amber fill')
  // Node at 50%/50% → centre (2000,1200) with a 140×56 box.
  assert.match(xml, /<mxCell id="n-a"[^>]*><mxGeometry x="1930" y="1172" width="140" height="56" as="geometry" \/>/)

  const l1 = cells.find((c) => c.id === 'l-l1')
  assert.equal(l1.value, 'Gi0/1 ⇄ Te1/1 (×2)')
  assert.match(l1.style, /strokeColor=#f59e0b/, 'fiber is amber')
  assert.match(l1.style, /edgeStyle=orthogonalEdgeStyle/)
  assert.doesNotMatch(l1.style, /dashed=1/)
  assert.match(xml, /<mxCell id="l-l1"[^>]*><mxGeometry relative="1" as="geometry"><Array as="points"><mxPoint x="400" y="480" \/><\/Array>/)
  const l2 = cells.find((c) => c.id === 'l-l2')
  assert.equal(l2.value, 'IPsec')
  assert.match(l2.style, /dashed=1/, 'vpn is dashed')
  assert.match(cells.find((c) => c.id === 'a-al1').style, /dashed=1/, 'wireless is dashed')
  assert.equal(cells.find((c) => c.id === 'a-al1').value, 'Gi0/48 ⇄ Gi1/0/48')

  const zone = cells.find((c) => c.id === 's-s-zone')
  assert.match(zone.style, /fillColor=#3b82f6;fillOpacity=6/)
  assert.match(zone.style, /dashed=1/)
  assert.match(zone.style, /verticalAlign=top/)
  assert.match(zone.style, /fontStyle=1/)
  assert.match(xml, /<mxCell id="s-s-zone"[^>]*><mxGeometry x="200" y="120" width="1600" height="720"/)
  assert.equal(cells.find((c) => c.id === 's-s-icon').value, 'SEP-unmanaged', 'unmanaged neighbour caption')
  // Zone (lowest z) is emitted before cables, devices after cables.
  assert(xml.indexOf('id="s-s-zone"') < xml.indexOf('id="l-l1"'))
  assert(xml.indexOf('id="l-l1"') < xml.indexOf('id="n-a"'))
}

// Colour conversion
assert.deepEqual(F.toDrawioColor('#abc'), { hex: '#aabbcc', opacity: 100 })
assert.deepEqual(F.toDrawioColor('#11223380'), { hex: '#112233', opacity: 50 })
assert.deepEqual(F.toDrawioColor('rgba(255, 0, 10, 0.25)'), { hex: '#ff000a', opacity: 25 })
assert.deepEqual(F.toDrawioColor('rgb(1 2 3)'), { hex: '#010203', opacity: 100 })
assert.equal(F.toDrawioColor('transparent'), null)
assert.equal(F.toDrawioColor('var(--x)'), null)
assert.equal(F.xmlEscape(`a&b<c>"d"\n'e'`), 'a&amp;b&lt;c&gt;&quot;d&quot;&#xa;&apos;e&apos;')
assert.equal(F.slugify('Al Aridh — Core/DC #1'), 'al-aridh-core-dc-1')
assert.equal(F.slugify('موقع العارض'), 'موقع-العارض')
assert.equal(F.slugify('***'), 'map')
assert.match(F.exportFileBase('Site A', new Date(2026, 9, 21, 7, 5)), /^zenplus-map-site-a-20261021-0705$/)

/* ── (b) JSON backup ─────────────────────────────────────────────────────── */
const backup = F.buildMapJson(detail, new Date('2026-10-21T10:00:00Z'))
assert.equal(backup.format, 'zenplus-map')
assert.equal(backup.version, 1)
assert.equal(backup.exported_at, '2026-10-21T10:00:00.000Z')
assert.equal(backup.map.name, detail.name)
assert.equal(backup.map.metadata.annotation_links.length, 3)
assert.equal(backup.nodes.length, 4)
assert.equal(backup.nodes[3].kind, 'server')
assert.equal(backup.nodes[3].server_id, 'server-1')
assert.equal(backup.nodes[3].device_id, null)
assert(!('status' in backup.nodes[0]), 'live status is not part of a backup')
const reparsed = F.parseMapJson(JSON.stringify(backup))
assert.deepEqual(reparsed, backup, 'parse(build(x)) round-trips')

// Legacy nodes without kind are devices.
{
  const legacy = JSON.parse(JSON.stringify(backup))
  delete legacy.nodes[0].kind
  assert.equal(F.parseMapJson(JSON.stringify(legacy)).nodes[0].kind, 'device')
}
// A raw version snapshot is accepted too.
{
  const snap = { name: 'Snap', description: null, metadata: {}, nodes: [node('z')], links: [], shapes: [] }
  const p = F.parseMapJson(JSON.stringify(snap))
  assert.equal(p.map.name, 'Snap')
  assert.equal(p.nodes[0].device_id, 'dev-z')
}
const bad = (obj, re) => assert.throws(() => F.parseMapJson(typeof obj === 'string' ? obj : JSON.stringify(obj)), re)
bad('{not json', /not valid JSON/)
bad('[1,2]', /does not contain a map backup/)
bad({ format: 'other', version: 1, map: {} }, /not a ZenPlus map backup/)
bad({ ...backup, version: 2 }, /newer ZenPlus/)
bad({ ...backup, version: 'x' }, /no valid version/)
bad({ ...backup, map: null }, /no "map" section/)
bad({ ...backup, nodes: {} }, /"nodes" must be a list/)
bad({ ...backup, nodes: [{ ...backup.nodes[0], id: '' }] }, /Node 1 has no id/)
bad({ ...backup, nodes: [backup.nodes[0], backup.nodes[0]] }, /Node 2 repeats id a/)
bad({ ...backup, nodes: [{ ...backup.nodes[0], x_pct: '5' }] }, /Node 1 \(a\) has no numeric x_pct/)
bad({ ...backup, nodes: [{ ...backup.nodes[0], device_id: null }] }, /Node 1 \(a\) has no device_id/)
bad({ ...backup, nodes: [{ ...backup.nodes[3], server_id: null }] }, /has no server_id/)
bad({ ...backup, shapes: [{ ...backup.shapes[0], w_pct: null }] }, /Shape 1 \(s-zone\) has no numeric w_pct/)
bad({ ...backup, links: [{ id: 'x', source_node_id: 'a' }] }, /Link 1 needs source_node_id and target_node_id/)
bad({ ...backup, map: { ...backup.map, metadata: { annotation_links: {} } } }, /annotation_links" must be a list/)

/* ── (c) bulk payload ────────────────────────────────────────────────────── */
{
  // dev-b is gone from this appliance; the server is present.
  const existing = new Set(['dev-a', 'dev-c', 'server-1'])
  const r = F.toBulkPayload(reparsed, existing)
  assert.deepEqual(r.dropped, [{ id: 'b', kind: 'device', entity_id: 'dev-b', name: 'host-b' }])
  assert.deepEqual(r.payload.nodes.map((n) => n.client_id), ['a', 'c', 'srv'])
  assert.deepEqual(r.payload.nodes[0], {
    client_id: 'a', device_id: 'dev-a', label: 'FW-01 & edge', icon: 'auto', x_pct: 50, y_pct: 50, metadata: null,
  })
  const c = r.payload.nodes.find((n) => n.client_id === 'c')
  assert.equal(c.x_pct, 98, 'clamped to the API range')
  assert.equal(c.y_pct, 2)
  const srv = r.payload.nodes.find((n) => n.client_id === 'srv')
  assert.equal(srv.server_id, 'server-1')
  assert(!('device_id' in srv), 'exactly one entity id per node')
  // l1 (a–b) and l2 (b–c) touch the dropped node, l3 points at a node not in the backup.
  assert.deepEqual(r.payload.links, [])
  assert.deepEqual(r.payload.shapes.map((s) => s.client_id), ['s-zone', 's-icon', 's-line'])
  assert.equal(r.payload.shapes[0].fill, 'rgba(59,130,246,0.06)')
  // al1 (a → icon) and al2 (c → icon) survive, al3 (missing shape) is dropped. Ids are left to the server.
  assert.deepEqual(r.payload.annotation_links, [
    { source: 'a', target: 's-icon', source_type: 'node', target_type: 'shape', link_type: 'wireless',
      metadata: { kind: 'wireless', src_interface: 'Gi0/48', dst_interface: 'Gi1/0/48' } },
    { source: 'c', target: 's-icon', source_type: 'node', target_type: 'shape', metadata: {} },
  ])
  assert.equal(r.droppedLinks, 3 + 1)
  assert.deepEqual(r.mapMetadata, { background: 'grid' }, 'annotation links travel in the payload, not the metadata')
  assert.equal(reparsed.map.metadata.annotation_links.length, 3, 'the backup itself is not mutated')
}
{
  // Everything present → all device links kept with node ids as endpoints.
  const r = F.toBulkPayload(reparsed, new Set(['dev-a', 'dev-b', 'dev-c', 'server-1']))
  assert.equal(r.dropped.length, 0)
  assert.deepEqual(r.payload.links.map((l) => [l.source, l.target, l.link_type]), [['a', 'b', 'fiber'], ['b', 'c', 'vpn']])
  assert.equal(r.payload.links[0].metadata.members.length, 2)
  assert.equal(r.droppedLinks, 2, 'l3 (ghost) + al3 (missing shape)')
}
{
  // Per-kind form: an omitted kind is not checked; a given set is.
  const r = F.toBulkPayload(reparsed, { devices: new Set(['dev-a', 'dev-b', 'dev-c']) })
  assert.equal(r.dropped.length, 0, 'servers unchecked when no server set is given')
  const r2 = F.toBulkPayload(reparsed, { devices: new Set(['dev-a', 'dev-b', 'dev-c']), servers: new Set() })
  assert.deepEqual(r2.dropped.map((d) => d.id), ['srv'])
}

console.log('map export formats: ok')
