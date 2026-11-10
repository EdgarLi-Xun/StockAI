import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source = readFileSync(new URL('../frontend/src/appearance.ts', import.meta.url), 'utf8')
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

function environment({ stored = null, dark = false, blocked = false } = {}) {
  const module = { exports: {} }, events = new Map(), mediaEvents = new Map(), tokens = new Map()
  const storage = new Map(stored === null ? [] : [['stockai.appearance.v1', stored]])
  const root = { dataset: {}, style: { setProperty: (name, value) => tokens.set(name, value) } }
  const media = { matches: dark, addEventListener: (name, fn) => mediaEvents.set(name, fn) }
  const window = {
    localStorage: {
      getItem(key) { if (blocked) throw new Error('Storage unavailable'); return storage.get(key) ?? null },
      setItem(key, value) { if (blocked) throw new Error('Storage unavailable'); storage.set(key, value) },
    },
    matchMedia: query => { assert.equal(query, '(prefers-color-scheme: dark)'); return media },
    addEventListener: (name, fn) => events.set(name, fn),
  }
  const document = { documentElement: root, querySelector: () => ({ setAttribute: () => {} }) }
  vm.runInNewContext(code, { require, module, exports: module.exports, window, document })
  const service = module.exports
  service.initializeAppearance()
  return { service, root, tokens, storage, events, mediaEvents }
}
const settings = (theme = 'system', accent = '#1b674c', marketColors = 'red-up') => JSON.stringify({ theme, accent, marketColors })

test('restores theme and normalized custom colors before the app mounts', () => {
  const { service, root, tokens } = environment({ stored: settings('dark', '#ABCDEF', 'green-up') })
  assert.equal(root.dataset.theme, 'dark')
  assert.equal(root.dataset.marketColors, 'green-up')
  assert.equal(service.appearance.accent, '#abcdef')
  assert.equal(tokens.get('--accent'), '#abcdef')
  assert.equal(tokens.get('--surface'), '#18211e')
})

test('follows OS changes only in system mode and resumes following when reselected', () => {
  const { service, root, mediaEvents } = environment({ dark: true })
  assert.equal(root.dataset.theme, 'dark')
  mediaEvents.get('change')({ matches: false })
  assert.equal(root.dataset.theme, 'light')
  service.updateAppearance({ theme: 'dark' })
  mediaEvents.get('change')({ matches: false })
  assert.equal(root.dataset.theme, 'dark')
  service.updateAppearance({ theme: 'system' })
  assert.equal(root.dataset.theme, 'light')
  mediaEvents.get('change')({ matches: true })
  assert.equal(root.dataset.theme, 'dark')
})

test('custom colors and market direction survive a fresh window', () => {
  const first = environment()
  first.service.updateAppearance({ theme: 'light', accent: '#2563EB', marketColors: 'green-up' })
  const saved = first.storage.get(first.service.APPEARANCE_KEY)
  assert.deepEqual(JSON.parse(saved), { theme: 'light', accent: '#2563eb', marketColors: 'green-up' })
  const restored = environment({ stored: saved })
  assert.equal(restored.root.dataset.theme, 'light')
  assert.equal(restored.tokens.get('--quote-up'), '#08794f')
  assert.equal(restored.tokens.get('--quote-down'), '#bf3545')
  assert.equal(restored.service.appearanceSaved.value, true)
})

test('text and chart palettes update together without a data reload', () => {
  const { service, tokens } = environment()
  service.updateAppearance({ theme: 'dark', accent: '#7c3aed', marketColors: 'green-up' })
  const palette = service.appearancePalette.value
  assert.equal(tokens.get('--quote-up'), palette.up)
  assert.equal(tokens.get('--quote-down'), palette.down)
  assert.equal(tokens.get('--surface'), palette.surface)
  assert.equal(tokens.get('--accent-text'), palette.accentText)
  assert.equal(palette.up, '#61d6a5')
  assert.equal(palette.down, '#ff858d')
})

test('corrupt and unsupported storage values fall back safely', () => {
  for (const stored of ['{broken', 'null', '[]', '42', '{"theme":"sepia","accent":"url(unsafe)","marketColors":"other"}']) {
    const { service, tokens } = environment({ stored })
    assert.equal(service.appearance.theme, 'system')
    assert.equal(service.appearance.accent, '#1b674c')
    assert.equal(service.appearance.marketColors, 'red-up')
    assert.equal(tokens.get('--accent'), '#1b674c')
  }
})

