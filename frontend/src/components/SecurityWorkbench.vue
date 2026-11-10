<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { ArrowLeft, ArrowRight, ArrowUpRight, BookOpen, ChevronRight, FileInput, LoaderCircle, MessageSquare, RefreshCw, ShieldCheck } from 'lucide-vue-next'
import { number as fmt, percent as pct, direction, dateTime } from '../workspaceFormat'
const ResearchChart = defineAsyncComponent(() => import('./ResearchChart.vue'))
const props = defineProps<{ id: string, detail: any, loading: boolean, busy: boolean, watchlist: any[], timeframe: string, budget: any, canEdit: boolean, canMaintain: boolean, publicMode: boolean }>()
const emit = defineEmits<{
  close: [], select: [id: string], period: [period: string], sync: [], verify: [], announcements: [],
  'import-bars': [event: Event], estimate: [form: {price: number, quantity: number}],
  research: [view: string, context?: {kind: string, id: string}],
}>()
const tab = ref('chart'), indicator = ref('volume'), importFile = ref<HTMLInputElement>()
const budgetInput = ref({price: 0, quantity: 100})
const position = computed(() => props.watchlist.find(row => row.id === props.id)?.position)
const quote = computed(() => props.detail?.bars?.at(-1))
const currency = computed(() => ({SH:'CNY',SZ:'CNY',FUND:'CNY',SHFE:'CNY',US:'USD',HK:'HKD'} as Record<string,string>)[props.detail?.instrument.market] || props.detail?.observation?.currency || '')
const evidenceLabels: Record<string,string> = { listing_url:'上市与名称',status_url:'停复牌状态',announcement_url:'官方公告' }
const indicatorLabels: Record<string,string> = {ma5:'MA5',ma20:'MA20',ma60:'MA60',rsi14:'RSI14',atr14:'ATR14',volatility20:'20日年化波动率 %',support20:'20周期支撑参考',resistance20:'20周期阻力参考'}
const holdings = computed<{label:string,value:any,change?:unknown}[]>(() => [
  {label:'持仓价',value:fmt(position.value?.cost_price,4)}, {label:'持仓数量',value:position.value?.quantity ?? '—'},
  {label:'持仓金额',value:fmt(position.value?.market_value)}, {label:'当天收益',value:fmt(position.value?.daily_pnl),change:position.value?.daily_pnl},
  {label:'当天收益率',value:pct(position.value?.daily_return_pct),change:position.value?.daily_return_pct},
  {label:'总收益率',value:pct(position.value?.total_return_pct),change:position.value?.total_return_pct},
])
watch(() => props.id, () => { tab.value='chart'; indicator.value='volume' })
watch(() => props.detail?.instrument.id, id => {
  if (id) budgetInput.value={price: props.detail.bars?.at(-1)?.close || 0,quantity: props.detail.instrument.rules?.lot_size || 100}
}, {immediate:true})
</script>

