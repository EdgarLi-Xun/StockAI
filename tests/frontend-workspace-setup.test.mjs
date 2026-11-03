import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source = readFileSync(new URL('../frontend/src/workspaceSetup.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText
const module = { exports: {} }
vm.runInNewContext(code, { exports: module.exports, module })
const { parseRecovery, syncConnection } = module.exports
const recovery = { format: 'stockai-sync-recovery/1', space_id: 'a'.repeat(32), key: 'local-test-key', anchor: { sequence: 1, hash: 'b'.repeat(64) } }

test('choosing either existing environment always joins its recovery space, even with a stale create flag', () => {
  for (const source of ['desktop', 'web']) {
    const request = syncConnection(source, { create: true, url: 'https://sync.example.test', recovery: JSON.stringify(recovery) })
    assert.equal(request.create, false)
    assert.equal(request.recovery.space_id, recovery.space_id)
    assert.equal(request.url, 'https://sync.example.test')
  }
})

test('only an explicit new space choice creates a space and excludes stale recovery material', () => {
  const request = syncConnection('publish', { create: false, recovery: JSON.stringify(recovery) })
  assert.equal(request.create, true)
  assert.equal(request.recovery, undefined)
  for (const source of ['', 'independent', 'legacy']) assert.throws(() => syncConnection(source, {}), /请先选择/)
})

test('invalid files, unrelated backup JSON and missing trust anchors cannot be sent as recovery material', () => {
  assert.throws(() => parseRecovery('broken'), /有效的 JSON/)
  for (const value of [null, [], { ...recovery, format: 'backup' }, { ...recovery, anchor: null }, { ...recovery, anchor: { sequence: -1, hash: 'b'.repeat(64) } }]) {
    assert.throws(() => syncConnection('web', { recovery: JSON.stringify(value) }), /StockAI/)
  }
})

const appSource = readFileSync(new URL('../frontend/src/PublicApp.vue', import.meta.url), 'utf8').match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const parsed = ts.createSourceFile('PublicApp.ts', appSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
const routeSource = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'initialWorkspaceRoute').getText(parsed)
const routeCode = ts.transpileModule(routeSource, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
test('login resumes a first merge until it is confirmed, while established and independent workspaces open normally', async () => {
  for (const [setup, expected] of [
    [{ required: true, first_merge: false }, 'system'],
    [{ required: false, first_merge: true }, 'system'],
    [{ required: false, first_merge: false }, 'dashboard'],
  ]) {
    const context = { api: async () => setup }
    vm.runInNewContext(routeCode + '\nglobalThis.route = initialWorkspaceRoute;', context)
    assert.equal(await context.route(), expected)
  }
})
