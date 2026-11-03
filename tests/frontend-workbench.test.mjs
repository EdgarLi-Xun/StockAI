import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { setImmediate as nextTurn } from 'node:timers/promises'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source = readFileSync(new URL('../frontend/src/ResearchWorkbench.vue', import.meta.url), 'utf8').split('</script>')[0]
const parsed = ts.createSourceFile('workbench.ts', source.slice(source.indexOf('>') + 1), ts.ScriptTarget.Latest, true)
// Execute the component's real request functions with controlled network completion order.
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && ['refresh', 'openSession'].includes(node.name?.text)).map(node => node.getText(parsed)).join('\n')
const code = ts.transpileModule(functions, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function client(view = 'journals') {
  const requests = [], ref = value => ({ value })
  const state = {
    props: { view }, generation: 0, alive: true, sessionRead: 0, location: { hash: '#dashboard' }, URLSearchParams,
    page: ref(1), size: ref(20), query: ref(''), status: ref(''), topicFilter: ref('original-topic'), sessionPage: ref(1), messagePage: ref(1),
    loading: ref(false), error: ref(''), list: ref({ items: [], total: 0 }), topics: ref([]), readiness: ref(null), usage: ref(null),
    limits: ref({}), settingsDirty: ref({}), events: ref(null), notification: ref(null), selectedAccount: ref('account-1'), accounts: ref([]),
    risk: ref(null), sessions: ref({ items: [] }), session: ref(null), help: ref(null),
    api: path => new Promise((resolve, reject) => requests.push({ path, resolve, reject }))
  }
  vm.runInNewContext(code + '\nglobalThis.invoke = refresh;', state)
  function take(prefix) {
    const index = requests.findIndex(request => request.path.startsWith(prefix))
    assert.notEqual(index, -1, 'expected request: ' + prefix)
    return requests.splice(index, 1)[0]
  }
  async function respond(prefix, value) { take(prefix).resolve(value); await nextTurn() }
  return { state, requests, take, respond, refresh: () => state.invoke() }
}

test('late journal rows cannot replace the experiment page after navigation', async () => {
  const c = client(), journal = c.refresh()
  const old = c.take('/me/journals?')
  c.state.props.view = 'experiments'
  const experiments = c.refresh()
  await c.respond('/me/experiment-topics?', { items: [] })
  await c.respond('/me/experiments?', { items: [{ id: 'experiment-1', parameters: { instrument_id: 'SH:1' } }], total: 1 })
  await experiments
  old.resolve({ items: [{ id: 'journal-1', data: { instrument_id: 'SH:1' } }], total: 1 })
  await journal
  assert.equal(c.state.list.value.items[0].id, 'experiment-1')
  assert.equal(c.state.list.value.items[0].parameters.instrument_id, 'SH:1')
})

test('a late earlier page cannot replace the newest page in the same view', async () => {
  const c = client(), first = c.refresh(), old = c.take('/me/journals?')
  c.state.page.value = 2
  const second = c.refresh(), current = c.take('/me/journals?')
  assert.equal(new URLSearchParams(current.path.split('?')[1]).get('page'), '2')
  current.resolve({ items: [{ id: 'page-2' }] }); await second
  old.resolve({ items: [{ id: 'page-1' }] }); await first
  assert.equal(c.state.list.value.items[0].id, 'page-2')
  assert.equal(c.state.loading.value, false)
})

test('a stale failure cannot replace the active request error or loading state', async () => {
  const c = client(), first = c.refresh(), old = c.take('/me/journals?')
  c.state.page.value = 2
  const second = c.refresh()
  old.reject(new Error('obsolete failure')); await first
  assert.equal(c.state.error.value, '')
  assert.equal(c.state.loading.value, true)
  await c.respond('/me/journals?', { items: [{ id: 'page-2' }] }); await second
  assert.equal(c.state.loading.value, false)
})

test('a stale multi-step experiment refresh stops before fetching another page', async () => {
  const c = client('experiments'), old = c.refresh()
  c.state.props.view = 'journals'
  const current = c.refresh()
  await c.respond('/me/experiment-topics?', { items: [{ id: 'stale-topic' }] }); await old
  assert.equal(c.requests.some(request => request.path.startsWith('/me/experiments?')), false)
  assert.equal(c.state.topics.value.length, 0)
  await c.respond('/me/journals?', { items: [] }); await current
})

test('experiment requests retain the topic and pagination captured when they started', async () => {
  const c = client('experiments'), pending = c.refresh()
  c.state.topicFilter.value = 'later-topic'; c.state.page.value = 3
  await c.respond('/me/experiment-topics?', { items: [] })
  const result = c.take('/me/experiments?'), params = new URLSearchParams(result.path.split('?')[1])
  assert.equal(params.get('topic_id'), 'original-topic')
  assert.equal(params.get('page'), '1')
  result.resolve({ items: [] }); await pending
})
