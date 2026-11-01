import assert from 'node:assert/strict'
import { createRequire } from 'node:module'
import path from 'node:path'
import { pathToFileURL } from 'node:url'
import { build } from 'esbuild'
import { JSDOM } from 'jsdom'
import React, { act, useState } from 'react'

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', { url: 'http://localhost' })
globalThis.window = dom.window
globalThis.document = dom.window.document
globalThis.HTMLElement = dom.window.HTMLElement
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator })
globalThis.IS_REACT_ACT_ENVIRONMENT = true
const { createRoot } = await import('react-dom/client')
const { flushSync } = await import('react-dom')
const require = createRequire(import.meta.url)
const source = path.join(process.cwd(), 'src')
const compiled = await build({
  entryPoints: ['src/components/forms/AlertWizardFooter.tsx'], bundle: true, write: false,
  format: 'esm', platform: 'browser', jsx: 'automatic', logLevel: 'silent',
  plugins: [{ name: 'test-resolution', setup(b) {
    b.onResolve({ filter: /^react(?:\/.*)?$/ }, a => ({ path: pathToFileURL(require.resolve(a.path)).href, external: true }))
    b.onResolve({ filter: /^@\// }, a => b.resolve(`./${a.path.slice(2)}`, { resolveDir: source, kind: a.kind }))
  } }],
})
const { AlertWizardFooter, isExplicitWizardSave } = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`)
const root = createRoot(document.getElementById('root'))
let writes = 0
let closed = 0
function Harness({ initialStep = 7, editing = true, pending = false }) {
  const [step, setStep] = useState(initialStep)
  return React.createElement('form', { onSubmit(e) {
    e.preventDefault()
    if (isExplicitWizardSave(step, 9, pending, e.nativeEvent.submitter)) writes++
  } }, React.createElement('h2', null, step === 8 ? 'Review' : `Step ${step + 1}`),
  React.createElement(AlertWizardFooter, { stepIndex: step, stepCount: 9, editing, pending,
    onPrevious: () => flushSync(() => step ? setStep(step - 1) : closed++),
    // Discrete React updates may flush before the browser completes activation.
    // The clicked Next node must not become a submit button during that click.
    onNext: () => flushSync(() => setStep(step + 1)),
  }))
}
const button = text => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === text)
try {
  await act(async () => root.render(React.createElement(Harness)))
  await act(async () => button('Next').click())
  assert.equal(document.querySelector('h2').textContent, 'Review')
  assert.equal(writes, 0, 'Next to Review must not save, even if React flushes during button activation')
  assert(button('Save alert'), 'Review keeps an explicit Save alert action visible')
  await act(async () => button('Back').click())
  assert.equal(writes, 0, 'Back never submits')
  await act(async () => button('Next').click())
  await act(async () => button('Save alert').click())
  assert.equal(writes, 1, 'Only an explicit Save submits')
  await act(async () => root.render(React.createElement(Harness, { key: 'pending', initialStep: 8, pending: true })))
  await act(async () => button('Back').click())
  assert.equal(document.querySelector('h2').textContent, 'Review', 'Pending saves cannot navigate to another draft')
  await act(async () => button('Save alert').click())
  assert.equal(writes, 1, 'A pending save cannot submit twice')
  await act(async () => root.render(React.createElement(Harness, { key: 'create', initialStep: 7, editing: false })))
  await act(async () => button('Next').click())
  assert.equal(writes, 1, 'Creating a rule also waits for review')
  await act(async () => button('Create alert').click())
  assert.equal(writes, 2)
  await act(async () => root.render(React.createElement(Harness, { key: 'first', initialStep: 0 })))
  await act(async () => document.querySelector('form').requestSubmit())
  assert.equal(writes, 2, 'Implicit submission before Review never saves')
  await act(async () => button('Cancel').click())
  assert.equal(closed, 1)
  const save = document.createElement('button')
  save.setAttribute('data-alert-save', 'true')
  assert.equal(isExplicitWizardSave(0, 9, false, save), false)
  assert.equal(isExplicitWizardSave(8, 9, true, save), false)
  assert.equal(isExplicitWizardSave(8, 9, false, null), false)
  assert.equal(isExplicitWizardSave(8, 9, false, save), true)
  console.log('Alert wizard navigation: Next and Back never submit; Review retains explicit Save/Create; implicit and duplicate submissions blocked.')
} finally {
  await act(async () => root.unmount())
  dom.window.close()
}
