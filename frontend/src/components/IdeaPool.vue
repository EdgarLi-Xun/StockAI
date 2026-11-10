<script setup lang="ts">
// 想法池：一行一条，对应已有证券。空池时不启动评审。
import { computed, onMounted, ref } from 'vue'
import { NotebookPen, Trash2 } from 'lucide-vue-next'
import { api } from '../api'

defineProps<{ canEdit: boolean }>()
const emit = defineEmits<{ arm: [item: any] }>()
const items = ref<any[]>([])
const watchlist = ref<any[]>([])
const error = ref('')
const busy = ref(false)
const ready = ref(false)
const form = ref({ instrument_id: '', sentence: '', entry: '', exit: '', wrong: '', counterparty: '拥挤之后的回归', parameters: '5, 20', max_risk: '', sensitive_to: '' })
const counterparties = ['被动抛售或强平', '拥挤之后的回归', '流动性变薄造成的过度波动', '大事前后的情绪错杀']
const series = ['', '黄金', '白银', '观察指数', '新公告', '已过期大事']
const rules = [
  { key: 'entry', label: '进场', placeholder: '例如：收盘价上穿20日均线' },
  { key: 'exit', label: '离场', placeholder: '例如：收盘价跌破20日均线' },
  { key: 'wrong', label: '错在哪', placeholder: '例如：收盘价跌破前低' },
] as const

const sorted = computed(() => [...items.value].reverse())

function instrumentOf(id: string) {
  return watchlist.value.find(row => row.id === id)
}

function parameterText(item: { parameters?: unknown }) {
  return Array.isArray(item.parameters) ? item.parameters.filter(value => value !== '' && value != null).join('、') : ''
}

function writtenAt(value: string) {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.replace('T', ' ').slice(0, 16)
  return new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(date)
}

async function load() {
  error.value = ''
  try {
    items.value = await api('/ideas')
    const rows = await api<any[]>('/watchlist')
    watchlist.value = rows
    if (!form.value.instrument_id && rows[0]) form.value.instrument_id = rows[0].id
  } catch (exc: any) {
    error.value = exc.message || '没有读到想法池'
  } finally {
    ready.value = true
  }
}

async function save() {
  error.value = ''
  busy.value = true
  try {
    const parameters = form.value.parameters.split(/[,，\s]+/).filter(Boolean).map(item => Number(item))
    await api('/ideas', { method: 'POST', body: JSON.stringify({ ...form.value, parameters, sensitive_to: form.value.sensitive_to }) })
    form.value.sentence = ''
    form.value.entry = ''
    form.value.exit = ''
    form.value.wrong = ''
    form.value.max_risk = ''
    await load()
  } catch (exc: any) {
    error.value = exc.message || '没有保存'
  } finally {
    busy.value = false
  }
}

async function remove(id: string) {
  error.value = ''
  await api('/ideas/' + id, { method: 'DELETE' })
  await load()
}

onMounted(load)
</script>

