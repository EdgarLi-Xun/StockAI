<script setup lang="ts">
import { computed, defineAsyncComponent, ref } from 'vue'
import { ArrowRight, ArrowUpRight, Bell, ChartCandlestick, Database, FileText, RefreshCw, ShieldCheck, Star } from 'lucide-vue-next'
import { number as fmt, percent as pct, direction, dateTime, preferHoldings, withLiveBar } from '../workspaceFormat'

const ResearchChart = defineAsyncComponent(() => import('./ResearchChart.vue'))
const props = defineProps<{ dashboard: any, indexData: any, indices: any[], activeIndex: string, canEdit: boolean, canMaintain: boolean, busy: boolean, publicMode: boolean }>()
const emit = defineEmits<{ navigate: [route: string], instrument: [id: string], report: [id: string], job: [kind: string], add: [], index: [id: string] }>()
const range = ref(30) // 总览默认只看最近 30 根，避免一屏塞进全部历史。
const rows = computed(() => preferHoldings((props.dashboard?.watchlist || []).filter((row: any) => row.asset_class !== 'index')).slice(0, 6))
const stats = computed(() => props.dashboard?.stats)
const currentIndex = computed(() => props.indices.find(row => row.id === props.activeIndex))
// 切换指数后，在对应行情到达前不展示上一指数的曲线。
const indexBars = computed(() => props.indexData?.instrument?.id === props.activeIndex ? props.indexData.bars || [] : [])
// 只取窗口尾部，30 期与 90 期共用同一份已对齐的 K 线。
const visibleBars = computed(() => withLiveBar(indexBars.value, currentIndex.value?.observation, '1d').slice(-range.value))
const modes: Record<string, string> = { standard: '标准版早报', full: '完整版复盘', closing: '收盘跟踪' }
// 分母无效时返回空，界面显示占位符，不把 0 只自选画成 0%。
const coverage = (count: unknown, total: unknown) => {
  // 缺数、非数字或总数为 0 都无法计算覆盖率。
  if (typeof count !== 'number' || typeof total !== 'number' || !Number.isFinite(count) || !Number.isFinite(total) || total <= 0) return null
  return Math.max(0, Math.min(100, Math.round(count / total * 100)))
}
// 行情覆盖和核验覆盖都按自选总数做分母，两根柱不互相替代。
const quoteCoverage = computed(() => coverage(stats.value?.quote_count, stats.value?.watch_count))
const verifiedCoverage = computed(() => coverage(stats.value?.verified_count, stats.value?.watch_count))
const attention = computed(() => {
  const items: { icon: typeof Database, title: string, note: string, route: string, action: string }[] = []
  if (!stats.value) return items
  const missing = Math.max(0, stats.value.watch_count - stats.value.quote_count)
  if (missing) items.push({ icon: Database, title: `${missing} 只证券缺少行情`, note: '补充来源与报价时间后再研究', route: props.publicMode ? 'readiness' : 'watchlist', action: '补充' })
  const unchecked = (props.dashboard.watchlist || []).filter((row: any) => !row.observation_only && row.exclusion_reasons?.length).length
  if (unchecked) items.push({ icon: ShieldCheck, title: `${unchecked} 只证券待核验`, note: '查看产品规则、公告与排除原因', route: props.publicMode ? 'readiness' : 'watchlist', action: '去核验' })
  const unfinished = (props.dashboard.jobs || []).filter((job: any) => ['failed', 'partial', 'interrupted'].includes(job.status)).length
  if (unfinished) items.push({ icon: RefreshCw, title: `${unfinished} 项近期任务需处理`, note: '查看执行结果与日志', route: 'jobs', action: '查看' })
  const unread = (props.dashboard.notifications || []).filter((notice: any) => !notice.read).length
  if (unread) items.push({ icon: Bell, title: `${unread} 条未读任务通知`, note: '从任务中心查看对应执行结果', route: 'jobs', action: '查看' })
  return items.slice(0, 4)
})
// 准备度只展示覆盖率，不把候选名额算进核验进度。
const readiness = computed(() => [
  { label: '行情覆盖', value: quoteCoverage.value, count: stats.value?.quote_count, tone: 'mint' },
  { label: '核验覆盖', value: verifiedCoverage.value, count: stats.value?.verified_count, tone: 'sand' },
])
</script>

