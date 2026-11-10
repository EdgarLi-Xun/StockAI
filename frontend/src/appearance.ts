import { computed, reactive, readonly, ref } from 'vue'

export type ThemeMode = 'system' | 'light' | 'dark'
export type MarketColors = 'red-up' | 'green-up'
export interface AppearanceSettings {
  theme: ThemeMode
  accent: string
  marketColors: MarketColors
}

export const APPEARANCE_KEY = 'stockai.appearance.v1'
export const DEFAULT_APPEARANCE: AppearanceSettings = { theme: 'system', accent: '#1b674c', marketColors: 'red-up' }
export const accentPresets = [
  { name: '森林绿', color: '#1b674c' },
  { name: '湖水蓝', color: '#2563eb' },
  { name: '鸢尾紫', color: '#7c3aed' },
  { name: '青碧', color: '#0f766e' },
  { name: '琥珀', color: '#b45309' },
  { name: '玫瑰', color: '#be185d' },
]

export function normalizeAppearance(value: unknown): AppearanceSettings {
  const input = value && typeof value === 'object' ? value as Partial<AppearanceSettings> : {}
  return {
    theme: ['system', 'light', 'dark'].includes(input.theme as string) ? input.theme! : DEFAULT_APPEARANCE.theme,
    accent: typeof input.accent === 'string' && /^#[\da-f]{6}$/i.test(input.accent) ? input.accent.toLowerCase() : DEFAULT_APPEARANCE.accent,
    marketColors: input.marketColors === 'green-up' ? 'green-up' : 'red-up',
  }
}

function parseAppearance(raw: string | null) {
  try { return normalizeAppearance(raw ? JSON.parse(raw) : null) }
  catch { return { ...DEFAULT_APPEARANCE } }
}

function rgb(hex: string) { return [1, 3, 5].map(index => parseInt(hex.slice(index, index + 2), 16)) }
export function mixColor(color: string, target: string, weight: number) {
  const end = rgb(target)
  return '#' + rgb(color).map((value, index) => Math.round(value * (1 - weight) + end[index]! * weight).toString(16).padStart(2, '0')).join('')
}
function luminance(color: string) {
  const channels = rgb(color).map(value => {
    const channel = value / 255
    return channel <= .04045 ? channel / 12.92 : ((channel + .055) / 1.055) ** 2.4
  })
  return channels[0]! * .2126 + channels[1]! * .7152 + channels[2]! * .0722
}
export function contrastRatio(first: string, second: string) {
  const values = [luminance(first), luminance(second)].sort((a, b) => b - a)
  return (values[0]! + .05) / (values[1]! + .05)
}
function readableAccent(accent: string, surface: string, dark: boolean) {
  for (let step = 0; step <= 20; step++) {
    const candidate = mixColor(accent, dark ? '#ffffff' : '#000000', step / 20)
    if (contrastRatio(candidate, surface) >= 4.5) return candidate
  }
  return dark ? '#ffffff' : '#000000'
}

// CSS 和画布共用一套颜色，切换主题时无需重新加载行情。
export function buildAppearancePalette(settings: AppearanceSettings, dark: boolean) {
    const background = dark ? '#101816' : '#eef3f0'
    const surface = dark ? '#18211e' : '#ffffff'
    const surfaceSoft = dark ? '#212b27' : '#f3f7f4'
    const red = dark ? '#ff858d' : '#bf3545'
    const green = dark ? '#61d6a5' : '#08794f'
  const accentText = readableAccent(settings.accent, surfaceSoft, dark)
  const onAccent = contrastRatio(settings.accent, '#ffffff') >= 4.5 ? '#ffffff' : '#000000'
  return {
    background, surface, surfaceSoft,
    text: dark ? '#e6f0ea' : '#1c2b26',
    muted: dark ? '#a3b5ab' : '#5f6f68',
    border: dark ? '#31423b' : '#e3ebe6',
    accent: settings.accent,
    accentHover: mixColor(settings.accent, onAccent === '#ffffff' ? '#000000' : '#ffffff', .18),
    accentText,
    accentSoft: mixColor(settings.accent, surface, dark ? .85 : .93),
    onAccent,
    up: settings.marketColors === 'red-up' ? red : green,
    down: settings.marketColors === 'red-up' ? green : red,
  }
}

const state = reactive<AppearanceSettings>({ ...DEFAULT_APPEARANCE })
const systemDark = ref(false)
export const appearance = readonly(state)
export const appearanceSaved = ref(true)
export const resolvedTheme = computed(() => state.theme === 'system' ? (systemDark.value ? 'dark' : 'light') : state.theme)
export const appearancePalette = computed(() => buildAppearancePalette(state, resolvedTheme.value === 'dark'))
let initialized = false

function applyAppearance() {
  const root = document.documentElement, palette = appearancePalette.value
  root.dataset.theme = resolvedTheme.value
  root.dataset.marketColors = state.marketColors
  const tokens: Record<string, string> = {
    '--canvas': palette.background, '--surface': palette.surface, '--surface-soft': palette.surfaceSoft,
    '--text-primary': palette.text, '--text-muted': palette.muted, '--line': palette.border,
    '--accent': palette.accent, '--accent-hover': palette.accentHover, '--accent-text': palette.accentText,
    '--accent-soft': palette.accentSoft, '--on-accent': palette.onAccent,
    '--green': palette.accent, '--green-dark': palette.accentHover, '--dark': palette.text,
    '--muted': palette.muted, '--border': palette.border, '--quote-up': palette.up, '--quote-down': palette.down,
  }
  for (const [name, value] of Object.entries(tokens)) root.style.setProperty(name, value)
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', palette.background)
}

// 在挂载应用前恢复本机外观；系统和其他窗口的更改也会即时应用。
export function initializeAppearance() {
  if (initialized) return
  initialized = true
  try { Object.assign(state, parseAppearance(window.localStorage.getItem(APPEARANCE_KEY))) }
  catch { appearanceSaved.value = false }
  const media = window.matchMedia('(prefers-color-scheme: dark)')
  systemDark.value = media.matches
  media.addEventListener('change', event => {
    systemDark.value = event.matches
    if (state.theme === 'system') applyAppearance()
  })
  window.addEventListener('storage', event => {
    if (event.key !== APPEARANCE_KEY && event.key !== null) return
    Object.assign(state, parseAppearance(event.newValue))
    appearanceSaved.value = true
    applyAppearance()
  })
  applyAppearance()
}

export function updateAppearance(patch: Partial<AppearanceSettings>) {
  Object.assign(state, normalizeAppearance({ ...state, ...patch }))
  applyAppearance()
  try {
    window.localStorage.setItem(APPEARANCE_KEY, JSON.stringify(state))
    appearanceSaved.value = true
  } catch { appearanceSaved.value = false }
}

export function resetAppearance() { updateAppearance(DEFAULT_APPEARANCE) }
