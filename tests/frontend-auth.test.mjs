import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const text = readFileSync(new URL('../frontend/src/PublicApp.vue', import.meta.url), 'utf8').split('</script>')[0]
const parsed = ts.createSourceFile('auth.ts', text.slice(text.indexOf('>') + 1), ts.ScriptTarget.Latest, true)
const code = ['restoreSavedLogin', 'clearSavedLogin', 'completeLogin', 'submitAuth', 'logout'].map(name => {
  const fn = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === name)
  assert.ok(fn, name)
  return ts.transpileModule(fn.getText(parsed), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
}).join('\n')

function auth(view = 'login', remember = true, setup = false) {
  const state = {
    form: { value: { email: 'owner@example.test', password: 'temporary-password', invitation: 'unused', confirmation: '' } },
    authView: { value: view }, rememberLogin: { value: remember }, needsSetup: { value: setup },
    rememberCredentials: { value: false }, savedCredentials: { value: false },
    user: { value: null }, route: { value: view }, notice: { value: '' }, location: { hash: '#' + view },
    calls: [], entered: 0, savedWrites: [], savedLogin: null, forgotten: 0,
    URLSearchParams,
  }
  state.action = async fn => fn()
  state.post = async (path, value) => { state.calls.push({ path, value }); return { user: { id: 'owner' } } }
  state.api = async path => { state.calls.push({ path }) }
  state.enterWorkspace = async () => { state.entered++ }
  state.saveLoginCredentials = async (email, password) => { state.savedWrites.push({ email, password }); state.savedLogin = { email, password } }
  state.loadLoginCredentials = async () => state.savedLogin
  state.forgetLoginCredentials = async () => { state.forgotten++; state.savedLogin = null }
  state.clearPrivate = () => { state.calls.push({ path: 'clear-private' }); state.user.value = null; state.form.value.password = '' }
  state.go = async path => { state.route.value = path }
  state.history = { replaceState: (_, __, hash) => { state.location.hash = hash } }
  vm.runInNewContext(code + '\nglobalThis.submitAuth = submitAuth; globalThis.logout = logout; globalThis.restoreSavedLogin = restoreSavedLogin; globalThis.clearSavedLogin = clearSavedLogin', state)
  return state
}

test('login submits the chosen persistence and clears the password after success', async () => {
  for (const remember of [true, false]) {
    const s = auth('login', remember)
    await s.submitAuth()
    assert.equal(s.calls[0].path, '/auth/login')
    assert.deepEqual(JSON.parse(JSON.stringify(s.calls[0].value)), { email: 'owner@example.test', password: 'temporary-password', remember })
    assert.equal(s.form.value.password, '')
    assert.equal(s.user.value.id, 'owner')
    assert.equal(s.entered, 1)
  }
})

test('first desktop setup uses the same remember-login choice', async () => {
  for (const remember of [true, false]) {
    const s = auth('register', remember, true)
    await s.submitAuth()
    assert.equal(s.calls[0].path, '/desktop/setup')
    assert.equal(s.calls[0].value.remember, remember)
    assert.equal(s.needsSetup.value, false)
    assert.equal(s.form.value.password, '')
    assert.equal(s.entered, 1)
  }
})

test('logout clears private data and establishes anonymous CSRF before the next login', async () => {
  const s = auth()
  s.user.value = { id: 'owner' }
  await s.logout()
  assert.deepEqual(s.calls.map(call => call.path), ['/auth/logout', 'clear-private', '/auth/csrf'])
  assert.equal(s.user.value, null)
  assert.equal(s.form.value.password, '')
  assert.equal(s.route.value, 'login')
})

test('password reset clears the revoked remembered session before signing in again', async () => {
  const s = auth('reset-password')
  s.location.hash = '#reset-password?token=reset-token'
  s.user.value = { id: 'owner' }
  await s.submitAuth()
  assert.deepEqual(s.calls.map(call => call.path), ['/auth/reset-password', 'clear-private', '/auth/csrf'])
  assert.equal(s.calls[0].value.token, 'reset-token')
  assert.equal(s.user.value, null)
  assert.equal(s.form.value.password, '')
  assert.equal(s.location.hash, '#login')
  assert.equal(s.forgotten, 1)
})

test('credentials are saved only after a successful opted-in login', async () => {
  const s = auth()
  s.rememberCredentials.value = true
  s.post = async () => { throw new Error('incorrect password') }
  await assert.rejects(s.submitAuth(), /incorrect password/)
  assert.equal(s.savedWrites.length, 0)
  s.post = async () => ({ user: { id: 'owner' } })
  await s.submitAuth()
  assert.deepEqual(s.savedWrites, [{ email: 'owner@example.test', password: 'temporary-password' }])
  assert.equal(s.form.value.password, '')
  assert.equal(s.entered, 1)
})

test('a password-manager failure cannot turn a successful login into failure', async () => {
  const s = auth()
  s.rememberCredentials.value = true
  s.saveLoginCredentials = async () => { throw new Error('storage unavailable') }
  await s.submitAuth()
  assert.equal(s.user.value.id, 'owner')
  assert.equal(s.entered, 1)
  assert.equal(s.form.value.password, '')
  assert.match(s.notice.value, /未能保存/)
})

test('logout may prefill remembered credentials but never logs in automatically', async () => {
  const s = auth()
  s.user.value = { id: 'owner' }
  s.savedLogin = { email: 'owner@example.test', password: 'remembered-password' }
  await s.logout()
  assert.equal(s.user.value, null)
  assert.equal(s.form.value.password, 'remembered-password')
  assert.equal(s.rememberCredentials.value, true)
  assert.equal(s.entered, 0)
  assert.ok(!s.calls.some(call => call.path === '/auth/login'))
})

test('restoring credentials cannot overwrite manual input for another account', async () => {
  const s = auth()
  s.savedLogin = { email: 'other@example.test', password: 'other-password' }
  s.form.value.password = 'manual-password'
  await s.restoreSavedLogin()
  assert.equal(s.form.value.email, 'owner@example.test')
  assert.equal(s.form.value.password, 'manual-password')
})