<template>
  <div class="info-strip"><NotebookPen :size="18" /><span>评审只判断过关或死因。不下单，不给买卖数量；池子是空的时候，三关不启动。</span></div>
  <p v-if="error" class="data-status warning">{{ error }}</p>
  <p v-if="!ready && !error" class="idea-loading">正在读取想法池</p>
  <div v-if="ready" class="idea-stack">
      <section v-if="!items.length" class="panel large-empty">
        <NotebookPen :size="28" />
        <h2>想法池还是空的</h2>
        <p>写下进场、离场和错在哪。早报只评审已经记下的想法。</p>
      </section>
      <section v-else class="panel idea-board">
        <div class="panel-heading">
          <div>
            <h2>已记下的想法 <span class="count-text">{{ items.length }}</span></h2>
            <p>每条对应一只已有证券。空着的条件不会被当成已经写明。</p>
          </div>
        </div>
        <article v-for="item in sorted" :key="item.id" class="idea-card">
          <header>
            <div class="idea-who">
              <strong>{{ instrumentOf(item.instrument_id)?.name || '未在观察池' }}</strong>
              <small>{{ instrumentOf(item.instrument_id)?.symbol || item.instrument_id }}</small>
            </div>
            <span class="tag">{{ item.counterparty || '未写对手盘' }}</span>
            <button class="text-button" :disabled="!canEdit" @click="emit('arm', item)">预填价格规则</button>
            <button class="icon-button" :disabled="!canEdit" aria-label="删除想法" @click="remove(item.id)"><Trash2 :size="15" /></button>
          </header>
          <h3>{{ item.sentence }}</h3>
          <div class="idea-rules">
            <div><small>进场</small><p>{{ item.entry || '未写' }}</p></div>
            <div><small>离场</small><p>{{ item.exit || '未写' }}</p></div>
            <div><small>错在哪</small><p>{{ item.wrong || '未写' }}</p></div>
          </div>
          <footer>
            <span v-if="parameterText(item)" class="tag">参数 {{ parameterText(item) }}</span>
            <span class="tag">{{ item.sensitive_to || '不对照' }}</span>
            <span :class="['tag', { amber: !item.max_risk || item.max_risk === '未知' }]">风险 {{ item.max_risk || '未知' }}</span>
            <time v-if="writtenAt(item.created_at)">{{ writtenAt(item.created_at) }}</time>
          </footer>
        </article>
      </section>

    <form class="panel idea-form" @submit.prevent="save">
      <div class="panel-heading">
        <div>
          <h2>写入一条想法</h2>
          <p>一句话，加上进场、离场和错在哪。</p>
        </div>
      </div>
      <fieldset class="idea-fields" :disabled="!canEdit || busy">
        <p class="idea-group">对应证券</p>
        <label>证券<select v-model="form.instrument_id" required><option v-if="!watchlist.length" value="" disabled>观察池还是空的</option><option v-for="row in watchlist" :key="row.id" :value="row.id">{{ row.name }} {{ row.symbol }}</option></select></label>
        <label>对手盘<select v-model="form.counterparty"><option v-for="name in counterparties" :key="name">{{ name }}</option></select></label>
        <p class="idea-group">这条想法</p>
        <label class="span-two">一句话<input v-model="form.sentence" maxlength="200" required placeholder="用一句话写下这个想法" /></label>
        <label v-for="rule in rules" :key="rule.key" class="span-two">{{ rule.label }}<input v-model="form[rule.key]" maxlength="200" required :placeholder="rule.placeholder" /></label>
        <p class="idea-group">可以后补</p>
        <label>数值参数<input v-model="form.parameters" placeholder="最多三个，用逗号分开" /></label>
        <label>对照项<select v-model="form.sensitive_to"><option v-for="name in series" :key="name" :value="name">{{ name || '不指定' }}</option></select></label>
        <label class="span-two">最大风险<input v-model="form.max_risk" maxlength="200" placeholder="不填则记为未知" /></label>
      </fieldset>
      <div class="form-actions">
        <small>{{ watchlist.length ? '保存后才会进入早报评审。' : '先到观察池添加证券，再写想法。' }}</small>
        <button class="primary" :disabled="!canEdit || busy || !watchlist.length">{{ busy ? '正在保存' : '保存到想法池' }}</button>
      </div>
    </form>
  </div>
</template>

<style scoped>
.idea-loading { margin: 0 0 18px; color: var(--muted); font-size: 13px; }
.idea-card { padding: 18px 24px 16px; border-top: 1px solid var(--border); }
.idea-card header { display: flex; align-items: center; gap: 10px; }
.idea-who { flex: 1; min-width: 0; }
.idea-who strong { display: block; font-size: 13px; font-weight: 600; color: var(--dark); }
.idea-who small { display: block; margin-top: 3px; color: var(--muted); font-size: 12px; }
.idea-card h3 { margin: 14px 0 12px; font-size: 16px; font-weight: 650; letter-spacing: -.2px; line-height: 1.55; color: var(--dark); overflow-wrap: anywhere; }
.idea-rules { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 10px; }
.idea-rules > div { min-width: 0; padding: 12px; border: 1px solid color-mix(in srgb, var(--green) 18%, var(--border)); border-radius: 8px; background: color-mix(in srgb, var(--green) 7%, var(--surface, #fff)); }
.idea-rules small { display: block; margin-bottom: 6px; color: var(--green); font-size: 11px; font-weight: 650; }
.idea-rules p { margin: 0; color: var(--dark); font-size: 13px; line-height: 1.65; overflow-wrap: anywhere; }
.idea-card footer { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 12px; }
.idea-card footer time { margin-left: auto; color: var(--muted); font-size: 12px; }
.idea-fields { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin: 0; padding: 0 24px 6px; border: 0; min-width: 0; }
.idea-fields label { display: grid; gap: 7px; color: var(--muted); font-size: 12px; }
.idea-fields .span-two { grid-column: 1 / -1; }
.idea-fields :is(input, select) { width: 100%; }
.idea-group { grid-column: 1 / -1; margin: 10px 0 0; color: var(--dark); font-size: 13px; font-weight: 650; }
.idea-group:first-child { margin-top: 0; }
.idea-form .form-actions { justify-content: space-between; align-items: center; }
.idea-form .form-actions small { margin-right: auto; color: var(--muted); font-size: 12px; line-height: 1.6; }
@media (max-width: 700px) {
  .idea-card { padding-inline: 16px; }
  .idea-card header { align-items: flex-start; }
  .idea-card footer time { margin-left: 0; }
  .idea-rules { grid-template-columns: 1fr; }
  .idea-fields { grid-template-columns: 1fr; padding-inline: 16px; }
  .idea-fields .span-two { grid-column: auto; }
  .idea-form .form-actions { align-items: stretch; flex-direction: column; padding-inline: 16px; }
  .idea-form .form-actions .primary { width: 100%; }
}
</style>
