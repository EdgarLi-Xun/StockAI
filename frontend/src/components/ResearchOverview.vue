<script setup lang="ts">
import { computed, defineAsyncComponent } from 'vue'
import { ArrowRight, ArrowUpRight, Bell, ChartCandlestick, Database, FileText, RefreshCw, ShieldCheck, Star } from 'lucide-vue-next'
import { number as fmt, percent as pct, direction, dateTime, preferHoldings } from '../workspaceFormat'
const ResearchChart = defineAsyncComponent(() => import('./ResearchChart.vue'))
const props = defineProps<{ dashboard: any, indexData: any, indices: any[], activeIndex: string, canEdit: boolean, canMaintain: boolean, busy: boolean, publicMode: boolean }>()
const emit = defineEmits<{ navigate: [route: string], instrument: [id: string], report: [id: string], job: [kind: string], add: [], index: [id: string] }>()
const rows = computed(() => preferHoldings((props.dashboard?.watchlist || []).filter((row: any) => row.asset_class !== 'index')).slice(0, 6))
const stats = computed(() => props.dashboard?.stats)
const currentIndex = computed(() => props.indices.find(row => row.id === props.activeIndex))
const modes: Record<string, string> = { standard: '标准版早报', full: '完整版复盘', closing: '收盘跟踪' }
const attention = computed(() => {
  const items: { icon: typeof Database, title: string, note: string, route: string, action: string }[] = []
  if (!stats.value) return items
  const missing = Math.max(0, stats.value.watch_count - stats.value.quote_count)
  if (missing) items.push({ icon: Database, title: `${missing} 只证券缺少行情`, note: '补充来源与报价时间后再研究', route: props.publicMode ? 'readiness' : 'watchlist', action: '检查' })
  const unchecked = (props.dashboard.watchlist || []).filter((row: any) => !row.observation_only && row.exclusion_reasons?.length).length
  if (unchecked) items.push({ icon: ShieldCheck, title: `${unchecked} 只证券待核验`, note: '查看产品规则、公告与排除原因', route: props.publicMode ? 'readiness' : 'watchlist', action: '核验' })
  const unfinished = (props.dashboard.jobs || []).filter((job: any) => ['failed', 'partial', 'interrupted'].includes(job.status)).length
  if (unfinished) items.push({ icon: RefreshCw, title: `${unfinished} 项近期任务需处理`, note: '保留原任务，查看失败原因与日志', route: 'jobs', action: '查看' })
  const unread = (props.dashboard.notifications || []).filter((notice: any) => !notice.read).length
  if (unread) items.push({ icon: Bell, title: `${unread} 条未读任务通知`, note: '从任务中心查看对应执行结果', route: 'jobs', action: '查看' })
  return items.slice(0, 4)
})
</script>