<template>
  <section class="security-workbench" aria-label="证券研究工作区">
    <aside class="security-context-list" aria-label="工作台自选清单">
      <h2>我的自选 <small>{{ watchlist.length }}</small></h2>
      <div class="security-context-scroll">
        <button v-for="row in watchlist" :key="row.id" type="button" :aria-current="id===row.id?'page':undefined" @click="emit('select',row.id)"><span><strong>{{ row.name }}</strong><small :class="direction(row.change_pct)">{{ pct(row.change_pct) }}</small></span><span><small>{{ row.market }}:{{ row.symbol }}</small><small>{{ fmt(row.quote?.close ?? row.quote?.price,row.asset_class==='etf'?3:2) }}</small></span></button>
      </div>
      <p>行情、持仓与来源围绕当前证券展开。</p>
    </aside>
    <div class="security-research-main">
      <div class="security-toolbar"><button type="button" class="text-button" @click="emit('close')"><ArrowLeft :size="15"/>返回自选</button><label class="security-picker"><span>当前证券</span><select :value="id" aria-label="切换当前证券" @change="emit('select',($event.target as HTMLSelectElement).value)"><option v-for="row in watchlist" :key="row.id" :value="row.id">{{ row.name }} · {{ row.id }}</option></select></label><button v-if="publicMode" class="text-button" @click="emit('research','assistant')"><MessageSquare :size="15"/>AI 助手</button></div>
      <div v-if="!detail" class="panel security-loading" role="status"><LoaderCircle v-if="loading" class="spin" :size="24"/><ShieldCheck v-else :size="24"/><h2>{{ loading?'正在读取证券资料':'证券资料暂不可用' }}</h2><p>{{ loading?'加载已有行情、来源与核验记录。':'可重新读取，或返回自选检查数据来源。' }}</p><button v-if="!loading" class="secondary" @click="emit('select',id)">重新读取</button></div>
      <template v-else>
        <header class="security-header"><div><h1>{{ detail.instrument.name }}</h1><p>{{ detail.instrument.symbol }} · {{ detail.instrument.market }}<span v-if="currency"> · {{ currency }}</span><span v-if="detail.instrument.group_name"> · {{ detail.instrument.group_name }}</span></p></div><div class="security-quote"><strong>{{ fmt(quote?.close ?? detail.observation?.price,detail.instrument.asset_class==='etf'?3:2) }}</strong><small>{{ dateTime(quote?.ts || detail.observation?.quote_time) }}</small></div></header>
        <div class="security-status"><span :class="['tag',detail.risk_reasons.length?'amber':'green']">{{ detail.risk_reasons.length?'交易资格待核验':'已核验' }}</span><span>{{ detail.observation?.unit || '已完成 K 线' }} · {{ detail.observation?.source || quote?.source || '行情来源待补充' }}</span><button class="text-button" @click="tab='research'">查看依据<ChevronRight :size="13"/></button></div>
        <nav class="security-tabs" aria-label="证券研究内容"><button v-for="item in [{id:'chart',label:'行情与图表'},{id:'research',label:'研究与依据'},{id:'holding',label:'持仓与预算'}]" :key="item.id" type="button" :aria-current="tab===item.id?'page':undefined" @click="tab=item.id">{{ item.label }}</button></nav>
        <section v-show="tab==='chart'" class="panel security-chart-panel">
          <div class="security-chart-tools"><div class="segmented"><button v-for="period in ['1d','1w','60m','15m']" :key="period" type="button" :class="{selected:timeframe===period}" :aria-pressed="timeframe===period" :disabled="busy" @click="emit('period',period)">{{ {'1d':'日 K','1w':'周 K','60m':'60 分','15m':'15 分'}[period] }}</button></div><select v-model="indicator" aria-label="附图指标"><option value="volume">成交量</option><option value="macd">MACD</option><option value="rsi">RSI14</option><option value="atr">ATR14</option></select><button v-if="canMaintain" class="icon-button" :disabled="busy || !canEdit" aria-label="同步当前周期" @click="emit('sync')"><RefreshCw :size="16"/></button></div>
          <ResearchChart :bars="detail.bars || []" :indicators="detail.indicators?.series || []" :indicator="indicator"/>
          <div class="security-source"><span>{{ quote?.source || detail.observation?.source || '行情不可用' }} · {{ dateTime(quote?.ts || detail.observation?.quote_time) }} · {{ detail.observation?.timestamp_basis || (quote?.adjustment==='none'?'不复权':'复权口径待核验') }}</span><a v-if="quote?.source_url || detail.observation?.source_url" :href="quote?.source_url || detail.observation?.source_url" target="_blank" rel="noopener">来源证据<ArrowUpRight :size="13"/></a></div>
          <div v-if="publicMode" class="security-position-summary"><div><small>持仓成本</small><strong>{{ fmt(position?.cost_price,4) }} <small>{{ currency }}</small></strong></div><div><small>记录数量</small><strong>{{ position?.quantity ?? '—' }}</strong></div><div><small>总收益率 · 不含费用</small><strong :class="direction(position?.total_return_pct)">{{ pct(position?.total_return_pct) }}</strong></div></div>
        </section>
        <section v-show="tab==='chart'" class="panel security-next"><ShieldCheck :size="20"/><div><h2>{{ detail.risk_reasons.length ? '继续研究前，先核对证据' : '把当时的依据留下来' }}</h2><p>{{ detail.risk_reasons[0] || '行情与交易资格分别展示，研究结论保留原始来源。' }}</p></div><button class="text-button" @click="tab='research'">查看详情<ArrowRight :size="14"/></button></section>
        <div v-show="tab==='research'" class="security-panel-stack">
          <section class="panel security-evidence"><div class="panel-heading"><h2>交易资格与官方证据</h2><button v-if="canMaintain" class="text-button" :disabled="busy" @click="emit('verify')">核验记录<ArrowRight :size="14"/></button></div><div class="security-panel-body"><p v-for="reason in detail.risk_reasons" :key="reason" class="risk-line"><span class="tiny-dot amber"></span>{{ reason }}</p><p v-if="!detail.risk_reasons.length" class="green-text">已通过核验 · {{ dateTime(detail.instrument.verified_at) }}</p><a v-for="(url,key) in detail.instrument.evidence || {}" v-show="evidenceLabels[key] && typeof url==='string' && url.startsWith('https://')" :key="key" :href="String(url)" class="security-evidence-link" target="_blank" rel="noopener"><span>{{ evidenceLabels[key] }}</span><ArrowUpRight :size="14"/></a><p class="help-text">名称、上市状态、停复牌、公告与产品规则分别核验，缺失资料不代表没有风险。</p></div></section>
          <section class="panel"><div class="panel-heading"><h2>技术指标</h2><button v-if="canMaintain" class="text-button" :disabled="busy" @click="importFile?.click()"><FileInput :size="14"/>导入历史 JSON</button><input ref="importFile" hidden type="file" accept=".json" @change="emit('import-bars',$event)"/></div><div class="security-indicators"><div v-for="(label,key) in indicatorLabels" :key="key"><small>{{ label }}</small><strong>{{ fmt(detail.indicators?.latest?.[key]) }}</strong></div></div></section>
          <section class="panel"><div class="panel-heading"><h2>公告与风险记录</h2><button v-if="canMaintain" class="text-button" :disabled="busy" @click="emit('announcements')">同步官方公告<RefreshCw :size="14"/></button></div><div class="security-panel-body"><div v-for="event in detail.events" :key="event.id" class="event-row"><a :href="event.source_url" target="_blank" rel="noopener">{{ event.title }}</a><small>{{ dateTime(event.verified_at) }} · {{ event.event_type==='manual_official_review'?'人工官方核验':'公告采集记录' }}</small></div><p v-if="!detail.events?.length" class="help-text">暂无已归档公告，不能默认为没有风险。</p></div></section>
        </div>
        <div v-show="tab==='holding'" class="security-panel-stack">
          <section v-if="publicMode" class="panel"><div class="panel-heading"><h2>个人持仓记录</h2><span class="tag">按已存日 K 计算</span></div><div class="security-holdings"><div v-for="item in holdings" :key="item.label"><small>{{ item.label }}</small><strong :class="direction(item.change)">{{ item.value }}</strong></div></div><p class="overview-footnote">个人记录与模拟账户分别管理；缺行情时收益留空，不含费用。</p><div class="security-panel-body"><button class="secondary small" @click="emit('research','assistant',{kind:'instrument',id})"><MessageSquare :size="14"/>让助手准备持仓记录</button></div></section>
          <section v-if="['SH','SZ'].includes(detail.instrument.market) && ['stock','etf'].includes(detail.instrument.asset_class)" class="panel"><div class="panel-heading"><h2>含费预算试算</h2><small>不提交模拟委托</small></div><div class="security-panel-body"><form class="budget-form" @submit.prevent="emit('estimate',budgetInput)"><label>成交价格<input v-model.number="budgetInput.price" type="number" min="0.001" step="0.001" required/></label><label>模拟数量<input v-model.number="budgetInput.quantity" type="number" min="1" required/></label><button class="secondary" :disabled="busy">计算</button></form><div v-if="budget" class="budget-result"><div><span>成交金额</span><b>¥{{ fmt(budget.gross) }}</b></div><div><span>预计买入费用</span><b>¥{{ fmt(budget.fees.total) }}</b></div><div><span>含费总成本</span><b>¥{{ fmt(budget.total) }}</b></div><span :class="['tag',budget.within_budget?'green':'red']">{{ budget.within_budget?'通过预算门槛':'超预算，拒绝模拟买入' }}</span><p>{{ budget.fee_note }} {{ budget.eligible?'':'预算通过不等于交易资格通过。' }}</p><small>最低佣金敏感性：{{ budget.sensitivity.map((row:any)=>'佣金底限¥'+row.minimum_commission+' → 总成本¥'+row.total).join('；') }}</small></div></div></section>
        </div>
        <footer v-if="publicMode" class="security-research-actions"><button class="secondary small" @click="emit('research','journals',{kind:'instrument',id})"><BookOpen :size="14"/>记录研究判断</button><button class="secondary small" @click="emit('research','assistant',{kind:'instrument',id})"><MessageSquare :size="14"/>就此证券提问</button></footer>
      </template>
    </div>
  </section>
</template>
