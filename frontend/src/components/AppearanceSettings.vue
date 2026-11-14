<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { Check, Monitor, Moon, Palette, RotateCcw, Sun } from 'lucide-vue-next'
import { accentPresets, appearance, appearanceSaved, resetAppearance, resolvedTheme, updateAppearance, type ThemeMode } from '../appearance'

const themes = [
  { id: 'light' as ThemeMode, name: '浅色', description: '暖灰底色与白色面板', icon: Sun },
  { id: 'dark' as ThemeMode, name: '深色', description: '适合夜间与低光环境', icon: Moon },
  { id: 'system' as ThemeMode, name: '跟随系统', description: '随设备外观自动切换', icon: Monitor },
]
const colorInput = ref(appearance.accent), invalidColor = ref(false)
watch(() => appearance.accent, value => { colorInput.value = value; invalidColor.value = false })
const colorName = computed(() => accentPresets.find(preset => preset.color === appearance.accent)?.name || '自定义')
function setCustomColor() {
  const value = colorInput.value.trim()
  invalidColor.value = !/^#[\da-f]{6}$/i.test(value)
  if (!invalidColor.value) { updateAppearance({ accent: value }); colorInput.value = appearance.accent }
}
function reset() { resetAppearance(); colorInput.value = appearance.accent; invalidColor.value = false }
</script>

<template>
  <section class="panel appearance-settings" aria-labelledby="appearance-title">
    <div class="panel-heading">
      <div><h2 id="appearance-title"><Palette :size="19" />主题与颜色</h2><p>即时生效，自动保存在当前浏览器或桌面窗口。</p></div>
      <button type="button" class="text-button appearance-reset" @click="reset"><RotateCcw :size="14" />恢复默认</button>
    </div>
    <div class="appearance-body">
      <fieldset class="appearance-fieldset">
        <legend>界面主题<span>当前为{{ resolvedTheme === 'dark' ? '深色' : '浅色' }}</span></legend>
        <div class="appearance-themes">
          <label v-for="theme in themes" :key="theme.id" class="appearance-theme" :class="{ selected: appearance.theme === theme.id }">
            <input type="radio" name="appearance-theme" :value="theme.id" :checked="appearance.theme === theme.id" @change="updateAppearance({ theme: theme.id })" />
            <span class="appearance-theme-preview" :class="'preview-' + theme.id" aria-hidden="true"><i /><span><b /><b /><b /></span></span>
            <span class="appearance-theme-name"><component :is="theme.icon" :size="16" /><strong>{{ theme.name }}</strong><Check v-if="appearance.theme === theme.id" :size="16" /></span>
            <small>{{ theme.description }}</small>
          </label>
        </div>
      </fieldset>
      <div class="appearance-color-layout">
        <div>
          <fieldset class="appearance-fieldset">
            <legend>主题色<span>{{ colorName }}</span></legend>
            <div class="appearance-swatches">
              <label v-for="preset in accentPresets" :key="preset.color" class="appearance-swatch" :class="{ selected: appearance.accent === preset.color }" :title="preset.name">
                <input type="radio" name="appearance-accent" :value="preset.color" :checked="appearance.accent === preset.color" :aria-label="preset.name" @change="updateAppearance({ accent: preset.color })" />
                <span :style="{ background: preset.color }"><Check v-if="appearance.accent === preset.color" :size="19" /></span>
              </label>
            </div>
          </fieldset>
          <div class="appearance-custom">
            <label class="appearance-picker">自定义<input type="color" :value="appearance.accent" aria-label="选择自定义主题色" @input="updateAppearance({ accent: ($event.target as HTMLInputElement).value })" /></label>
            <input v-model="colorInput" class="appearance-hex" aria-label="主题色十六进制值" :aria-invalid="invalidColor" :aria-describedby="invalidColor ? 'appearance-color-error' : undefined" maxlength="7" placeholder="#1b674c" spellcheck="false" @blur="setCustomColor" @keydown.enter.prevent="setCustomColor" />
          </div>
          <p v-if="invalidColor" id="appearance-color-error" class="appearance-error" role="alert">请输入 # 开头的六位十六进制颜色，如 #2563eb。</p>
          <fieldset class="appearance-fieldset appearance-market">
            <legend>行情涨跌色</legend>
            <label><input type="radio" name="appearance-market" value="red-up" :checked="appearance.marketColors === 'red-up'" @change="updateAppearance({ marketColors: 'red-up' })" />红涨绿跌</label>
            <label><input type="radio" name="appearance-market" value="green-up" :checked="appearance.marketColors === 'green-up'" @change="updateAppearance({ marketColors: 'green-up' })" />绿涨红跌</label>
            <small>同步应用于行情数值与 K 线图。</small>
          </fieldset>
        </div>
        <div class="appearance-live-preview" aria-label="当前外观预览">
          <div class="appearance-preview-heading"><span>效果预览</span><span class="tag">{{ colorName }}</span></div>
          <strong>行情与控件</strong>
          <p>预览主题色和行情涨跌色。</p>
          <div class="appearance-quote-preview"><span>上涨示例<b class="up">+2.36%</b></span><span>下跌示例<b class="down">−1.28%</b></span></div>
          <span class="appearance-sample-button"><Check :size="15" />当前主题色</span>
          <small>示例数值仅用于配色预览</small>
        </div>
      </div>
      <p class="appearance-save-status" :class="{ 'appearance-error': !appearanceSaved }" role="status">{{ appearanceSaved ? '外观设置已在本机保存' : '当前环境无法保存外观设置；本次选择仍已生效，关闭后可能恢复默认。' }}</p>
    </div>
  </section>
