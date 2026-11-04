import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const text = readFileSync(new URL('../frontend/src/loginCredentials.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(text, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
function client(native = null, manager = true) {
  const storage = new Map(), stored = [], module = { exports: {} }
  const window = { stockaiLogin: native, isSecureContext: true }
  if (manager) window.PasswordCredential = class { constructor(data) { Object.assign(this, data); this.type = 'password' } }
  const credentials = { store: async value => { stored.push(value) }, get: async () => stored.at(-1) || null, preventSilentAccess: async () => { stored.length = 0 } }
  vm.runInNewContext(code, { exports: module.exports, module, window, navigator: { credentials }, localStorage: { getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) }, setTimeout, clearTimeout })
  return { api: module.exports, storage, stored, credentials }
}

test('native secrets use the pinned bridge and never browser storage', async () => {
  let login = null
  const c = client({ read: done => done(JSON.stringify({ ok: true, login })), save: (email, password, done) => { login = { email, password }; done('{"ok":true}') }, clear: done => { login = null; done('{"ok":true}') } })
  assert.equal(await c.api.loginCredentialSupport(), 'native')
  await c.api.saveLoginCredentials('owner@example.test', 'test-private-password')
  assert.equal((await c.api.loadLoginCredentials()).password, 'test-private-password')
  assert.equal(c.storage.size, 0)
  assert.equal(c.stored.length, 0)
  await c.api.forgetLoginCredentials()
  assert.equal(await c.api.loadLoginCredentials(), null)
})

test('browser passwords go to the password manager, with only the username in local storage', async () => {
  const c = client()
  assert.equal(await c.api.loginCredentialSupport(), 'browser')
  await c.api.saveLoginCredentials('owner@example.test', 'test-private-password')
  assert.equal(c.stored[0].password, 'test-private-password')
  assert.deepEqual([...c.storage.values()], ['owner@example.test'])
  assert.equal((await c.api.loadLoginCredentials()).password, 'test-private-password')
  await c.api.forgetLoginCredentials()
  assert.equal(c.storage.size, 0)
})

test('another saved browser identity cannot supply a password to this username', async () => {
  const c = client()
  await c.api.saveLoginCredentials('owner@example.test', 'test-private-password')
  c.credentials.get = async () => ({ type: 'password', id: 'other@example.test', password: 'other-private-password' })
  const value = await c.api.loadLoginCredentials()
  assert.equal(value.email, 'owner@example.test')
  assert.equal(value.password, '')
})

test('unsupported browsers do not fall back to storing passwords as text', async () => {
  const c = client(null, false)
  assert.equal(await c.api.loginCredentialSupport(), null)
  await assert.rejects(c.api.saveLoginCredentials('owner@example.test', 'test-private-password'))
  assert.equal(c.storage.size, 0)
})

test('native storage errors remain failures and never trigger browser storage', async () => {
  const c = client({ save: (_, __, done) => done('{"ok":false,"error":"storage unavailable"}') })
  await assert.rejects(c.api.saveLoginCredentials('owner@example.test', 'test-private-password'), /storage unavailable/)
  assert.equal(c.storage.size, 0)
  assert.equal(c.stored.length, 0)
})
