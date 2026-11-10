import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source = readFileSync(new URL('../frontend/src/App.vue', import.meta.url), 'utf8').split('</script>')[0]
const parsed = ts.createSourceFile('app.ts', source.slice(source.indexOf('>') + 1), ts.ScriptTarget.Latest, true)
const names = ['loadIndex', 'showReport', 'closeReport', 'go', 'syncHash', 'action']
const functions = parsed.statements.filter(node => ts.isFunctionDeclaration(node) && names.includes(node.name?.text)).map(node => node.getText(parsed)).join('\n')
const code = ts.transpileModule(functions, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText

function client(page = 'dashboard') {
  const ref = value => ({ value }), requests = []
  const state = {
    activeIndex: ref('SH:000001'), indexData: ref(null), indexRead: 0,
    selectedReport: ref(null), reportRead: 0, reportLoading: ref(false),
    page: ref(page), busy: ref(false), error: ref(''), sidebarOpen: ref(false),
    props: { publicMode: false }, location: { hash: '#' + page },
    nav: ['dashboard', 'reports', 'review', 'market'].map(id => ({ id })),
    emit() {}, notify() {}, openRouteContext() {},
    api: path => new Promise((resolve, reject) => requests.push({ path, resolve, reject })),
  }
  vm.runInNewContext(code, state)
  return { state, requests }
}

const index = (id, close) => ({ instrument: { id }, bars: [{ close }] })

test('a slow previous index cannot replace the newly selected index', async () => {
  const { state, requests } = client()
  const first = state.loadIndex()
  state.activeIndex.value = 'SZ:399001'
  const second = state.loadIndex()
  requests[1].resolve(index('SZ:399001', 20)); await second
  requests[0].resolve(index('SH:000001', 10)); await first
  assert.equal(state.indexData.value.instrument.id, 'SZ:399001')
})

test('changing the index clears the previous chart while data is loading', async () => {
  const { state, requests } = client('market')
  state.indexData.value = index('SH:000001', 10)
  state.activeIndex.value = 'SZ:399001'
  const pending = state.loadIndex()
  assert.equal(state.indexData.value, null)
  requests[0].resolve(index('SZ:399001', 20)); await pending
})

test('returning to the same index still discards its earlier request', async () => {
  const { state, requests } = client()
  const first = state.loadIndex()
  state.activeIndex.value = 'SZ:399001'
  const second = state.loadIndex()
  state.activeIndex.value = 'SH:000001'
  const third = state.loadIndex()
  requests[2].resolve(index('SH:000001', 30)); await third
  requests[1].resolve(index('SZ:399001', 20)); await second
  requests[0].resolve(index('SH:000001', 10)); await first
  assert.equal(state.indexData.value.bars[0].close, 30)
})

test('index failures are handled and only the active request can show an error', async () => {
  const { state, requests } = client()
  const first = state.loadIndex()
  state.activeIndex.value = 'SZ:399001'
  const second = state.loadIndex()
  requests[1].resolve(index('SZ:399001', 20)); await second
  requests[0].reject(new Error('old failure')); await first
  assert.equal(state.error.value, '')
  const retry = state.loadIndex()
  requests[2].reject(new Error('current failure')); await retry
  assert.equal(state.error.value, 'current failure')
})

test('opening two reports keeps the latest selection when responses arrive out of order', async () => {
  const { state, requests } = client('reports')
  const first = state.showReport('first'), second = state.showReport('second')
  requests[1].resolve({ id: 'second' }); await second
  requests[0].resolve({ id: 'first' }); await first
  assert.equal(state.selectedReport.value.id, 'second')
})

test('leaving the index page discards its late error', async () => {
  const { state, requests } = client()
  const pending = state.loadIndex()
  state.go('reports')
  requests[0].reject(new Error('old index failure')); await pending
  assert.equal(state.error.value, '')
})

test('leaving and returning to reports cannot revive an old pending report', async () => {
  for (const viaHash of [false, true]) {
    const { state, requests } = client('reports')
    const pending = state.showReport('old')
    if (viaHash) {
      state.location.hash = '#dashboard'; state.syncHash()
      state.location.hash = '#reports'; state.syncHash()
    } else {
      state.go('dashboard'); state.go('reports')
    }
    requests[0].resolve({ id: 'old' }); await pending
    assert.equal(state.selectedReport.value, null)
  }
})

test('stale report failures cannot overwrite a newer report or its loading state', async () => {
  const { state, requests } = client('reports')
  const first = state.showReport('first'), second = state.showReport('second')
  requests[0].reject(new Error('old failure')); await first
  assert.equal(state.error.value, '')
  assert.equal(state.reportLoading.value, true)
  requests[1].resolve({ id: 'second' }); await second
  assert.equal(state.selectedReport.value.id, 'second')
  assert.equal(state.reportLoading.value, false)
})

test('cancelling a pending report keeps the list visible after its response arrives', async () => {
  const { state, requests } = client('reports')
  const pending = state.showReport('cancelled')
  state.closeReport()
  assert.equal(state.reportLoading.value, false)
  requests[0].resolve({ id: 'cancelled' }); await pending
  assert.equal(state.selectedReport.value, null)
})

test('a failed active report can be retried without leaving loading stuck', async () => {
  const { state, requests } = client('reports')
  const first = state.showReport('retry')
  requests[0].reject(new Error('temporarily unavailable')); await first
  assert.equal(state.error.value, 'temporarily unavailable')
  assert.equal(state.reportLoading.value, false)
  const retry = state.showReport('retry')
  requests[1].resolve({ id: 'retry' }); await retry
  assert.equal(state.selectedReport.value.id, 'retry')
  assert.equal(state.error.value, '')
  assert.equal(state.reportLoading.value, false)
})
