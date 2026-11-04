<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { ArrowLeft, Expand, History, LoaderCircle, MessageSquare, Minus, Plus } from 'lucide-vue-next'

const props = defineProps<{ user: any, fullPage: boolean, context?: {kind:string,id:string,report_id?:string}|null }>()
const emit = defineEmits<{ navigate: [name:string], 'clear-context': [] }>()
const ResearchWorkbench = defineAsyncComponent(() => import('../ResearchWorkbench.vue'))
const client = ref<any>(null), launcher = ref<HTMLButtonElement|null>(null), container = ref<HTMLElement|null>(null)
const initialized = ref(props.fullPage), opened = ref(false), historyOpen = ref(false), unread = ref(false), blocked = ref(false)
const viewportStyle=ref<Record<string,string>>({})
const shortViewport=ref(false)
const visible = computed(() => props.fullPage || opened.value)
const pending = computed(() => !!client.value?.pending)
const dirty = computed(() => !!client.value?.dirty)
const messageVersion = computed(() => client.value?.messageVersion || '')
const status = computed(() => pending.value ? '正在生成' : unread.value ? '有新回复' : 'AI 助手')
let observer: MutationObserver|undefined, previousPage = 'dashboard'

async function open() {
  initialized.value = true; opened.value = true; unread.value = false
  await nextTick(); client.value?.focusQuestion()
}
async function collapse() {
  opened.value = false
  await nextTick(); launcher.value?.focus()
}
function navigate(name:string) { emit('navigate', name) }
function expand() {
  previousPage = location.hash.slice(1) || 'dashboard'
  const session = client.value?.sessionId
  emit('navigate', 'assistant' + (session ? '?session=' + encodeURIComponent(session) : ''))
}
function leavePage() { opened.value = true; emit('navigate', previousPage.startsWith('assistant') ? 'dashboard' : previousPage) }
function escape(event:KeyboardEvent) {
  if (event.key === 'Escape' && opened.value && !props.fullPage && !blocked.value) { event.preventDefault(); void collapse() }
}
// 业务弹窗优先，助手只暂时隐藏；草稿、会话和后台任务仍保留。
function checkDialogs() {
  blocked.value = Array.from(document.querySelectorAll<HTMLElement>('.modal-scrim')).some(el => el.getClientRects().length > 0)
}
function updateViewport(){const v=window.visualViewport;shortViewport.value=(v?.height||window.innerHeight)<650;if(v)viewportStyle.value={'--assistant-viewport-height':v.height+'px','--assistant-keyboard-inset':Math.max(0,window.innerHeight-v.height-v.offsetTop)+'px'}}
watch(messageVersion, (value, old) => { if (value && old && value !== old && !visible.value && !pending.value) unread.value = true })
watch(visible, value => { if (value) { initialized.value = true; unread.value = false; void client.value?.reload() } })
watch(client, async value => { if (value && visible.value) { await nextTick(); value.focusQuestion() } })
watch(() => props.context, value => { if (value) void open() })
watch(() => props.fullPage, async () => {
  const messages = container.value?.querySelector<HTMLElement>('.assistant-messages')
  const position = messages?.scrollTop || 0
  await nextTick()
  if (messages) messages.scrollTop = position
})
onMounted(() => {
  window.addEventListener('keydown', escape)
  observer = new MutationObserver(checkDialogs)
  observer.observe(document.body, { childList:true, subtree:true })
  checkDialogs();updateViewport();window.visualViewport?.addEventListener('resize',updateViewport);window.visualViewport?.addEventListener('scroll',updateViewport)
})
onBeforeUnmount(() => { observer?.disconnect(); window.removeEventListener('keydown', escape);window.visualViewport?.removeEventListener('resize',updateViewport);window.visualViewport?.removeEventListener('scroll',updateViewport) })
defineExpose({ open, dirty })
</script>

