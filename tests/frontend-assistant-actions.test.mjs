import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source = readFileSync(new URL('../frontend/src/ResearchWorkbench.vue', import.meta.url), 'utf8').split('</script>')[0]
const parsed = ts.createSourceFile('workbench.ts', source.slice(source.indexOf('>') + 1), ts.ScriptTarget.Latest, true)
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && ['action', 'openSession', 'applyAssistantAction'].includes(node.name?.text)).map(node => node.getText(parsed)).join('\n')
const code = ts.transpileModule(functions, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function client() {
  const calls = [], ref = value => ({ value })
  const message = { id: 'message-1', status: 'success', reply: { actions: [{ kind: 'preferences', changes: [{ field: 'budget', before: 1000, after: 500 }] }] } }
  const state = {
    busy: ref(false), error: ref(''), notice: ref(''), alive: true, sessionRead: 0,
    session: ref({ id: 'session-1', messages: [] }), messagePage: ref(1), size: ref(20),
    common: async () => { calls.push({ method: 'options' }) },
    api: async path => { calls.push({ method: 'GET', path }); return { id: 'session-1', messages: [message] } },
    post: async (path, body) => { calls.push({ method: 'POST', path, body }); return { reused: false } }
  }
  vm.runInNewContext(code + '\nglobalThis.read = openSession; globalThis.apply = applyAssistantAction;', state)
  return { calls, state, message }
}

test('reading and polling a proposed configuration never applies it', async () => {
  const c = client()
  await c.state.read('session-1')
  await c.state.read('session-1')
  assert.equal(c.state.session.value.messages[0].reply.actions[0].changes[0].after, 500)
  assert.deepEqual(c.calls.map(call => call.method), ['GET', 'GET'])
})

test('manual apply targets the displayed message and explicitly confirms once', async () => {
  const c = client()
  await c.state.apply(c.message, 0)
  assert.deepEqual(c.calls.map(call => call.method), ['POST', 'GET', 'options'])
  assert.equal(c.calls[0].path, '/me/assistant/requests/message-1/actions/0/apply')
  assert.equal(c.calls[0].body.confirm, true)
  assert.equal(c.state.busy.value, false)
})

test('a failed manual application keeps the proposal visible and never retries automatically', async () => {
  const c = client()
  c.state.post = async (path, body) => { c.calls.push({ method: 'POST', path, body }); throw Object.assign(new Error('stale proposal'), { code: 'VERSION_CONFLICT' }) }
  await c.state.apply(c.message, 0)
  assert.equal(c.calls.length, 1)
  assert.match(c.state.error.value, /已有新版本/)
  assert.equal(c.message.reply.actions[0].applied_at, undefined)
  assert.equal(c.state.busy.value, false)
})

test('research action labels are available on the proposal card', () => {
  const vue = readFileSync(new URL('../frontend/src/components/AssistantAction.vue', import.meta.url), 'utf8')
  for (const label of ['添加提醒', '保存判断', '创建课题', '加入实验队列']) assert.match(vue, new RegExp(label))
})
