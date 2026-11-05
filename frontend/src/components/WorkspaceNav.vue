<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Activity, Bell, BookOpen, CalendarDays, ChartCandlestick, ChartNoAxesCombined, Clock3, FileText, FlaskConical, Globe2, LayoutDashboard, LockKeyhole, NotebookPen, RefreshCw, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, Star, Wallet, X } from 'lucide-vue-next'
import { workspaceGroup, workspaceSections } from '../workspaceNavigation'

const props = defineProps<{ current: string, open: boolean, admin: boolean, version: string }>()
const emit = defineEmits<{ navigate: [route: string], close: [], open: [] }>()
const navigation = ref<HTMLElement|null>(null)
const active = computed(() => workspaceGroup(props.current)?.id)
const icons = { Activity, Bell, BookOpen, CalendarDays, ChartCandlestick, ChartNoAxesCombined, Clock3, FileText, FlaskConical, Globe2, LayoutDashboard, LockKeyhole, NotebookPen, RefreshCw, Settings2, ShieldCheck, SlidersHorizontal, Sparkles, Star, Wallet, X }
const icon = (name: string) => (icons as any)[name]
const narrowViewport = window.matchMedia('(max-width: 760px)')
function viewportChanged() { if (!narrowViewport.matches && props.open) emit('close') }

watch(() => props.open, async open => {
  if (open) {
    await nextTick()
    requestAnimationFrame(focusCurrent)
  }
})
function focusCurrent() {
  if (props.open) (navigation.value?.querySelector<HTMLElement>('[aria-current="page"]') || navigation.value?.querySelector<HTMLElement>('button'))?.focus()
}
function keyboard(event: KeyboardEvent) {
  if (!props.open) return
  if (event.key === 'Escape') { event.preventDefault(); emit('close'); return }
  if (event.key !== 'Tab') return
  const buttons = Array.from(navigation.value?.querySelectorAll<HTMLElement>('a[href], button:not([disabled])') || []).filter(button => button.getClientRects().length)
  const first = buttons[0], last = buttons[buttons.length - 1]
  if (!buttons.includes(document.activeElement as HTMLElement)) { event.preventDefault(); first?.focus() }
  else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
onMounted(() => { window.addEventListener('keydown', keyboard); narrowViewport.addEventListener('change', viewportChanged) })
onBeforeUnmount(() => { window.removeEventListener('keydown', keyboard); narrowViewport.removeEventListener('change', viewportChanged) })
</script>

<template>
  <button v-if="open" class="workspace-nav-scrim" aria-label="关闭导航遮罩" tabindex="-1" @click="emit('close')"></button>
  <aside id="primary-workspace-navigation" ref="navigation" :class="['workspace-nav', { 'is-open': open }]" :role="open?'dialog':undefined" :aria-modal="open||undefined" aria-label="研究空间导航" @transitionend.self="focusCurrent">
    <div class="workspace-nav-brand"><a href="#dashboard" @click.prevent="emit('navigate', 'dashboard')"><icons.ChartNoAxesCombined :size="25"/><span>Stock<b>AI</b></span></a><button class="workspace-nav-close icon-button" aria-label="关闭导航" @click="emit('close')"><icons.X :size="19"/></button></div>
    <div class="workspace-nav-caption"><span class="status-dot"></span>个人研究空间</div>
    <nav class="workspace-nav-sections" aria-label="主导航">
      <section v-for="section in workspaceSections" :key="section.label" class="workspace-nav-section">
        <h2>{{ section.label }}</h2>
        <button v-for="item in section.items" :key="item.id" :class="{ active: active === item.id }" :aria-current="active === item.id ? 'page' : undefined" @click="emit('navigate', item.id)"><component :is="icon(item.icon)" :size="17"/><span>{{ item.label }}</span><span v-if="active === item.id" class="workspace-nav-active-mark"></span></button>
      </section>
      <section v-if="admin" class="workspace-nav-section workspace-nav-admin"><button :class="{ active: current === 'admin' }" :aria-current="current === 'admin' ? 'page' : undefined" @click="emit('navigate', 'admin')"><icons.LockKeyhole :size="17"/><span>站点管理</span></button></section>
    </nav>
    <footer class="workspace-nav-footer"><icons.LockKeyhole :size="13"/><span>个人资料独立保存</span><small v-if="version">v{{ version }}</small></footer>
  </aside>
  <nav v-if="!open" class="workspace-mobile-nav" aria-label="常用导航">
    <button type="button" :aria-current="active==='dashboard'?'page':undefined" @click="emit('navigate','dashboard')"><icons.LayoutDashboard :size="19"/><span>总览</span></button>
    <button type="button" :aria-current="active==='watchlist'?'page':undefined" @click="emit('navigate','watchlist')"><icons.ChartCandlestick :size="19"/><span>自选</span></button>
    <button type="button" :aria-current="active==='reports'||active==='candidates'?'page':undefined" @click="emit('navigate','reports')"><icons.FileText :size="19"/><span>研究</span></button>
    <button type="button" :aria-current="active==='simulation'?'page':undefined" @click="emit('navigate','simulation')"><icons.Wallet :size="19"/><span>组合</span></button>
    <button type="button" aria-label="打开更多导航" :aria-current="!['dashboard','watchlist','reports','candidates','simulation'].includes(active || '')?'page':undefined" :aria-expanded="open" aria-controls="primary-workspace-navigation" @click="emit('open')"><icons.Settings2 :size="19"/><span>更多</span></button>
  </nav>
</template>