<template>
  <section v-show="visible && !blocked" ref="container" :style="viewportStyle" :class="['assistant-host', fullPage ? 'assistant-host-page' : 'assistant-host-panel', {'assistant-short':shortViewport}]" :role="fullPage ? undefined : 'region'" aria-label="AI 助手对话面板" :inert="!visible || blocked">
    <header class="assistant-host-toolbar">
      <div><MessageSquare :size="18"/><strong>AI 助手</strong><span v-if="pending" role="status"><LoaderCircle :size="13" class="spin"/>正在生成</span></div>
      <div class="button-row">
        <button v-if="!fullPage" class="icon-button" :aria-pressed="historyOpen" aria-label="查看历史对话" @click="historyOpen=!historyOpen"><History :size="17"/></button>
        <button v-if="!fullPage" class="icon-button" aria-label="新建对话" @click="client?.newSession()"><Plus :size="18"/></button>
        <button v-if="!fullPage" class="icon-button" aria-label="在独立页面打开" @click="expand"><Expand :size="17"/></button>
        <button v-if="!fullPage" class="icon-button" aria-label="收起 AI 助手" @click="collapse"><Minus :size="19"/></button>
        <button v-else class="text-button" @click="leavePage"><ArrowLeft :size="15"/>返回页面并悬浮</button>
      </div>
    </header>
    <!-- 一个持续挂载的实例负责两种展示；收起、换页和展开都不会重新发送。 -->
    <ResearchWorkbench v-if="initialized" ref="client" view="assistant" :user="user" :context="context" :assistant-panel="!fullPage" :embedded="fullPage" :show-history="historyOpen" @navigate="navigate" @clear-context="emit('clear-context')"/>
  </section>
  <button v-show="!fullPage && !blocked" ref="launcher" :style="viewportStyle" class="assistant-launcher" :aria-label="'打开 AI 助手' + (pending ? '，正在生成' : unread ? '，有新回复' : '')" :aria-expanded="opened" @click="opened ? collapse() : open()">
    <LoaderCircle v-if="pending" :size="21" class="spin"/><MessageSquare v-else :size="21"/>
    <span>{{ opened ? '收起助手' : status }}</span><i v-if="unread" aria-hidden="true"/>
  </button>
</template>

