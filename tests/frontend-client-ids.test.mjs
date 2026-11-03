import { test } from 'node:test'
import assert from 'node:assert/strict'
import { webcrypto } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source = readFileSync(new URL('../frontend/src/clientIds.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

function bootstrap(crypto) {
  const module = { exports: {} }
  vm.runInNewContext(code, { crypto, module, exports: module.exports })
  return crypto
}

test('LAN HTTP bootstrap creates valid UUIDs with Web Crypto when randomUUID is unavailable', () => {
  let calls = 0
  const crypto = bootstrap({ getRandomValues(bytes) { calls++; return webcrypto.getRandomValues(bytes) } })
  const ids = Array.from({ length: 10 }, () => crypto.randomUUID())
  assert.equal(new Set(ids).size, ids.length)
  assert.equal(calls, ids.length)
  for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/)
})

test('fallback UUIDs retain the standard version and variant bits', () => {
  const crypto = bootstrap({ getRandomValues(bytes) { assert.equal(bytes.length, 16); return bytes.fill(255) } })
  assert.equal(crypto.randomUUID(), 'ffffffff-ffff-4fff-bfff-ffffffffffff')
})

test('secure contexts keep the original native UUID function', () => {
  const native = () => 'native-uuid'
  const crypto = bootstrap({ randomUUID: native, getRandomValues() { throw new Error('Native UUID generation should be retained') } })
  assert.equal(crypto.randomUUID, native)
  assert.equal(crypto.randomUUID(), 'native-uuid')
})
