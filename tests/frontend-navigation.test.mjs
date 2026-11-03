import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
function implementation(file, name) {
  const text = readFileSync(new URL('../frontend/src/' + file, import.meta.url), 'utf8').split('</script>')[0]
  const parsed = ts.createSourceFile(file + '.ts', text.slice(text.indexOf('>') + 1), ts.ScriptTarget.Latest, true)
  const fn = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name)
  assert.ok(fn, name)
  return ts.transpileModule(fn.getText(parsed), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
}
const goCode = implementation('PublicApp.vue', 'go')
function navigation(from, view, allowed) {
  const ref = value => ({ value })
  const state = {
    route: ref(from), view: ref(view), editor: ref(null), preferences: ref({}), preferencesBaseline: ref('{}'),
    systemSettings: ref({ guard: async () => allowed }), core: ref({ guard: async () => allowed }),
    workbench: ref({ guard: async () => allowed }), workbenchViews: new Set(['journals', 'experiments']),
    navigationOpen: ref(true), error: ref('old'), notice: ref('old'), form: ref({ password: 'temporary' }),
    location: { hash: '#' + from }, askConfirm: async () => allowed, loads: 0, closed: 0,
  }
  state.closeNavigation = () => { state.navigationOpen.value = false; state.closed++ }
  state.loadView = async () => { state.loads++ }
  vm.runInNewContext(goCode + '\nglobalThis.go = go', state)
  return state
}

test('shared navigation preserves drafts and the open drawer when leaving is cancelled', async () => {
  for (const [from, view] of [['watchlist', 'workspace'], ['journals', 'journals'], ['system', 'system'], ['preferences', 'preferences'], ['models', 'models']]) {
    const s = navigation(from, view, false)
    if (from === 'preferences') s.preferences.value.budget = 900
    if (from === 'models') s.editor.value = { name: 'draft' }
    await s.go('jobs')
    assert.equal(s.route.value, from)
    assert.equal(s.location.hash, '#' + from)
    assert.equal(s.navigationOpen.value, true)
    assert.equal(s.loads, 0)
  }
})

test('accepted navigation closes the drawer and retains the destination deep link', async () => {
  const s = navigation('journals', 'journals', true)
  await s.go('experiments?experiment=archive-1')
  assert.equal(s.route.value, 'experiments')
  assert.equal(s.location.hash, 'experiments?experiment=archive-1')
  assert.equal(s.navigationOpen.value, false)
  assert.equal(s.closed, 1)
  assert.equal(s.loads, 1)
  assert.equal(s.form.value.password, '')
})

const keyboardCode = implementation('components/WorkspaceNav.vue', 'keyboard')
test('mobile drawer keyboard loop includes the brand link and skips hidden controls', () => {
  let focused = ''
  const element = (name, visible = true) => ({ focus() { focused = name }, getClientRects() { return visible ? [{}] : [] } })
  const brand = element('brand'), hidden = element('hidden', false), last = element('last')
  const state = {
    props: { open: true }, navigation: { value: { querySelectorAll: () => [brand, hidden, last] } },
    document: { activeElement: last }, emitted: [],
  }
  state.emit = event => state.emitted.push(event)
  vm.runInNewContext(keyboardCode + '\nglobalThis.keyboard = keyboard', state)
  let prevented = false
  const event = { key: 'Tab', shiftKey: false, preventDefault() { prevented = true } }
  state.keyboard(event)
  assert.equal(focused, 'brand')
  assert.equal(prevented, true)
  state.document.activeElement = brand
  state.keyboard({ ...event, shiftKey: true })
  assert.equal(focused, 'last')
  state.document.activeElement = {}
  state.keyboard(event)
  assert.equal(focused, 'brand')
  state.keyboard({ ...event, key: 'Escape' })
  assert.deepEqual(state.emitted, ['close'])
})