</template>

<style scoped>
.appearance-settings { margin-bottom: 26px; }
.appearance-settings h2 { display: flex; align-items: center; gap: 10px; }
.appearance-settings h2 svg { color: var(--accent-text); }
.appearance-settings .panel-heading { flex-wrap: wrap; }
.appearance-body { padding: 0 26px 22px; }
.appearance-fieldset { min-width: 0; margin: 0; padding: 0; border: 0; }
.appearance-fieldset legend { width: 100%; margin-bottom: 15px; padding: 0; font-size: 13px; font-weight: 600; color: var(--dark); }
.appearance-fieldset legend > span { margin-left: 12px; color: var(--muted); font-size: 12px; font-weight: 400; }
.appearance-themes { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 16px; }
.appearance-theme { position: relative; display: block; padding: 12px; border: 1px solid var(--border); border-radius: 10px; cursor: pointer; background: var(--surface); }
.appearance-theme.selected { border-color: var(--accent-text); box-shadow: 0 0 0 1px var(--accent-text); }
.appearance-theme > input, .appearance-swatch > input { position: absolute; width: 1px; height: 1px; min-height: 0; opacity: 0; }
.appearance-theme:focus-within, .appearance-swatch:focus-within { outline: 3px solid var(--accent-text); outline-offset: 4px; }
.appearance-theme-preview { display: flex; height: 84px; gap: 10px; border: 1px solid #e3ebe6; border-radius: 5px; overflow: hidden; background: #f5f4f0; }
.appearance-theme-preview > i { width: 24%; background: #fff; border-right: 1px solid #e3ebe6; }
.appearance-theme-preview > span { flex: 1; display: grid; gap: 7px; padding: 12px 12px 12px 0; }
.appearance-theme-preview b { height: 9px; width: 55%; border-radius: 2px; background: #c6d1c9; }
.appearance-theme-preview b:first-child { width: 35%; background: var(--accent); }
.appearance-theme-preview b:last-child { height: 22px; width: 100%; background: #fff; }
.preview-dark { background: #101816; border-color: #31423b; }
.preview-dark > i { background: #18211e; border-color: #31423b; }
.preview-dark b { background: #3d5248; }
.preview-dark b:last-child { background: #212b27; }
.preview-system { background: linear-gradient(110deg, #f5f4f0 50%, #101816 50%); }
.preview-system b:last-child { background: linear-gradient(110deg, #fff 44%, #212b27 44%); }
.appearance-theme-name { display: flex; align-items: center; gap: 7px; margin: 12px 0 5px; color: var(--dark); font-size: 13px; }
.appearance-theme-name > svg:last-child:not(:first-child) { margin-left: auto; color: var(--accent-text); }
.appearance-theme > small { display: block; color: var(--muted); font-size: 12px; }
.appearance-color-layout { display: grid; grid-template-columns: 1.2fr 1fr; gap: 32px; margin-top: 26px; padding-top: 25px; border-top: 1px solid var(--border); }
.appearance-swatches { display: flex; flex-wrap: wrap; gap: 12px; }
.appearance-swatch { display: block; position: relative; padding: 3px; border: 1px solid transparent; border-radius: 50%; cursor: pointer; }
.appearance-swatch.selected { border-color: var(--accent-text); }
.appearance-swatch > span { display: grid; place-items: center; width: 32px; height: 32px; border-radius: 50%; color: #fff; }
.appearance-custom { display: flex; align-items: center; gap: 12px; margin-top: 18px; }
.appearance-picker { display: flex; align-items: center; gap: 10px; color: var(--muted); font-size: 12px; }
.appearance-picker input { width: 36px; height: 34px; min-height: 34px; padding: 3px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); cursor: pointer; }
.appearance-hex { width: 110px; padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px; background: var(--surface); font-family: monospace; }
.appearance-hex[aria-invalid=true] { border-color: var(--danger); }
.appearance-error { color: var(--danger); font-size: 12px; margin: 8px 0 0; }
.appearance-market { margin-top: 25px; }
.appearance-market legend { margin-bottom: 10px; }
.appearance-market label { display: inline-flex; align-items: center; gap: 7px; margin: 0 20px 9px 0; font-size: 13px; cursor: pointer; }
.appearance-market input { width: 16px; height: 16px; min-height: 0; margin: 0; accent-color: var(--accent); }
.appearance-market small { display: block; color: var(--muted); font-size: 12px; }
.appearance-live-preview { padding: 8px 0 8px 28px; border-left: 1px solid var(--border); }
.appearance-preview-heading { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; font-size: 12px; color: var(--muted); }
.appearance-live-preview > strong { display: block; font-size: 16px; font-weight: 500; color: var(--dark); }
.appearance-live-preview > p { margin: 9px 0 20px; font-size: 12px; color: var(--muted); }
.appearance-quote-preview { display: flex; gap: 30px; font-size: 11px; color: var(--muted); }
.appearance-quote-preview b { display: block; margin-top: 4px; font-size: 21px; font-weight: 600; font-variant-numeric: tabular-nums; }
.appearance-sample-button { display: inline-flex; align-items: center; gap: 7px; margin-top: 22px; padding: 9px 14px; border-radius: 6px; background: var(--accent); color: var(--on-accent); font-size: 12px; font-weight: 600; }
.appearance-live-preview > small { display: block; margin-top: 13px; font-size: 11px; color: var(--muted); }
.appearance-save-status { margin: 22px 0 0; font-size: 12px; color: var(--muted); }
.appearance-save-status.appearance-error { color: var(--danger); }
@media (max-width: 700px) {
  .appearance-body { padding: 0 18px 20px; }
  .appearance-themes { gap: 10px; }
  .appearance-theme { padding: 9px; }
  .appearance-theme-preview { height: 60px; }
  .appearance-theme > small { display: none; }
  .appearance-theme-name { gap: 5px; font-size: 12px; flex-wrap: wrap; }
  .appearance-color-layout { grid-template-columns: 1fr; gap: 22px; }
  .appearance-live-preview { padding: 22px 0 0; border-left: 0; border-top: 1px solid var(--border); }
  .appearance-swatches { gap: 9px; }
}
</style>
