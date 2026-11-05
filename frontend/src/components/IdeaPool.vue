<script setup lang="ts">
// 想法池：一行一条，对应已有证券。空池时不启动评审。
import { onMounted, ref } from 'vue'
import { Trash2 } from 'lucide-vue-next'
import { api } from '../api'

defineProps<{ canEdit: boolean }>()
const items = ref<any[]>([])
const watchlist = ref<any[]>([])
const error = ref('')
const busy = ref(false)
const form = ref({ instrument_id: '', sentence: '', entry: '', exit: '', wrong: '', counterparty: '拥挤之后的回归', parameters: '5, 20', max_risk: '', sensitive_to: '' })
const counterparties = ['被动抛售或强平', '拥挤之后的回归', '流动性变薄造成的过度波动', '大事前后的情绪错杀']
const series = ['', '黄金', '白银', '观察指数', '新公告', '已过期大事']

async function load() {
  items.value = await api('/ideas')
  const rows = await api<any[]>('/watchlist')
  watchlist.value = rows
  if (!form.value.instrument_id && rows[0]) form.value.instrument_id = rows[0].id
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
  <div class="info-strip">一行一条。池子是空的时候，早报只说明任务和不会做的事，三关不启动。评审不下单，也不给买卖数量。</div>
  <p v-if="error" class="data-status warning">{{ error }}</p>
  <section v-if="!items.length" class="panel large-empty">
    <h2>想法池还是空的</h2>
    <p>任务是评审已有想法并写明过关或死因。不下单、不给买卖数量、不编写自动交易、不保存行情或交易所密钥。</p>
  </section>
  <section v-for="item in items" :key="item.id" class="panel idea-row">
    <div>
      <h2>{{ item.sentence }}</h2>
      <p>{{ item.instrument_id }} · {{ item.counterparty || '未写对手盘' }}</p>
      <p>进场：{{ item.entry || '未写' }}</p>
      <p>离场：{{ item.exit || '未写' }}</p>
      <p>错在哪：{{ item.wrong || '未写' }}</p>
    </div>
    <button class="icon-button" :disabled="!canEdit" aria-label="删除想法" @click="remove(item.id)"><Trash2 :size="15" /></button>
  </section>
  <form class="panel idea-form" @submit.prevent="save">
    <h2>写入一条想法</h2>
    <div class="form-grid">
      <label>证券<select v-model="form.instrument_id" required><option v-for="row in watchlist" :key="row.id" :value="row.id">{{ row.name }} {{ row.symbol }}</option></select></label>
      <label>对手盘<select v-model="form.counterparty"><option v-for="name in counterparties" :key="name">{{ name }}</option></select></label>
      <label class="span-two">一句话<input v-model="form.sentence" maxlength="200" required placeholder="用一句话写下这个想法" /></label>
      <label class="span-two">什么条件进场<input v-model="form.entry" maxlength="200" required placeholder="例如：收盘价上穿20日均线" /></label>
      <label class="span-two">什么条件离场<input v-model="form.exit" maxlength="200" required placeholder="例如：收盘价跌破20日均线" /></label>
      <label class="span-two">错在哪<input v-model="form.wrong" maxlength="200" required placeholder="例如：收盘价跌破前低" /></label>
      <label>数值参数<input v-model="form.parameters" placeholder="最多三个，用逗号分开" /></label>
      <label>对照项<select v-model="form.sensitive_to"><option v-for="name in series" :key="name" :value="name">{{ name || '不指定' }}</option></select></label>
      <label class="span-two">最大风险<input v-model="form.max_risk" maxlength="200" placeholder="不填则记为未知" /></label>
    </div>
    <button class="primary" :disabled="!canEdit || busy">保存到想法池</button>
  </form>
</template>

<style scoped>
.idea-row { display: flex; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.idea-row h2 { font-size: 16px; margin-bottom: 6px; }
.idea-row p { margin: 0 0 4px; color: var(--muted); }
.idea-form { display: grid; gap: 16px; }
.idea-form h2 { font-size: 16px; }
@media (max-width: 700px) {
  .idea-row { flex-direction: column; }
}
</style>