test('storage restrictions keep the app usable and report that settings cannot be saved', () => {
  const { service, root, tokens } = environment({ blocked: true })
  assert.equal(service.appearanceSaved.value, false)
  service.updateAppearance({ theme: 'dark', accent: '#be185d' })
  assert.equal(root.dataset.theme, 'dark')
  assert.equal(tokens.get('--accent'), '#be185d')
  assert.equal(service.appearanceSaved.value, false)
})

test('other windows and clearing local storage update the current appearance', () => {
  const { service, root, events, tokens } = environment()
  const receive = events.get('storage')
  receive({ key: 'unrelated', newValue: settings('dark') })
  assert.equal(root.dataset.theme, 'light')
  receive({ key: service.APPEARANCE_KEY, newValue: settings('dark', '#0f766e', 'green-up') })
  assert.equal(root.dataset.theme, 'dark')
  assert.equal(tokens.get('--accent'), '#0f766e')
  receive({ key: null, newValue: null })
  assert.equal(root.dataset.theme, 'light')
  assert.equal(service.appearance.accent, '#1b674c')
})

test('reset restores all defaults and persists them', () => {
  const { service, storage } = environment({ stored: settings('dark', '#b45309', 'green-up') })
  service.resetAppearance()
  assert.deepEqual(JSON.parse(storage.get(service.APPEARANCE_KEY)), JSON.parse(JSON.stringify(service.DEFAULT_APPEARANCE)))
})

test('very light, very dark, and midtone custom colors retain readable text', () => {
  const { service } = environment()
  for (const dark of [false, true]) {
    for (const accent of ['#ffffff', '#000000', '#ffff00', '#808080', '#777777', '#00ffff', '#ff0000', '#0000ff', ...service.accentPresets.map(p => p.color)]) {
      const palette = service.buildAppearancePalette({ theme: 'system', accent, marketColors: 'red-up' }, dark)
      assert.ok(service.contrastRatio(palette.accentText, palette.surfaceSoft) >= 4.5, accent + ' emphasis')
      assert.ok(service.contrastRatio(palette.onAccent, palette.accent) >= 4.5, accent + ' button text')
      assert.ok(service.contrastRatio(palette.onAccent, palette.accentHover) >= 4.5, accent + ' hover text')
    }
  }
})

test('chart rendering uses the current candle, tooltip, and equity colors', () => {
  const { service } = environment()
  const script = readFileSync(new URL('../frontend/src/components/ResearchChart.vue', import.meta.url), 'utf8').split('</script>')[0]
  const parsed = ts.createSourceFile('chart.ts', script.slice(script.indexOf('>') + 1), ts.ScriptTarget.Latest, true)
  const draw = parsed.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'draw')
  const drawCode = ts.transpileModule(draw.getText(parsed), { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText
  const renders = []
  const context = {
    root: { value: {} }, hasData: { value: true },
    chart: { setOption: value => renders.push(value) },
    props: { kind: 'candle', indicator: 'volume', bars: [{ ts: '2026-01-01', open: 10, close: 11, low: 9, high: 12, volume: 100 }], indicators: [], curve: [{ ts: '2026-01-01', equity: 1000 }] },
    appearancePalette: service.appearancePalette, mixColor: service.mixColor, compact: String,
  }
  vm.runInNewContext(drawCode + '\nglobalThis.draw = draw', context)
  context.draw()
  assert.equal(renders.at(-1).series[0].itemStyle.color, '#bf3545')
  service.updateAppearance({ theme: 'dark', marketColors: 'green-up', accent: '#2563eb' })
  context.draw()
  const dark = renders.at(-1)
  assert.equal(dark.series[0].itemStyle.color, '#61d6a5')
  assert.equal(dark.series[0].itemStyle.color0, '#ff858d')
  assert.equal(dark.tooltip.backgroundColor, '#18211e')
  assert.equal(dark.tooltip.textStyle.color, '#e6f0ea')
  context.props.kind = 'equity'
  context.draw()
  assert.equal(renders.at(-1).series[0].lineStyle.color, service.appearancePalette.value.accentText)
})