<template>
  <div class="research-overview">
    <div class="overview-stat-grid" aria-label="研究概况">
      <article class="overview-stat">
        <div class="stat-label"><span>自选证券</span><Star :size="15"/></div>
        <div class="stat-body">
          <div class="stat-number">{{ stats?.watch_count ?? '—' }}<small>只证券</small></div>
          <button class="stat-link" @click="emit('navigate','watchlist')">查看我的自选<ArrowUpRight :size="14"/></button>
        </div>
        <p class="stat-note"><span class="stat-note-dot"></span>持仓优先 · 集中跟踪</p>
      </article>
      <article class="overview-stat">
        <div class="stat-label"><span>行情覆盖</span><Database :size="15"/></div>
        <div class="stat-body">
          <div class="stat-number">{{ quoteCoverage === null ? '—' : quoteCoverage + '%' }}</div>
          <div class="stat-progress" aria-hidden="true"><i :style="{width:(quoteCoverage ?? 0)+'%'}"></i></div>
        </div>
        <p class="stat-note">{{ stats ? (stats.quote_count ?? '—') + ' / ' + (stats.watch_count ?? '—') + ' 只已有报价' : '等待数据' }}</p>
      </article>
      <article class="overview-stat">
        <div class="stat-label"><span>核验通过</span><ShieldCheck :size="15"/></div>
        <div class="stat-body">
          <div class="stat-number">{{ verifiedCoverage === null ? '—' : verifiedCoverage + '%' }}</div>
          <div class="stat-progress sand" aria-hidden="true"><i :style="{width:(verifiedCoverage ?? 0)+'%'}"></i></div>
        </div>
        <p class="stat-note">{{ verifiedCoverage === null ? '等待核验' : (stats?.verified_count ?? '—') + ' / ' + (stats?.watch_count ?? '—') + ' 只证据有效' }}</p>
      </article>
      <article class="overview-stat stat-candidates">
        <div class="stat-label"><span>研究候选</span><ChartCandlestick :size="15"/></div>
        <div class="stat-body">
          <div class="stat-number">{{ stats?.candidate_count ?? '—' }}<small>/ 3 个名额</small></div>
          <div class="candidate-slots" aria-hidden="true"><span v-for="slot in 3" :key="slot" :class="{filled:slot<=(stats?.candidate_count || 0)}"></span></div>
        </div>
        <button class="stat-link stat-footer-link" @click="emit('navigate','candidates')">查看候选与依据<ArrowRight :size="13"/></button>
      </article>
    </div>

    <div class="overview-content-grid">
      <div class="overview-primary-column">
        <section class="overview-card pulse-card" aria-labelledby="market-heading">
        <header class="overview-card-heading">
          <h2 id="market-heading">市场指数</h2>
          <button class="overview-round-button" aria-label="进入市场行情" @click="emit('navigate','market')"><ArrowUpRight :size="18"/></button>
        </header>
        <div class="pulse-toolbar">
          <div class="pulse-index-tabs" aria-label="选择市场指数">
            <button v-for="index in indices" :key="index.id" :aria-pressed="activeIndex===index.id" @click="emit('index',index.id)">{{ index.name }}</button>
            <span v-if="!indices.length" class="muted">等待指数数据</span>
          </div>
          <div class="pulse-range" aria-label="显示范围"><button v-for="period in [30,90]" :key="period" :aria-pressed="range===period" @click="range=period">{{ period }} 期</button></div>
        </div>
        <div class="pulse-price"><strong>{{ fmt(currentIndex?.quote?.close) }}</strong><span :class="['pulse-change',direction(currentIndex?.change_pct)]">{{ pct(currentIndex?.change_pct) }}</span><small>{{ currentIndex?.quote?.kind==='spot' ? '最新价' : '最近收盘' }}</small></div>
        <ResearchChart v-if="visibleBars.length" :bars="visibleBars" kind="trend"/>
        <div v-else class="overview-blank pulse-blank"><span class="overview-empty-icon"><ChartCandlestick :size="25"/></span><h3>暂无指数行情</h3><p>同步行情后查看指数走势。</p><button v-if="canMaintain" class="text-button" :disabled="busy" @click="emit('job','sync')">同步市场行情<ArrowRight :size="14"/></button></div>
        <footer class="pulse-source"><span class="pulse-source-dot"></span>{{ currentIndex?.quote ? (currentIndex.quote.source || '来源未标注')+' · '+dateTime(currentIndex.quote.ts)+' · '+(currentIndex.quote.kind==='spot'?'最新价':'不复权收盘') : '数据来源与报价时间将在同步后显示' }}</footer>
      </section>
        <section class="overview-card holdings-card" aria-labelledby="holdings-heading">
      <header class="overview-card-heading"><h2 id="holdings-heading">我的自选 <span class="overview-caption">{{ rows.length ? '持仓优先 · 显示 '+rows.length+' 只' : '持仓优先' }}</span></h2><button class="text-button" @click="emit('navigate','watchlist')">管理自选<ArrowUpRight :size="14"/></button></header>
      <div v-if="rows.length" class="table-scroll"><table class="watch-table overview-table"><thead><tr><th>证券 / 代码</th><th class="number">最近价格</th><th class="number">涨跌幅</th><template v-if="publicMode"><th class="number">持仓价</th><th class="number">持仓数量</th><th class="number">持仓金额</th><th class="number">当天收益</th><th class="number">当天收益率</th><th class="number">总收益</th><th class="number">总收益率</th></template><th>研究状态</th></tr></thead><tbody>
        <tr v-for="row in rows" :key="row.id"><td><button class="security-name" @click="emit('instrument',row.id)">{{ row.name }}</button><small>{{ row.symbol }} · {{ row.market }}</small></td><td class="number mono">{{ fmt(row.quote?.close ?? row.quote?.price, row.asset_class === 'etf' ? 3 : 2) }}</td><td :class="['number mono',direction(row.change_pct)]">{{ pct(row.change_pct) }}</td><template v-if="publicMode"><td class="number mono">{{ fmt(row.position?.cost_price, 4) }}</td><td class="number mono">{{ row.position?.quantity ?? '—' }}</td><td class="number mono">{{ fmt(row.position?.market_value) }}</td><td :class="['number mono',direction(row.position?.daily_pnl)]">{{ fmt(row.position?.daily_pnl) }}</td><td :class="['number mono',direction(row.position?.daily_return_pct)]">{{ pct(row.position?.daily_return_pct) }}</td><td :class="['number mono',direction(row.position?.total_pnl)]">{{ fmt(row.position?.total_pnl) }}</td><td :class="['number mono',direction(row.position?.total_return_pct)]">{{ pct(row.position?.total_return_pct) }}</td></template><td><span :class="['tag',row.observation_only?'':row.exclusion_reasons?.length?'amber':'green']">{{ row.observation_only ? '仅观察' : row.exclusion_reasons?.length ? '待核验' : '已核验' }}</span></td></tr>
      </tbody></table></div>
      <div v-else class="overview-blank watchlist-blank"><span class="overview-empty-icon"><Star :size="25"/></span><div><h3>尚未添加自选</h3><p>添加证券，查看行情与持仓。</p></div><button v-if="canEdit" class="secondary" @click="emit('add')">添加自选<ArrowRight :size="14"/></button></div>
      <p v-if="rows.length && publicMode" class="overview-footnote">持仓收益按最近价与记录成本计算，不含费用；没有最近价时用已存日 K。缺失行情保留「—」。</p>
    </section>
      </div>
      <div class="overview-secondary-column">
        <section class="overview-card insights-card" aria-labelledby="insights-heading">
        <header class="overview-card-heading"><h2 id="insights-heading">研究提醒</h2><span class="overview-count">{{ attention.length }}</span></header>
        <div class="insight-list">
          <button v-for="item in attention" :key="item.title" class="insight-item" @click="emit('navigate',item.route)"><span class="insight-icon"><component :is="item.icon" :size="17"/></span><span class="insight-copy"><strong>{{ item.title }}</strong><small>{{ item.note }}</small></span><span class="insight-action">{{ item.action }}</span></button>
        </div>
        <div v-if="!attention.length" class="overview-blank"><span class="overview-empty-icon"><ShieldCheck :size="24"/></span><h3>{{ stats?.watch_count ? '暂无待处理事项' : '从第一只自选开始' }}</h3><p>{{ stats?.watch_count ? '行情缺失、核验与任务异常会显示在这里。' : '添加自选后，在这里关注数据与核验状态。' }}</p><button v-if="!stats?.watch_count && canEdit" class="text-button" @click="emit('add')">添加自选<ArrowRight :size="14"/></button></div>
      </section>
        <section class="overview-card readiness-card" aria-labelledby="readiness-heading">
        <header class="overview-card-heading"><h2 id="readiness-heading">研究准备度</h2><button class="overview-round-button" aria-label="查看数据与核验状态" @click="emit('navigate',publicMode?'readiness':'watchlist')"><ArrowUpRight :size="17"/></button></header>
        <div class="readiness-chart">
          <div v-for="item in readiness" :key="item.label" :class="['readiness-column',item.tone]">
            <div class="readiness-label"><span>{{ item.label }}</span><strong>{{ item.value===null?'—':item.value+'%' }}</strong></div>
            <div class="readiness-bar" role="meter" :aria-label="item.label" :aria-valuenow="item.value ?? undefined" aria-valuemin="0" aria-valuemax="100" :aria-valuetext="item.value===null?'暂无数据':item.value+'%'">
              <div class="readiness-bar-fill" :style="{width:(item.value ?? 0)+'%'}"></div>
            </div>
            <small>{{ item.count ?? '—' }} / {{ stats?.watch_count ?? '—' }} 只证券</small>
          </div>
        </div>
        <div class="readiness-footer"><span class="status-dot"></span>依据自选数据统计 · 不代表投资评级</div>
      </section>
        <section class="overview-card research-feed" aria-labelledby="reports-heading">
        <header class="overview-card-heading"><h2 id="reports-heading">最近研究</h2><button class="text-button" @click="emit('navigate','reports')">全部<ArrowUpRight :size="14"/></button></header>
        <button v-for="report in dashboard?.reports?.slice(0,3)" :key="report.id" class="research-feed-item" @click="emit('report',report.id)"><span class="feed-icon"><FileText :size="18"/></span><span class="feed-copy"><span class="feed-meta">{{ modes[report.mode] || report.mode }}<small>{{ report.report_date || dateTime(report.generated_at).slice(0,10) }}</small></span><strong>{{ report.summary }}</strong><small>{{ report.candidate_count ?? '—' }} 只候选 · 阅读报告</small></span><ArrowUpRight :size="15"/></button>
        <div v-if="!dashboard?.reports?.length" class="overview-blank"><span class="overview-empty-icon"><FileText :size="24"/></span><h3>暂无研究报告</h3><p>同步行情后，可生成首份报告。</p></div>
      </section>
      </div>
    </div>

    <section v-if="dashboard?.metals?.length" class="overview-metals-strip" aria-label="贵金属行情">
      <button class="metals-strip-label" @click="emit('navigate','metals')">黄金与白银<ArrowUpRight :size="14"/></button>
      <div v-for="metal in dashboard.metals.slice(0,2)" :key="metal.id" class="metals-strip-item"><span>{{ metal.id==='FX:XAUUSD'?'伦敦金现货':metal.id==='FX:XAGUSD'?'伦敦银现货':metal.name || metal.id }}</span><strong>{{ fmt(metal.price) }}</strong><small>{{ metal.quote_time ? dateTime(metal.quote_time) : '等待报价' }}{{ metal.quote_time && metal.stale?' · 已过期':'' }}</small></div>
      <small class="metals-strip-source">{{ dashboard.metals[0]?.source || '行情提供方' }} · 美元 / 金衡盎司</small>
    </section>
  </div>
</template>