<template>
  <div class="overview-layout">
    <div class="stats-grid overview-metrics">
      <article class="metric-card"><div class="metric-label">自选证券</div><div class="metric-value">{{ stats?.watch_count ?? '—' }}<span>只</span></div><div class="metric-note">股票 / ETF / 跨市场</div></article>
      <article class="metric-card"><div class="metric-label">行情可用</div><div class="metric-value">{{ stats?.quote_count ?? '—' }}<span>/ {{ stats?.watch_count ?? '—' }}</span></div><div class="metric-note">已取得报价的证券</div></article>
      <article class="metric-card"><div class="metric-label">通过核验</div><div class="metric-value">{{ stats?.verified_count ?? '—' }}<span>只</span></div><div class="metric-note">证据有效且符合规则</div></article>
      <article class="metric-card accent-card"><div class="metric-label">研究候选</div><div class="metric-value">{{ stats?.candidate_count ?? '—' }}<span>/ 最多 3 只</span></div><button class="metric-link" @click="emit('navigate','candidates')">查看候选与依据<ArrowUpRight :size="13"/></button></article>
    </div>
    <div class="overview-columns">
      <div class="overview-primary">
        <section class="panel overview-watchlist">
          <div class="panel-heading"><h2>我的自选 <small>{{ stats?.watch_count ?? '—' }}</small></h2><button class="text-button" @click="emit('navigate','watchlist')">管理自选<ArrowUpRight :size="14"/></button></div>
          <div v-if="rows.length" class="table-scroll"><table class="watch-table overview-table"><thead><tr><th>证券 / 代码</th><th class="number">最近价格</th><th class="number">涨跌幅</th><template v-if="publicMode"><th class="number">持仓价</th><th class="number">持仓数量</th><th class="number">持仓金额</th><th class="number">当天收益</th><th class="number">当天收益率</th><th class="number">总收益</th><th class="number">总收益率</th></template><th>研究状态</th></tr></thead><tbody>
            <tr v-for="row in rows" :key="row.id"><td><button class="security-name" @click="emit('instrument',row.id)">{{ row.name }}</button><small>{{ row.symbol }} · {{ row.market }}</small></td><td class="number mono">{{ fmt(row.quote?.close ?? row.quote?.price, row.asset_class === 'etf' ? 3 : 2) }}</td><td :class="['number mono',direction(row.change_pct)]">{{ pct(row.change_pct) }}</td><template v-if="publicMode"><td class="number mono">{{ fmt(row.position?.cost_price, 4) }}</td><td class="number mono">{{ row.position?.quantity ?? '—' }}</td><td class="number mono">{{ fmt(row.position?.market_value) }}</td><td :class="['number mono',direction(row.position?.daily_pnl)]">{{ fmt(row.position?.daily_pnl) }}</td><td :class="['number mono',direction(row.position?.daily_return_pct)]">{{ pct(row.position?.daily_return_pct) }}</td><td :class="['number mono',direction(row.position?.total_pnl)]">{{ fmt(row.position?.total_pnl) }}</td><td :class="['number mono',direction(row.position?.total_return_pct)]">{{ pct(row.position?.total_return_pct) }}</td></template><td><span :class="['tag',row.observation_only?'':row.exclusion_reasons?.length?'amber':'green']">{{ row.observation_only ? '仅观察' : row.exclusion_reasons?.length ? '待核验' : '已核验' }}</span></td></tr>
          </tbody></table></div>
          <div v-else class="overview-empty"><Star :size="25"/><div><h3>尚未添加自选</h3><p>添加证券，查看行情与持仓。</p><button v-if="canEdit" class="text-button" @click="emit('add')">添加自选<ArrowRight :size="14"/></button></div></div>
          <p v-if="rows.length && publicMode" class="overview-footnote">持仓收益按已存日 K 与记录成本计算，不含费用；缺失行情保留「—」。</p>
        </section>
        <section class="panel market-panel overview-market">
          <div class="panel-heading"><h2>市场指数 <span class="small-badge">A 股</span></h2><button class="text-button" @click="emit('navigate','market')">进入行情<ArrowUpRight :size="14"/></button></div>
          <div class="index-tabs"><button v-for="index in indices" :key="index.id" :class="{selected:activeIndex===index.id}" :aria-pressed="activeIndex===index.id" @click="emit('index',index.id)"><span>{{ index.name }}</span><strong :class="direction(index.change_pct)">{{ fmt(index.quote?.close) }}<small>{{ pct(index.change_pct) }}</small></strong></button></div>
          <ResearchChart v-if="indexData?.bars?.length" :bars="indexData.bars" :indicators="indexData.indicators?.series || []"/>
          <div v-else class="overview-empty"><ChartCandlestick :size="25"/><div><h3>暂无指数行情</h3><p>同步行情后查看收盘走势。</p><button v-if="canMaintain" class="text-button" :disabled="busy" @click="emit('job','sync')">同步市场行情<ArrowRight :size="14"/></button></div></div>
          <div class="chart-foot">{{ currentIndex?.quote ? currentIndex.quote.source+' · '+dateTime(currentIndex.quote.ts)+' · 不复权收盘' : '数据来源与报价时间将在同步后显示' }}</div>
        </section>
      </div>
      <div class="overview-secondary">
        <section class="panel overview-attention"><div class="panel-heading"><h2>需要关注</h2><span v-if="attention.length" class="tag amber">{{ attention.length }} 类事项</span></div>
          <div v-for="item in attention" :key="item.title" class="attention-row"><component :is="item.icon" :size="16"/><div><strong>{{ item.title }}</strong><p>{{ item.note }}</p></div><button class="text-button" @click="emit('navigate',item.route)">{{ item.action }}</button></div>
          <div v-if="!attention.length" class="overview-empty"><ShieldCheck :size="24"/><div><h3>{{ stats?.watch_count ? '暂无待处理事项' : '尚无待办' }}</h3><p>{{ stats?.watch_count ? '行情缺失、核验与任务异常会显示在这里。' : '添加自选后，查看核验与同步状态。' }}</p></div></div>
        </section>
        <section class="panel overview-reports"><div class="panel-heading"><h2>最近研究</h2><button class="text-button" @click="emit('navigate','reports')">全部<ArrowUpRight :size="14"/></button></div>
          <button v-for="report in dashboard?.reports?.slice(0,3)" :key="report.id" class="overview-report" @click="emit('report',report.id)"><span class="overview-report-meta"><span class="tag">{{ modes[report.mode] || report.mode }}</span><small>{{ report.report_date || dateTime(report.generated_at).slice(0,10) }}</small></span><strong>{{ report.summary }}</strong><span class="overview-report-meta"><small>{{ report.candidate_count ?? '—' }} 只候选 · {{ dateTime(report.generated_at).slice(11) }}</small><span class="overview-read">阅读报告 →</span></span></button>
          <div v-if="!dashboard?.reports?.length" class="overview-empty"><FileText :size="24"/><div><h3>暂无研究报告</h3><p>同步行情后，可生成首份研究报告。</p></div></div>
        </section>
        <section class="panel overview-metals"><div class="panel-heading"><h2>黄金与白银</h2><button class="text-button" aria-label="贵金属专题" @click="emit('navigate','metals')"><ArrowUpRight :size="15"/></button></div><div v-for="metal in dashboard?.metals?.slice(0,2)" :key="metal.id" class="overview-metal"><div><strong>{{ metal.id==='FX:XAUUSD'?'伦敦金现货':metal.id==='FX:XAGUSD'?'伦敦银现货':metal.name || metal.id }}</strong><small>{{ metal.id==='FX:XAUUSD'?'XAU / USD':metal.id==='FX:XAGUSD'?'XAG / USD':metal.id }}</small></div><div><b>{{ fmt(metal.price) }}</b><small>{{ metal.quote_time ? dateTime(metal.quote_time) : '等待报价时间' }}{{ metal.quote_time && metal.stale?' · 已过期':'' }}</small></div></div><p class="overview-footnote">{{ dashboard?.metals?.[0]?.source || '行情提供方' }} · 美元 / 金衡盎司，延迟以来源说明为准</p></section>
      </div>
    </div>
  </div>
</template>