<style>
.assistant-launcher{position:fixed;right:max(24px,env(safe-area-inset-right));bottom:calc(max(24px,env(safe-area-inset-bottom)) + var(--assistant-keyboard-inset,0px));z-index:58;min-width:124px;min-height:48px;display:flex;align-items:center;justify-content:center;gap:10px;border:1px solid #3e7956;border-radius:28px;background:#285f40;color:#fff;padding:12px 18px;box-shadow:0 6px 24px #183c2930;font:600 13px inherit;cursor:pointer}.assistant-launcher:hover{background:#1b4a30}.assistant-launcher i{width:8px;height:8px;border-radius:50%;background:#ffdc8b}.assistant-launcher:focus-visible{outline:3px solid #9ec6a9;outline-offset:3px}
.assistant-host-panel{position:fixed;z-index:59;right:max(24px,env(safe-area-inset-right));bottom:calc(max(24px,env(safe-area-inset-bottom)) + 60px + var(--assistant-keyboard-inset,0px));width:min(420px,calc(100% - 32px));height:min(650px,calc(var(--assistant-viewport-height,100dvh) - var(--userbar-height,64px) - 110px));display:flex;flex-direction:column;background:#fff;border:1px solid #cdded2;border-radius:14px;box-shadow:0 18px 60px #183c2930;overflow:hidden}.assistant-host-toolbar{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 14px;border-bottom:1px solid var(--border);background:#f5f9f5;flex-shrink:0}.assistant-host-toolbar>div:first-child{display:flex;align-items:center;gap:8px;min-width:0;color:#326448}.assistant-host-toolbar strong{font-size:14px;white-space:nowrap}.assistant-host-toolbar span{display:flex;align-items:center;gap:4px;font-size:11px;color:var(--muted)}.assistant-host-toolbar .button-row{gap:1px}.assistant-host-toolbar .icon-button{min-width:32px;min-height:36px}
.assistant-model-summary{font-size:11px;color:var(--muted);margin:0 0 10px;overflow-wrap:anywhere}.assistant-host-panel .assistant-model-summary{font-size:10px;margin-bottom:7px}
.assistant-host-panel.assistant-short .assistant-scope,.assistant-host-panel.assistant-short .assistant-help-index,.assistant-host-panel.assistant-short .assistant-compose-controls,.assistant-host-panel.assistant-short .assistant-context-picker{display:none}.assistant-host-panel.assistant-short .assistant-compose{padding:7px 10px}.assistant-host-panel.assistant-short .assistant-compose textarea{height:40px;min-height:40px}.assistant-host-panel.assistant-short .assistant-model-summary{white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:5px}.assistant-host-panel.assistant-short .context-chips{max-height:30px;margin-bottom:5px}.assistant-host-panel.assistant-short .assistant-messages{min-height:35px}.assistant-host-panel.assistant-short .assistant-sessions{max-height:90px}.assistant-host-panel.assistant-short .assistant-compose-footer{margin-top:4px}
.public-workspace .assistant-host-panel .research-shell{display:flex;flex-direction:column;align-items:stretch;overflow:hidden;min-height:0;flex:1;background:#fff}.assistant-host-panel .research-main{display:flex;flex:1;flex-direction:column;min-height:0;min-width:0;margin:0;overflow:hidden;padding:0!important}.assistant-host-panel .research-main>.public-message{font-size:12px;margin:6px 10px;padding:8px;max-height:80px;overflow:auto}.assistant-host-panel .assistant-layout{display:flex;flex:1;flex-direction:column;gap:0;min-height:0}.assistant-host-panel .assistant-sessions{flex-shrink:0;max-height:170px;overflow-y:auto;padding:10px;display:block;width:100%;border:0;border-bottom:1px solid var(--border);border-radius:0}.assistant-host-panel .assistant-sessions .assistant-session{display:flex;max-width:100%;width:100%;padding:8px}.assistant-host-panel .assistant-sessions .panel-heading{margin-bottom:8px}.assistant-host-panel .assistant-sessions form{width:100%}.assistant-host-panel .assistant-conversation{display:flex;flex:1;flex-direction:column;min-height:0;width:100%;border:0;border-radius:0}.assistant-host-panel .assistant-scope{padding:9px 12px;gap:7px}.assistant-host-panel .assistant-scope p{font-size:11px;line-height:1.5}.assistant-host-panel .assistant-help-index{padding:8px 12px;font-size:11px}.assistant-host-panel .assistant-messages{flex:1;min-height:60px;max-height:none;padding:12px;scroll-behavior:auto}.assistant-host-panel .assistant-welcome{margin:15px auto}.assistant-host-panel .assistant-orb{width:36px;height:36px;border-radius:10px;margin-bottom:6px}.assistant-host-panel .assistant-welcome h2{font-size:16px!important}.assistant-host-panel .assistant-welcome p,.assistant-host-panel .assistant-prose{font-size:12px;line-height:1.8}.assistant-host-panel .assistant-starters{margin:12px auto;gap:5px}.assistant-host-panel .assistant-starters button{font-size:12px;padding:8px;min-height:34px}.assistant-host-panel .assistant-compose{flex-shrink:0;padding:10px 12px}.assistant-host-panel .assistant-compose-controls{gap:8px;margin-bottom:8px}.assistant-host-panel .assistant-compose-controls label{font-size:11px}.assistant-host-panel .assistant-compose select{min-height:32px;font-size:11px;padding:5px}.assistant-host-panel .assistant-context-picker{gap:5px;margin-bottom:8px}.assistant-host-panel .assistant-context-picker label:first-child{width:85px}.assistant-host-panel .assistant-compose textarea{min-height:56px;height:56px;max-height:120px;font-size:12px}.assistant-host-panel .assistant-compose-footer{margin-top:6px}.assistant-host-panel .assistant-compose-footer small{font-size:10px}.assistant-host-panel .assistant-compose-footer .primary{min-height:34px;padding:0 12px}.assistant-host-panel .assistant-question{font-size:12px;padding:10px;margin:0;max-width:min(88%,420px)}.assistant-host-panel .assistant-answer{max-width:100%;padding-top:2px}.assistant-host-panel .assistant-answer-heading{font-size:12px}.assistant-host-panel .assistant-source{margin-left:0}.assistant-host-panel .context-chips{max-height:70px;overflow:auto;margin-bottom:6px}.assistant-host-page .assistant-host-toolbar{justify-content:flex-end;padding-inline:30px}.assistant-host-page .assistant-host-toolbar>div:first-child{margin-right:auto}
@media(max-width:600px){.assistant-launcher{right:16px;bottom:calc(16px + env(safe-area-inset-bottom) + var(--assistant-keyboard-inset,0px));min-height:46px}.assistant-host-panel{right:8px;bottom:calc(76px + env(safe-area-inset-bottom) + var(--assistant-keyboard-inset,0px));width:calc(100% - 16px);height:min(650px,calc(var(--assistant-viewport-height,100dvh) - var(--userbar-height,106px) - 92px - env(safe-area-inset-bottom)))}.assistant-host-toolbar{padding:7px 10px}.assistant-host-toolbar span{font-size:10px}.assistant-host-page .assistant-host-toolbar{padding-inline:14px}.assistant-host-page .assistant-host-toolbar .text-button{font-size:11px}.assistant-host-panel .assistant-compose-controls>.text-button{font-size:11px}}
</style>
