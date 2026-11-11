<script setup lang="ts">
// 共享研究界面：连接本地或个人公网 API，展示自选、报告、模拟交易和回测。
import { computed, defineAsyncComponent, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Activity, ArrowDownToLine, ArrowRight, ArrowUpRight, Bell, BookOpen, CalendarDays, ChartCandlestick, ChartNoAxesCombined, Check, ChevronDown, ChevronLeft, ChevronRight, CircleHelp, Clock3, Database, FileText, FlaskConical, Globe2, LayoutDashboard, ListFilter, LoaderCircle, LockKeyhole, Menu, MoreHorizontal, NotebookPen, Plus, RefreshCw, Search, Settings2, ShieldCheck, Sparkles, Star, Terminal, Trash2, Wallet, X, Zap } from 'lucide-vue-next'
import { api, post, download, setToken } from './api'
import { preferHoldings } from './workspaceFormat'
import ConfirmDialog from './components/ConfirmDialog.vue'
import AppearanceSettings from './components/AppearanceSettings.vue'
import ResearchOverview from './components/ResearchOverview.vue'
import IdeaPool from './components/IdeaPool.vue'
import IdeaReview from './components/IdeaReview.vue'
import SecurityWorkbench from './components/SecurityWorkbench.vue'
const props = defineProps<{ publicMode?: boolean, publicUser?: any }>()
const emit = defineEmits<{ 'open-settings': [], 'open-workbench': [name:string,context?:{kind:string,id:string,report_id?:string}], 'security-context': [security: {id:string,name:string}|null] }>()
const confirmText = ref('')
let confirmResolve: ((answer: boolean) => void) | null = null
// 将本机原生确认与公网可访问确认弹窗统一为异步结果。
function confirmForUser(message: string): Promise<boolean> { if (!props.publicMode) return Promise.resolve(window.confirm(message)); confirmText.value = message; return new Promise(resolve => { confirmResolve = resolve }) }
// 关闭确认弹窗并完成等待中的选择，随后清理回调。
function settleConfirm(answer: boolean) { confirmText.value = ''; confirmResolve?.(answer); confirmResolve = null }
// 按需加载图表组件，避免首次打开工作台时立即加载全部图表依赖。
const ResearchChart = defineAsyncComponent(() => import('./components/ResearchChart.vue'))
// 定义研究页面与导航图标，公网模式将设置入口指向个人偏好。
const nav = [
  { id: 'dashboard', name: '研究总览', icon: LayoutDashboard }, { id: 'watchlist', name: '自选证券', icon: Star }, { id: 'market', name: '市场行情', icon: ChartCandlestick }, { id: 'metals', name: '黄金与白银', icon: Globe2 },
  { id: 'candidates', name: '候选研究', icon: Sparkles }, { id: 'ideas', name: '想法池', icon: NotebookPen }, { id: 'reports', name: '研究报告', icon: FileText }, { id: 'simulation', name: '模拟交易', icon: Wallet }, { id: 'backtest', name: '回测实验', icon: FlaskConical }, { id: 'review', name: '周度复盘', icon: BookOpen }, { id: 'jobs', name: '任务中心', icon: Clock3 }, { id: 'settings', name: props.publicMode ? '个人偏好' : '系统设置', icon: Settings2 },
]
// 从地址哈希恢复当前研究页面，支持刷新和浏览器前进后退。
const page = ref(location.hash.slice(1).split('?')[0] || 'dashboard')
let handledHash = location.hash
const sidebarOpen = ref(false), loading = ref(true), ready = ref(false), busy = ref(false)
const error = ref(''), toast = ref(''), showNotifications = ref(false), requiresLogin = ref(false), role = ref('admin')
// 集中保存服务端快照，具体页面通过派生值和按需请求读取。
const dashboard = ref<any>(null), model = ref<any>({ available: false, message: '正在检查模型' }), config = ref<any>(null), runtime = ref<any>(null)
const reports = ref<any[]>([]), jobData = ref<any>({ jobs: [], logs: [], schedule: { plans: [] } }), portfolio = ref<any>({ accounts: [], orders: [], ledger: [], rules: [] }), experiments = ref<any[]>([]), backups = ref<any[]>([])
const taskStatus=ref(''),taskScope=ref(''),taskSearch=ref(''),selectedTaskKey=ref('')
const allTasks=computed<any[]>(()=>[...(jobData.value.jobs||[]).map((j:any)=>({...j,scope:'personal',key:'personal:'+j.id})),...(jobData.value.shared_jobs||[]).map((j:any)=>({...j,scope:'shared',key:'shared:'+j.id}))].sort((a,b)=>b.created_at.localeCompare(a.created_at)))
const visibleTasks=computed(()=>allTasks.value.filter(j=>(!taskStatus.value||j.status===taskStatus.value)&&(!taskScope.value||j.scope===taskScope.value)&&(!taskSearch.value||[modeNames[j.kind],j.id,j.error,j.logical_key].join(' ').toLowerCase().includes(taskSearch.value.toLowerCase()))))
const selectedTask=computed(()=>allTasks.value.find(j=>j.key===selectedTaskKey.value))
const taskLogs=computed<any[]>(()=>{
  const own=(jobData.value.logs||[]).map((l:any)=>({...l,scope:'personal'}))
  const shared=allTasks.value.filter(j=>j.scope==='shared').flatMap(j=>['created_at','started_at','finished_at'].filter(f=>j[f]).map(f=>({id:j.id+':'+f,job_id:j.id,scope:'shared',ts:j[f],level:f==='finished_at'&&j.status==='failed'?'error':'info',message:(modeNames[j.kind]||j.kind)+' · '+(f==='created_at'?'已加入队列':f==='started_at'?'开始执行':j.error||'已完成')})))
  return [...own,...shared].filter(l=>!selectedTask.value||(l.job_id===selectedTask.value.id&&l.scope===selectedTask.value.scope)).sort((a,b)=>b.ts.localeCompare(a.ts))
})
const selectedReport = ref<any>(null), selectedBacktest = ref<any>(null), detail = ref<any>(null), detailId = ref(''), timeframe = ref('1d'), lowerIndicator = ref('volume')
const detailLoading = ref(false)
const reportLoading = ref(false)
let detailRead = 0
let dashboardRead = 0
let indexRead = 0
let reportRead = 0
const search = ref(''), group = ref('全部'), statusFilter = ref('全部'), reportMode = ref(''), reportSearch = ref(''), activeIndex = ref('SH:000001'), accountTab = ref('orders')
const modal = ref(''), form = ref<any>({}), modalError = ref(''), budget = ref<any>(null), budgetForm = ref({ price: 0, quantity: 100 }), importFile = ref<HTMLInputElement>(), barsFile = ref<HTMLInputElement>()
async function guard(){return !modal.value||await confirmForUser('当前有未提交的编辑。确认离开并清除当前临时表单？')}
defineExpose({guard,searchSecurities,get dirty(){return !!modal.value}})
// 回测参数包含明确虚拟本金及严格证据模式，不推断用户真实资产。
const testParams = ref<any>({ instrument_id: 'SH:603126', initial_cash: 10000, fast: 5, slow: 20, train_ratio: 0.7, stop_loss_pct: 0.05, strict: true, start_date: '', end_date: '' })
let timer: ReturnType<typeof setInterval>, toastTimer: ReturnType<typeof setTimeout>
// 根据运行模式与会话角色控制个人数据编辑入口。
const canEdit = computed(() => props.publicMode || role.value === 'admin')
// 区分个人编辑权限与共享市场维护权限，公网维护要求站点管理员。
const canMaintain = computed(() => !props.publicMode ? canEdit.value : props.publicUser?.role === 'admin')
// 根据当前页面标识生成页面标题，未知标识回退至研究总览。
const title = computed(() => nav.find(n => n.id === page.value)?.name || '研究总览')
// 今日总览、自选证券和市场行情共用：已记录持仓在前，其次置顶收藏。
const watchlist = computed<any[]>(() => preferHoldings(dashboard.value?.watchlist || []))
const overviewWatchlist = computed(() => watchlist.value.filter(r => r.asset_class === 'stock' && ['SH', 'SZ'].includes(r.market)).slice(0, 5))
// 从自选中的个人分组去重生成筛选选项。
const groups = computed(() => ['全部', ...new Set(watchlist.value.map(r => r.group_name))])
// 组合分组、关键词和核验状态筛选，保持原始自选数据不变。
// 仅观察与待核验互斥，避免观察标的被算进待核验。
function matchesWatchStatus(row: any) {
  if (statusFilter.value === '全部') return true
  // 仅观察单独成档，不看排除原因。
  if (statusFilter.value === '仅观察') return !!row.observation_only
  // 待核验只含仍需处理的非观察证券。
  if (statusFilter.value === '待核验') return !row.observation_only && !!row.exclusion_reasons?.length
  // 其余视为已核验：非观察且没有排除原因。
  return !row.observation_only && !row.exclusion_reasons?.length
}
const filteredWatch = computed(() => watchlist.value.filter(r => (group.value === '全部' || r.group_name === group.value) && (!search.value || (r.symbol + r.name + r.group_name).toLowerCase().includes(search.value.toLowerCase())) && matchesWatchStatus(r)))
// 公网使用共享指数列表，本地从自选列表中提取指数。
const indices = computed(() => props.publicMode ? (dashboard.value?.indices || []) : watchlist.value.filter(r => r.asset_class === 'index'))
// 取得当前选择的指数，供行情概览卡使用。
const currentIndex = computed(() => indices.value.find((r: any) => r.id === activeIndex.value))
// 取得总览中的最近一份报告，供候选与摘要卡展示。
const latestReport = computed(() => dashboard.value?.reports?.[0])
// 按报告模式和日期或摘要关键词筛选归档列表。
const visibleReports = computed(() => reports.value.filter(r => (!reportMode.value || r.mode === reportMode.value) && (r.report_date + r.summary).includes(reportSearch.value)))
const metalNames: Record<string, string> = { 'FX:XAUUSD': '伦敦金现货', 'FX:XAGUSD': '伦敦银现货', 'SHFE:AU0': '沪金主力连续', 'SHFE:AG0': '沪银主力连续' }
const statuses: Record<string, string> = { unknown: '未核验', active: '正常', suspended: '停牌', delisted: '退市', queued: '排队中', running: '运行中', success: '已完成', failed: '失败', partial: '部分完成', interrupted: '已中断', missed: '错过执行', pending: '待成交', filled: '已成交', cancelled: '已撤销', rejected: '已拒绝', unavailable: '不可用' }
const modeNames: Record<string, string> = { standard: '标准版早报', full: '完整版复盘', closing: '收盘跟踪', sync: '全池数据同步', match: '模拟撮合', metals: '金银行情', backup: '数据备份', supplement:'报告补充 AI',profile_test:'模型连接测试',assistant:'AI 助手对话',experiment:'历史实验',names:'证券名称补全',instrument:'证券行情同步',announcements:'公告同步',context:'背景资料同步' }
// 将有效数值格式化为中文千分位，缺失值统一显示占位符。
const fmt = (v: any, digits = 2) => v === null || v === undefined || v === '' ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
// 统一以北京时间显示带时区时间，无法解析时保留原始文本。
const dt = (v: any) => !v ? '尚无记录' : Number.isNaN(new Date(v).getTime()) ? String(v).replace('T', ' ').slice(0, 19) : new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }).format(new Date(v))
// 格式化百分比并为正值补上加号，缺失值保留占位符。
const pct = (v: any) => v === null || v === undefined ? '—' : (Number(v) > 0 ? '+' : '') + Number(v).toFixed(2) + '%'
// 把数值涨跌映射为样式类名，零值不附加涨跌色。
const direction = (v: any) => Number(v) > 0 ? 'up' : Number(v) < 0 ? 'down' : ''
// 以服务端时间优先生成上海时区的中文日期和星期。
const dateText = computed(() => new Intl.DateTimeFormat('zh-CN', { year: 'numeric', month: 'long', day: 'numeric', weekday: 'long', timeZone: 'Asia/Shanghai' }).format(new Date(dashboard.value?.time || Date.now())))
// 总览标题跟着行情、核验和任务状态变化，未完成时不写成已经就绪。
const dashboardBrief = computed(() => {
  const stats = dashboard.value?.stats
  // 还没有自选时不宣称研究已就绪。
  if (!stats?.watch_count) return { title: '从第一只自选开始', lead: '添加自选后，这里汇总行情与核验。' }
  const missing = Math.max(0, (stats.watch_count || 0) - (stats.quote_count || 0))
  // 仅观察不计入待核验，与自选筛选保持同一口径。
  const unchecked = (dashboard.value.watchlist || []).filter((row: any) => !row.observation_only && row.exclusion_reasons?.length).length
  // 只把失败、部分完成和中断当成未完成，成功任务不拉低标题。
  const unfinished = (dashboard.value.jobs || []).filter((job: any) => ['failed', 'partial', 'interrupted'].includes(job.status)).length
  if (missing || unchecked || unfinished) return { title: '先处理待办，再开始研究', lead: '行情、核验或任务还有未完成项。' }
  return { title: '今日可以开始研究', lead: '行情与核验已对齐。' }
})
// 切换页面并同步地址哈希，清理侧栏与报告详情的临时状态。
function go(id: string) { if (props.publicMode && id === 'settings') { emit('open-settings'); return }; if(props.publicMode&&id==='backtest'){emit('open-workbench','experiments');return};page.value = id; location.hash = id; handledHash = location.hash; sidebarOpen.value = false; closeReport() }
// 响应浏览器哈希变化，只接受导航中已定义的页面。
function syncHash() {
  const hash = location.hash, id = hash.slice(1).split('?')[0]
  // 主页面相同时，浏览器返回列表仍需取消旧详情；内部导航已在 go 中记录。
  const reportRemoved = id === 'reports' && handledHash.slice(1).split('?')[0] === 'reports'
    && new URLSearchParams(handledHash.split('?')[1]).get('report')
    && !new URLSearchParams(hash.split('?')[1]).get('report')
  handledHash = hash
  if (nav.some(n => n.id === id) && id !== page.value) { page.value = id; closeReport(); sidebarOpen.value = false }
  else if (reportRemoved) closeReport()
  void openRouteContext()
}
// 公网会话就绪后解析报告或证券深链接，打开对应的归档详情。
async function openRouteContext(){if(!props.publicMode||!ready.value)return;const p=new URLSearchParams(location.hash.split('?')[1]);if(p.get('report')&&page.value==='reports')await showReport(p.get('report')!);if(p.get('instrument')&&page.value==='watchlist')await openDetail(p.get('instrument')!);if(p.get('backtest')&&page.value==='review')await action(async()=>selectedBacktest.value=await api('/backtests/'+p.get('backtest')))}
// 显示短时操作提示，重复触发时重置关闭计时器。
function notify(message: string) { toast.value = message; clearTimeout(toastTimer); toastTimer = setTimeout(() => toast.value = '', 5000) }
// 统一维护操作中的忙碌状态、错误展示与可选成功提示。
async function action(fn: () => Promise<any>, success = '') { busy.value = true; error.value = ''; try { const result = await fn(); if (success) notify(success); return result } catch (e: any) { error.value = e.message; return null } finally { busy.value = false } }
// 刷新研究总览，作为自选和首页指标的共享数据来源。
async function refreshBase() {
  const read = ++dashboardRead
  const next = await api('/dashboard')
  if (read === dashboardRead) dashboard.value = next
}
// 先改当前自选的置顶状态，避免等待整页总览；同时作废进行中的旧总览响应。
function applyPinned(id: string, pinned: boolean) {
  dashboardRead++
  const current = dashboard.value
  if (!current?.watchlist) return
  dashboard.value = { ...current, watchlist: current.watchlist.map((item: any) => item.id === id ? { ...item, pinned } : item) }
}
// 仅在会话就绪后加载当前页面需要的数据，避免无关请求。
async function loadPage() {
  if (!ready.value) return
  error.value = ''
  try {
    if (['reports', 'review', 'candidates'].includes(page.value)) reports.value = await api('/reports' + (page.value === 'review' ? '?mode=full' : ''))
    if (page.value === 'jobs') jobData.value = await api('/jobs')
    if (page.value === 'simulation' || page.value === 'review') portfolio.value = await api('/simulation')
    if (page.value === 'backtest' || page.value === 'review') experiments.value = await api('/backtests')
    if (page.value === 'settings') { const r = await api('/settings'); config.value = r.settings; runtime.value = r; backups.value = await api('/backups') }
    if (page.value === 'dashboard' || page.value === 'market') await loadIndex()
  } catch (e: any) { error.value = e.message }
}
const indexData = ref<any>(null)
// 只接纳当前选择的最新请求，切换时先清除上一指数的曲线。
async function loadIndex() {
  const id = activeIndex.value, read = ++indexRead, originPage = page.value
  // 序号、指数和页面任一变化，这次响应就作废。
  const current = () => read === indexRead && id === activeIndex.value && page.value === originPage
  // 标识对不上时先清空，避免切换瞬间仍画着上一只指数。
  if (indexData.value?.instrument?.id !== id) indexData.value = null
  error.value = ''
  try {
    const next = await api('/instruments/' + encodeURIComponent(id))
    if (current()) indexData.value = next
  } catch (e: any) {
    // 过期请求的错误也不覆盖当前页面的提示。
    if (current()) error.value = e.message
  }
}
// 按公网身份或本地令牌完成引导，并分别处理模型与设置加载失败。
async function initialize() {
  try {
    if (props.publicMode) {
      role.value = props.publicUser?.role || 'user'; requiresLogin.value = false
      await refreshBase(); model.value = await api('/model/health'); config.value = (await api('/settings')).settings
      if (page.value === 'settings') page.value = 'dashboard'
      ready.value = true; await loadPage(); await openRouteContext(); return
    }
    const s = await api('/session')
    if (s.requires_login && !sessionStorage.getItem('stockai-token')) { requiresLogin.value = true; return }
    if (s.token) { setToken(s.token); role.value = s.role }
    else role.value = sessionStorage.getItem('stockai-role') || 'reader'
    const result = await Promise.allSettled([refreshBase(), api('/model/health'), api('/settings')])
    if (result[0].status === 'rejected') { requiresLogin.value = true; throw result[0].reason }
    if (result[1].status === 'fulfilled') model.value = result[1].value
    if (result[2].status === 'fulfilled') { config.value = result[2].value.settings; runtime.value = result[2].value }
    ready.value = true
    await loadPage()
  } catch (e: any) { error.value = e.message } finally { loading.value = false }
}
// 验证局域网角色和密码，保存本页会话令牌后重新初始化。
async function login() { await action(async () => { const r = await post('/login', form.value); setToken(r.token); role.value = r.role; sessionStorage.setItem('stockai-role', r.role); requiresLogin.value = false; await initialize() }) }
// 提交后台任务并展示幂等复用或排队提示，然后刷新相关视图。
async function runJob(kind: string) { await action(async () => { const r = await post('/jobs', { kind }); notify(r.status === 'success' ? '当日任务已完成，沿用归档结果' : '任务已加入队列，可在任务中心查看进度'); await refreshBase(); if (page.value === 'jobs') await loadPage() }) }
async function retryTask(job:any){if(props.publicMode&&job.scope!=='shared'&&!await confirmForUser('使用当前配置'+(job.retry_label||'重新运行')+'？这会创建新任务；模型调用可能消耗额度，原任务与报告会保留。'))return;await action(async()=>{await post((job.scope==='shared'?'/admin/jobs/':'/jobs/')+job.id+'/retry');await loadPage();notify('已提交，重复操作会复用同一条重试任务')})}
function canRetryTask(job:any){return job.can_retry??['failed','partial','interrupted','missed'].includes(job.status)}
function taskResult(job:any){if(job.result)return job.result;try{return JSON.parse(job.result_json||'null')}catch{return null}}
function openTaskResult(job:any){location.hash=job.result_route;syncHash()}
// 清理旧详情并加载证券资料，用最新报价和最小申报量初始化预算表单。
async function openDetail(id: string, refresh = false) {
  if (!refresh && detailId.value === id && (detail.value || detailLoading.value)) return
  const read = ++detailRead
  detailId.value = id; detailLoading.value = true; detail.value = null; budget.value = null; timeframe.value = '1d'
  page.value = 'watchlist'
  if (props.publicMode) emit('open-workbench','watchlist?instrument='+encodeURIComponent(id))
  else location.hash = 'watchlist'
  await action(async () => {
    try {
      const result = await api('/instruments/' + encodeURIComponent(id))
      if (read !== detailRead) return
      detail.value = result
      budgetForm.value = {price: result.bars.at(-1)?.close || 0,quantity: result.instrument.rules.lot_size}
    } finally { if (read === detailRead) detailLoading.value = false }
  })
}
function closeDetail() {
  ++detailRead; detailId.value = ''; detail.value = null; detailLoading.value = false; budget.value = null
  if (props.publicMode && location.hash.startsWith('#watchlist?')) history.replaceState(null,'','#watchlist')
}
function searchSecurities(value: string) { closeDetail(); search.value = value }
async function changeTimeframe(tf: string) {
  const id=detailId.value,read=++detailRead
  timeframe.value=tf
  await action(async()=>{const result=await api('/instruments/'+encodeURIComponent(id)+'?timeframe='+tf);if(read===detailRead&&detailId.value===id)detail.value=result})
}
async function syncDetail() { await action(async()=>{const result=await post('/instruments/'+encodeURIComponent(detailId.value)+'/sync',{timeframe:timeframe.value==='1w'?'1d':timeframe.value});notify(result.status==='success'?'同步成功':result.message);await changeTimeframe(timeframe.value);await refreshBase()}) }
async function syncAnnouncements() { await action(async()=>{await post('/instruments/'+encodeURIComponent(detailId.value)+'/announcements');await changeTimeframe(timeframe.value);notify('公告已同步，仍需核对停复牌与产品规则')}) }
watch(() => [detailId.value,detail.value?.instrument.name], () => emit('security-context',detailId.value?{id:detailId.value,name:detail.value?.instrument.name || detailId.value}:null))
watch(page, value => {if(value!=='watchlist'&&detailId.value)closeDetail()})
// 复制初始表单数据，避免编辑弹窗时直接修改列表对象。
function openModal(name: string, initial: any = {}) { modal.value = name; form.value = structuredClone(initial); modalError.value = '' }
// 按弹窗类型提交对应操作，成功后刷新总览、页面与证券详情。
async function submitModal() {
  busy.value = true; modalError.value = ''
  try {
    const f = form.value
    if (modal.value === 'add') await post('/watchlist', f)
    if (modal.value === 'account') await post('/accounts', f)
    if (modal.value === 'order') await post('/orders', { ...f, stop_loss: f.stop_loss || null })
    if (modal.value === 'rule') await post('/simulation/rules', { ...f, expires_on: f.expires_on || null, max_orders: f.max_orders || null, source_id: f.source_id || null, stop_loss: f.stop_loss || null, trigger_price: f.trigger_price || null, limit_price: f.limit_price || null, stop_price: f.stop_price || null, sell_all: !!f.sell_all })
    if (modal.value === 'password') await post('/security/password', f)
    if (modal.value === 'verify') await post('/instruments/' + encodeURIComponent(detailId.value) + '/verify', f)
    if (modal.value === 'restore') await post('/backups/' + f.id + '/restore', { confirmation: f.confirmation })
    modal.value = ''; notify('已保存'); await refreshBase(); await loadPage(); if (detailId.value) await openDetail(detailId.value, true)
  } catch (e: any) { modalError.value = e.message } finally { busy.value = false }
}
// 生成新的委托幂等键，并以当前账户、证券和参考报价填充表单。
function newOrder(id = '') { openModal('order', { client_key: crypto.randomUUID(), account_id: portfolio.value.accounts[0]?.id || '', instrument_id: id || watchlist.value.find(r => r.asset_class === 'stock')?.id || '', side: 'buy', quantity: 100, limit_price: watchlist.value.find(r => r.id === id)?.quote?.close || 0, stop_loss: null }) }
// 先加载本人模拟账户，尚无账户时引导输入虚拟本金。
async function prepareOrder(id = '') { portfolio.value = await api('/simulation'); if (!portfolio.value.accounts.length) { openModal('account', { name: '我的模拟账户', initial_cash: null }); return } newOrder(id) }
const ruleKinds: Record<string, string> = { close_at_or_below: '收盘落到', close_at_or_above: '收盘升到', close_stop: '收盘止损', ma_forward: '均线跟随' }
const ruleStatuses: Record<string, string> = { active: '启用', paused: '暂停', stopped: '停止' }
// 只打开预填表单。价格由用户确认后才提交，不从想法原文提取。
async function openRule(partial: any = {}) { portfolio.value = await api('/simulation'); if (!portfolio.value.accounts.length) { openModal('account', { name: '我的模拟账户', initial_cash: null }); return } openModal('rule', { account_id: portfolio.value.accounts[0].id, instrument_id: partial.instrument_id || watchlist.value.find((r: any) => ['stock', 'etf'].includes(r.asset_class) && ['SH', 'SZ'].includes(r.market))?.id || '', kind: partial.kind || 'close_at_or_below', trigger_price: partial.trigger_price ?? null, limit_price: partial.limit_price ?? null, quantity: partial.quantity ?? 100, stop_loss: partial.stop_loss ?? null, stop_price: partial.stop_price ?? null, sell_all: !!partial.sell_all, fast: partial.fast ?? 5, slow: partial.slow ?? 20, stop_loss_pct: partial.stop_loss_pct ?? 0.05, expires_on: partial.expires_on || '', max_orders: partial.max_orders ?? null, source_kind: partial.source_kind || 'manual', source_id: partial.source_id || '', source_note: partial.source_note || '' }) }
async function ruleAction(id: string, verb: string, cancelPending = false) { await action(async () => { await post('/simulation/rules/' + id + '/' + verb, { cancel_pending: cancelPending }); await loadPage() }) }
// 根据现有证券规则和证据创建核验表单草稿。
function verifyForm() { const i = detail.value.instrument; openModal('verify', { name: i.name, status: 'active', risk_flag: 'clear', rules: { ...i.rules, verified: true }, evidence: { listing_url: i.source_url || '', status_url: '', announcement_url: '', review_note: '', ...i.evidence } }) }
// 提交报价与数量，展示后端按真实产品规则计算的含费预算。
async function estimate() {
  const id = detailId.value, read = detailRead
  await action(async () => {
    const result = await post('/budget', { instrument_id: id, ...budgetForm.value })
    if (read === detailRead && detailId.value === id) budget.value = result
  })
}
// 读取自选、行情或配置文件，经后端校验和必要确认后导入。
async function readFile(event: Event, kind: string) {
  const target = event.target as HTMLInputElement, file = target.files?.[0]
  if (!file) return
  const id = detailId.value, tf = timeframe.value, read = detailRead
  await action(async () => {
    const content = await file.text()
    if (kind === 'watch') {
      const r = await post('/watchlist/import', { content, format: file.name.endsWith('.csv') ? 'csv' : 'json' })
      notify(`已导入 ${r.imported} 项，按市场与代码去重`); await refreshBase()
    } else if (kind === 'bars') {
      if (read !== detailRead || detailId.value !== id) return
      await post('/instruments/' + encodeURIComponent(id) + '/bars/import', { bars: JSON.parse(content), timeframe: tf === '1w' ? '1d' : tf })
      if (read === detailRead && detailId.value === id) {
        await changeTimeframe(tf); notify('历史数据已导入并校验')
      }
    } else {
      const settings = JSON.parse(content)
      if (await confirmForUser('确认导入配置？费用、预算和安全设置将按文件更新。')) {
        await api('/settings', { method: 'PUT', body: JSON.stringify({ settings, confirm: true }) }); await loadPage(); notify('配置已导入')
      }
    }
  })
  target.value = ''
}
// 取得明确确认后保存配置，并提示监听配置是否需要重启。
async function saveSettings() { if (!await confirmForUser('确认保存设置？费用与预算会影响后续模拟和研究；局域网开关变更需重启服务。')) return; await action(async () => { const r = await api('/settings', { method: 'PUT', body: JSON.stringify({ settings: config.value, confirm: true }) }); notify(r.restart_required ? '设置已保存，重启服务后网络监听变更生效' : '设置已保存'); await refreshBase() }) }
// 清理空日期，确认未验证模式后运行实验并刷新归档。
async function runBacktest() { await action(async () => { const p = { ...testParams.value }; if (!p.start_date) delete p.start_date; if (!p.end_date) delete p.end_date; if (!p.strict && !await confirmForUser('将运行未验证实验：缺失的历史状态或公司行动可能使结果失真。继续？')) return; selectedBacktest.value = await post('/backtests', p); experiments.value = await api('/backtests'); notify('历史撮合完成，结果与数据快照已归档') }) }
// 读取指定报告的完整事实和解释，交给报告详情展示。
async function showReport(id: string) {
  const read = ++reportRead, originPage = page.value
  // 连点另一份报告或离开页面后，旧响应不再写回。
  const current = () => read === reportRead && page.value === originPage
  selectedReport.value = null; reportLoading.value = true; error.value = ''
  try {
    const next = await api('/reports/' + id)
    if (current()) selectedReport.value = next
  } catch (e: any) {
    if (current()) error.value = e.message
  } finally {
    // 只有仍是当前请求时才关掉加载态，避免取消后的迟到响应改掉界面。
    if (current()) reportLoading.value = false
  }
}
// 返回列表或离开页面时，使尚未完成的报告请求失效。
function closeReport() {
  ++reportRead // 递增序号，迟到的报告响应不再写回。
  selectedReport.value = null
  reportLoading.value = false
}
// 确认模型调用后对原事实提交补充解释，生成新的报告版本。
async function supplementReport() { if (!selectedReport.value || !await confirmForUser('使用这份报告的原始事实补充 AI？这会使用当前默认配置并可能消耗本人模型额度，原归档保持不变。')) return; await action(async () => { await post('/reports/' + selectedReport.value.id + '/supplement'); notify('已提交补充 AI，完成后将保存为新的报告版本') }) }
// 切到报告页面并打开指定归档。
function reportRoute(id: string) { go('reports'); showReport(id) }
// 切换自选置顶状态：先更新当前列表，保存成功后再后台对齐总览。
async function pin(row: any) {
  const next = !row.pinned
  applyPinned(row.id, next)
  await action(async () => { await api('/instruments/' + encodeURIComponent(row.id), { method: 'PATCH', body: JSON.stringify({ pinned: next }) }) })
  if (error.value) applyPinned(row.id, !next)
  else void refreshBase()
}
// 确认后移除自选关联，刷新列表中的个人状态。
async function remove(row: any) { if (!await confirmForUser('从自选中移除 ' + row.name + '？关联条件提醒将默认停用，触发历史和研究记录会保留。')) return; await action(async () => { await api('/watchlist/' + encodeURIComponent(row.id), { method: 'DELETE' }); await refreshBase() }) }
// 收集非空分组名称并仅更新该证券的个人分组。
async function editGroup(row: any) { const v = prompt('分组名称', row.group_name); if (v?.trim()) await action(async () => { await api('/instruments/' + encodeURIComponent(row.id), { method: 'PATCH', body: JSON.stringify({ group_name: v.trim() }) }); await refreshBase() }) }
// 通过统一操作状态管理下载错误与忙碌状态。
async function exportData(path: string, name: string) { await action(() => download(path, name)) }
// 页面与指数选择变化时仅刷新对应数据。
watch(page, loadPage)
watch(activeIndex, loadIndex)
// 挂载后引导会话并定期刷新；隐藏页面或编辑弹窗打开时跳过轮询。
onMounted(async () => { if (!nav.some(n => n.id === page.value)) page.value = 'dashboard'; await initialize(); timer = setInterval(async () => { if (!ready.value || requiresLogin.value || modal.value || document.hidden) return; try { await refreshBase(); if (page.value === 'jobs') jobData.value = await api('/jobs'); if (page.value === 'simulation') portfolio.value = await api('/simulation'); if (['reports', 'review'].includes(page.value)) reports.value = await api('/reports' + (page.value === 'review' ? '?mode=full' : '')) } catch { /* 自动刷新失败时保留最近成功的视图，手动刷新时再展示错误。 */ } }, 15000) })
// 监听浏览器历史导航，使页面选择与地址哈希保持一致。
onMounted(() => window.addEventListener('hashchange', syncHash))
// 卸载时清理计时器和事件监听，并完成未结束的确认流程。
onBeforeUnmount(() => { clearInterval(timer); clearTimeout(toastTimer); window.removeEventListener('hashchange', syncHash); settleConfirm(false); ++detailRead; ++indexRead; closeReport(); emit('security-context',null) })
</script>

<template>
<!-- 依次显示加载、局域网登录或已就绪的研究工作台。 -->
  <div v-if="loading" class="initial-loading"><div class="brand-mark"><ChartNoAxesCombined :size="24" /></div><LoaderCircle class="spin" :size="22" /><span>{{ props.publicMode ? '正在加载个人研究空间' : '正在连接本地研究服务' }}</span></div>
  <main v-else-if="requiresLogin" class="login-page"><form class="login-card" @submit.prevent="login"><div class="brand"><span class="brand-mark"><ChartNoAxesCombined /></span><b>Stock<span>AI</span></b></div><h1>连接你的研究终端</h1><p>局域网访问需要本机管理员配置的访问密码。</p><label>访问角色<select v-model="form.role" required><option value="admin">管理员</option><option value="reader">只读访问者</option></select></label><label>访问密码<input type="password" v-model="form.password" required autocomplete="current-password" /></label><p v-if="error" class="inline-error">{{ error }}</p><button class="primary" :disabled="busy">登录</button></form></main>
  <div v-else class="app-shell" :class="{'app-security-focused':!!detailId, 'is-watchlist': page==='watchlist' && !detailId}">
    <div v-if="sidebarOpen && !props.publicMode" class="sidebar-scrim" @click="sidebarOpen = false"></div>
    <aside v-if="!props.publicMode" id="workspace-navigation" class="sidebar" :class="{ open: sidebarOpen }" @keydown.esc="sidebarOpen = false">
      <a href="#dashboard" class="brand" @click.prevent="go('dashboard')"><span class="brand-mark"><ChartNoAxesCombined :size="23" /></span><b>Stock<span>AI</span></b><span class="version">{{ props.publicMode ? (dashboard?.version || "3.1") : "2.0" }}</span></a>
      <div class="workspace"><span class="workspace-avatar">研</span><div><strong>个人研究工作台</strong><small>{{ props.publicMode ? 'PRIVATE RESEARCH SPACE' : 'LOCAL WORKSPACE' }}</small></div><button class="icon-button sidebar-close" aria-label="关闭导航" @click="sidebarOpen = false"><X :size="17" /></button></div>
      <div class="sidebar-navigation">
      <div class="nav-caption">行情与研究</div>
      <nav aria-label="主导航"><template v-for="(item, idx) in nav" :key="item.id"><div v-if="idx === 7" class="nav-caption separated">策略与系统</div><button :class="['nav-item', { active: page === item.id }]" :aria-current="page === item.id ? 'page' : undefined" @click="go(item.id)"><component :is="item.icon" :size="18" /><span>{{ item.name }}</span><span v-if="item.id === 'candidates'" class="nav-count">{{ dashboard?.stats.candidate_count || 0 }}</span><span v-if="item.id === 'jobs' && dashboard?.jobs.some((j: any) => j.status === 'running')" class="status-dot"></span></button></template></nav>
      <template v-if="props.publicMode"><div class="nav-caption separated">研究增强</div><nav class="research-extra-nav" aria-label="研究增强入口"><button @click="emit('open-workbench','readiness')"><ShieldCheck :size="16"/>数据就绪度</button><button @click="emit('open-workbench','alerts')"><Bell :size="16"/>条件提醒</button><button @click="emit('open-workbench','journals')"><BookOpen :size="16"/>研究日志</button><button @click="emit('open-workbench','portfolio')"><Wallet :size="16"/>组合风险</button><button @click="emit('open-workbench','usage')"><Activity :size="16"/>模型用量</button><button @click="emit('open-workbench','assistant')"><CircleHelp :size="16"/>AI 助手</button></nav></template>
      </div>
      <div class="sidebar-bottom"><div class="local-model"><div class="model-icon"><Activity :size="18" /></div><div><strong>{{ props.publicMode ? '个人模型' : '本地模型' }}</strong><small :class="{ warning: !model.available }">{{ props.publicMode ? (model.configured ? '已配置 · 按需调用' : '未选择默认配置') : model.available ? 'Ollama 已连接' : 'Ollama 未连接' }}</small></div><span :class="['status-dot', { off: !model.available }]"></span></div><div class="sidebar-foot"><LockKeyhole :size="12" /> {{ props.publicMode ? '独立的个人研究空间' : '数据保存在本机' }} <span>{{ props.publicMode ? 'v'+(dashboard?.version||'3.1') : 'v2.0.0' }}</span></div></div>
    </aside>
    <div class="main-shell">
      <header v-if="!props.publicMode" class="topbar"><div class="breadcrumbs"><button class="icon-button mobile-menu" @click="sidebarOpen = true" aria-label="打开导航" :aria-expanded="sidebarOpen" aria-controls="workspace-navigation"><Menu :size="20" /></button><span>研究空间</span><ChevronRight :size="13" /><strong>{{ title }}</strong></div><div class="topbar-right"><span class="local-pill"><span class="status-dot"></span> {{ props.publicMode ? '个人工作台' : '本地运行' }}</span><span class="top-divider"></span><button class="icon-button notification-button" aria-label="通知" @click="showNotifications = !showNotifications"><Bell :size="18" /><i v-if="dashboard?.notifications.some((n: any) => !n.read)"></i></button><span class="user-avatar">{{ role === 'admin' ? '研' : '阅' }}</span></div></header>
      <div v-if="showNotifications" class="notification-panel panel"><div class="panel-heading"><h3>通知</h3><button class="text-button" @click="action(async () => { await post('/notifications/read'); await refreshBase() })">全部已读</button></div><p v-if="!dashboard?.notifications.length" class="muted">暂无通知</p><div v-for="n in dashboard?.notifications" :key="n.id" class="notification-item"><strong>{{ n.message }}</strong><small>{{ dt(n.ts) }}</small></div></div>
      <main class="content">
        <div v-if="error" class="error-banner" role="alert"><CircleHelp :size="17" /><span>{{ error }}</span><button class="icon-button" @click="error = ''" aria-label="关闭错误"><X :size="15" /></button></div>
        <div v-if="!detailId" class="page-heading"><div><div :class="['eyebrow', { 'page-kicker': page === 'dashboard' }]">{{ page === 'dashboard' ? '研究简报' : '研究空间' }}</div><h1>{{ page === 'dashboard' ? dashboardBrief.title : title }}</h1><p>{{ page === 'dashboard' ? dateText + ' · ' + dashboardBrief.lead : page === 'watchlist' ? '持仓优先。核验未过的证券不进入候选。' : page === 'metals' ? '现货报价、金银比与关联资产。' : page === 'simulation' ? '虚拟资金、持仓与委托。不连接真实证券账户。' : page === 'backtest' ? '历史数据回测与策略表现。' : page === 'reports' ? '已保存的研究报告与原始资料。' : page === 'jobs' ? '任务进度、执行日志与计划。' : page === 'settings' ? '配置本地模型、数据规则与研究环境。' : page === 'review' ? '回看已保存的研究、交易与策略实验。' : page === 'ideas' ? '一行一条想法。池子是空的时候不开始评审。' : '价格、证据与风险，一起看。' }}</p></div><div class="heading-actions"><button v-if="props.publicMode" class="icon-button notification-button" aria-label="通知" :aria-expanded="showNotifications" @click="showNotifications=!showNotifications"><Bell :size="18"/><i v-if="dashboard?.notifications.some((n: any)=>!n.read)"></i></button><template v-if="['dashboard', 'candidates', 'reports', 'review'].includes(page)"><button v-if="canMaintain" class="secondary" :disabled="busy" @click="runJob('sync')"><RefreshCw :size="15" />同步数据</button><button class="primary" :disabled="busy || !canEdit" @click="runJob(page === 'review' || reportMode === 'full' ? 'full' : 'standard')"><Sparkles :size="15" />{{ page === 'review' || reportMode === 'full' ? '生成深度复盘' : '生成研究报告' }}</button></template><button v-else-if="page === 'watchlist'" class="primary" :disabled="!canEdit" @click="openModal('add', { symbol: '', market: 'SZ', name: '待核验', asset_class: 'stock', group_name: '我的自选' })"><Plus :size="16" />添加证券</button><button v-else-if="(page === 'metals' || page === 'market') && canMaintain" class="secondary" :disabled="busy" @click="runJob(page === 'metals' ? 'metals' : 'sync')"><RefreshCw :size="15" />同步行情</button><button v-else-if="page === 'simulation'" class="primary" :disabled="!canEdit" @click="prepareOrder()"><Plus :size="16" />模拟委托</button><button v-else-if="page === 'settings'" class="primary" :disabled="busy || !canEdit" @click="saveSettings"><Check :size="16" />保存设置</button></div></div>

        <SecurityWorkbench v-if="detailId" :id="detailId" :detail="detail" :loading="detailLoading" :busy="busy" :watchlist="watchlist" :timeframe="timeframe" :budget="budget" :can-edit="canEdit" :can-maintain="canMaintain" :public-mode="!!props.publicMode" @close="closeDetail" @select="openDetail" @period="changeTimeframe" @sync="syncDetail" @verify="verifyForm" @announcements="syncAnnouncements" @import-bars="readFile($event,'bars')" @estimate="budgetForm=$event;estimate()" @research="(view,context)=>emit('open-workbench',view,context)" />
        <ResearchOverview v-else-if="page === 'dashboard'" :dashboard="dashboard" :index-data="indexData" :indices="indices" :active-index="activeIndex" :can-edit="canEdit" :can-maintain="canMaintain" :busy="busy" :public-mode="!!props.publicMode" @navigate="props.publicMode && ['readiness','jobs'].includes($event) ? emit('open-workbench',$event) : go($event)" @instrument="openDetail" @report="reportRoute" @job="runJob" @add="openModal('add',{symbol:'',market:'SZ',name:'待核验',asset_class:'stock',group_name:'我的自选'})" @index="activeIndex=$event" />

        <template v-else-if="page === 'watchlist' || page === 'market'">
          <section v-if="page === 'market'" class="panel"><div class="panel-heading"><h2>指数研究</h2><select v-model="activeIndex"><option v-for="i in indices" :key="i.id" :value="i.id">{{ i.name }}</option></select></div><ResearchChart :bars="indexData?.bars || []" :indicators="indexData?.indicators.series || []" /></section>
          <section :class="['panel','watch-full',{'watch-page':page==='watchlist'}]"><div class="filter-toolbar"><div class="search-box"><Search :size="16" /><input v-model="search" aria-label="搜索证券" placeholder="搜索名称、代码或分组" /></div><select v-model="group" aria-label="分组"><option v-for="g in groups" :key="g" :value="g">{{ g === '全部' ? '全部分组' : g }}</option></select><div class="segmented" role="group" aria-label="状态"><button v-for="item in ['全部','已核验','待核验','仅观察']" :key="item" type="button" :class="{selected: statusFilter===item}" :aria-pressed="statusFilter===item" @click="statusFilter=item">{{ item }}</button></div><div class="toolbar-spacer"></div><button class="secondary small" :disabled="!canEdit" @click="importFile?.click()"><Plus :size="14" />导入</button><button class="secondary small" @click="exportData('/watchlist/export?format=csv', 'StockAI自选.csv')"><ArrowDownToLine :size="14" />CSV</button><button class="secondary small" @click="exportData('/watchlist/export', 'StockAI自选.json')">JSON</button><input hidden ref="importFile" type="file" accept=".json,.csv" @change="readFile($event, 'watch')" /></div><div class="table-scroll"><table class="watch-table full-table"><thead><tr><th></th><th>证券 / 代码</th><th>分组</th><th class="number">最近价格</th><th class="number">涨跌幅</th><th class="number">持仓价</th><th class="number">持仓数量</th><th class="number">持仓金额</th><th class="number">当天收益</th><th class="number">当天收益率</th><th class="number">总收益率</th><th>风险状态</th><th class="number">¥1000 预算估算</th><th>来源 / 报价时间</th><th></th></tr></thead><tbody><tr v-for="r in filteredWatch" :key="r.id"><td><button class="icon-button" :class="{ 'pinned-star': r.pinned }" :disabled="!canEdit" @click="pin(r)" :aria-label="r.pinned ? '取消置顶' : '置顶'"><Star :size="15" :fill="r.pinned ? 'currentColor' : 'none'" /></button></td><td><button class="security-name" @click="openDetail(r.id)">{{ r.name }}</button><small>{{ r.symbol }} · {{ r.market }}</small></td><td><button class="group-button" :disabled="!canEdit" @click="editGroup(r)">{{ r.group_name }}</button></td><td class="number mono">{{ fmt(r.quote?.close ?? r.quote?.price, r.asset_class === 'etf' ? 3 : 2) }}</td><td :class="['number mono', direction(r.change_pct)]">{{ pct(r.change_pct) }}</td><td class="number mono">{{ fmt(r.position?.cost_price) }}</td><td class="number mono">{{ r.position?.quantity ?? '—' }}</td><td class="number mono">{{ fmt(r.position?.market_value) }}</td><td :class="['number mono', direction(r.position?.daily_pnl)]">{{ fmt(r.position?.daily_pnl) }}</td><td :class="['number mono', direction(r.position?.daily_return_pct)]">{{ pct(r.position?.daily_return_pct) }}</td><td :class="['number mono', direction(r.position?.total_return_pct)]">{{ pct(r.position?.total_return_pct) }}</td><td><span :class="['tag', !r.exclusion_reasons.length ? 'green' : r.risk_flag === 'st' || r.status === 'delisted' ? 'red' : 'amber']" :title="r.exclusion_reasons.join('；')">{{ r.observation_only ? '仅观察' : r.risk_flag === 'st' ? 'ST风险' : r.status === 'delisted' ? '已退市' : r.status === 'suspended' ? '停牌' : r.exclusion_reasons.length ? '待核验' : '已核验' }}</span></td><td class="number">{{ r.budget ? r.budget.max_quantity : '—' }}<small>{{ r.budget ? (r.exclusion_reasons.length ? '仅数学试算' : '含费预算估算') : '缺少行情' }}</small></td><td class="source-cell"><span>{{ r.quote?.source || '未获取' }}</span><small>{{ r.quote ? dt(r.quote.ts) : '—' }}</small></td><td><button class="icon-button" :disabled="!canEdit" @click="remove(r)" aria-label="移除自选"><Trash2 :size="15" /></button></td></tr></tbody></table></div><div v-if="!filteredWatch.length" class="empty-inline">没有符合条件的证券</div><div class="table-footer">共 {{ filteredWatch.length }} 项 · 初始名称来自需求清单，最新名称和交易资格须分别核验。</div></section>
        </template>

        <template v-else-if="page === 'metals'">
          <div class="metal-card-grid"><section v-for="(m, idx) in dashboard?.metals" :key="m.id" class="panel metal-card"><div class="metal-card-top"><span :class="['metal-symbol', { silver: Number(idx) % 2 === 1 }]">{{ Number(idx) % 2 ? 'Ag' : 'Au' }}</span><span class="tag">{{ Number(idx) < 2 ? '国际现货' : '期货连续合约' }}</span></div><h2>{{ metalNames[m.id] }}</h2><p>{{ m.id }}</p><div class="metal-card-price">{{ fmt(m.price) }}<span>{{ m.currency || (Number(idx) < 2 ? 'USD' : 'CNY') }}</span></div><div class="metal-meta"><span>计价单位</span><strong>{{ m.unit || (Number(idx) < 2 ? '美元/金衡盎司' : Number(idx) === 2 ? '元/克' : '元/千克') }}</strong><span>报价时间</span><strong>{{ dt(m.quote_time) }}</strong><span>采集时间</span><strong>{{ dt(m.fetched_at) }}</strong><span>延迟说明</span><strong>{{ m.delay_type || '数据不可用' }}</strong><span>来源</span><a v-if="m.source_url" :href="m.source_url" target="_blank" rel="noopener">{{ m.source }}</a><strong v-else>未获取</strong></div><div :class="['data-status', { warning: m.stale }]">{{ m.price ? m.stale ? '报价已超过24小时，请重新同步' : '已取得来源报价' : m.message }}</div></section></div><section class="panel prose-panel"><h2>同是金银，报价口径不同</h2><p>伦敦现货以美元/金衡盎司计价；沪金以元/克、沪银以元/千克计价。期货连续合约包含换月与期限结构影响，不能当作现货价格直接比较。金银比仅在同币种、同单位、同一报价时点下计算。</p><h3>贵金属关联 A 股</h3><div class="related-chips"><button v-for="r in watchlist.filter(r => r.group_name === '贵金属关联')" :key="r.id" @click="openDetail(r.id)">{{ r.name }}<small>{{ r.symbol }}</small><ArrowUpRight :size="14" /></button></div></section>
        </template>

        <IdeaPool v-else-if="page === 'ideas'" :can-edit="canEdit" @arm="openRule({ instrument_id: $event.instrument_id, source_kind: 'idea', source_id: $event.id, source_note: '进场 ' + ($event.entry || '') + '\n离场 ' + ($event.exit || '') + '\n错在哪 ' + ($event.wrong || '') })" />

        <template v-else-if="page === 'candidates'">
          <div class="info-strip"><ShieldCheck :size="19" /><span>交易状态、官方公告、行情新鲜度、流动性与单只含费预算全部通过，才进入候选。评分为规则匹配程度，不是上涨概率。</span></div><div v-if="!dashboard?.candidates.length" class="panel large-empty"><ShieldCheck :size="37" /><h2>今日无合格模拟候选</h2><p>宁可保留观察，也不把未知写成安全。先同步行情，并在证券详情核验官方证据。</p><button class="secondary" @click="go('watchlist')">查看观察池 <ArrowRight :size="15" /></button></div><div v-else class="candidate-grid"><section v-for="c in dashboard.candidates" :key="c.instrument_id" class="panel candidate-card"><div class="panel-heading"><h2>{{ c.name }} <small>{{ c.symbol }}</small></h2><b class="score">{{ c.score }}</b></div><div class="candidate-cost"><span>模拟 {{ c.quantity }} 股/份</span><strong>¥{{ fmt(c.budget.total) }}<small>含买入费用 ¥{{ fmt(c.budget.fees.total) }}</small></strong></div><h3>研究理由</h3><p>{{ c.rationale }}</p><h3>反方观点</h3><p>{{ c.counterargument }}</p><h3>失效条件</h3><p>{{ c.invalidation }}</p><small>{{ dt(c.quote_time) }} · <a :href="c.source_url" target="_blank" rel="noopener">行情来源</a></small><button class="secondary" :disabled="!canEdit" @click="prepareOrder(c.instrument_id)">创建模拟计划 <ArrowRight :size="15" /></button><button class="text-button" :disabled="!canEdit" @click="openRule({ instrument_id: c.instrument_id, quantity: c.quantity, source_kind: 'candidate', source_id: c.instrument_id, kind: 'close_at_or_below' })">预填价格规则</button></section></div><section v-if="reports.length" class="panel prose-panel"><h2>查看完整排除依据</h2><p>报告逐只保留未入选原因与当时的事实快照。</p><button class="text-button" @click="reportRoute(reports[0].id)">打开最近报告 <ArrowRight :size="14" /></button></section>
        </template>

        <template v-else-if="page === 'reports' || page === 'review'">
          <p v-if="reportLoading" role="status" class="data-status">正在加载研究报告…<button class="text-button" @click="closeReport">取消</button></p>
          <template v-if="selectedReport"><button class="text-button back-button" @click="closeReport"><ChevronLeft :size="16" />返回报告列表</button><section class="panel report-document"><div class="report-document-top"><div><span class="eyebrow">RESEARCH ARCHIVE</span><h2>{{ modeNames[selectedReport.mode] }}</h2><p>{{ selectedReport.report_date }} · {{ dt(selectedReport.generated_at) }} · Asia/Shanghai</p></div><div class="button-row"><button v-for="f in ['md', 'html', 'json']" :key="f" class="secondary small" @click="exportData('/reports/' + selectedReport.id + '/export?format=' + f, 'StockAI-' + selectedReport.report_date + '.' + f)"><ArrowDownToLine :size="13" />{{ f.toUpperCase() }}</button></div></div><div class="report-evidence-strip"><span>模型 <b>{{ selectedReport.model }}</b></span><span>策略 <b>{{ selectedReport.strategy_version }}</b></span><span class="tag" :class="selectedReport.ai_status === 'fallback' ? 'amber' : 'green'">{{ selectedReport.ai_status === 'fallback' ? '规则型报告' : 'AI解释已校验' }}</span></div><p v-if="selectedReport.ai_error" class="data-status warning">{{ selectedReport.ai_error }}</p><div v-if="props.publicMode" class="report-version-controls"><span class="tag">第 {{ selectedReport.version }} 版</span><button class="secondary small" @click="emit('open-workbench','assistant',{kind:'report',id:selectedReport.id})">就此报告提问</button><button class="secondary small" @click="emit('open-workbench','journals',{kind:'report',id:selectedReport.id})">记录研究判断</button><button v-for="v in selectedReport.versions" :key="v.id" class="secondary small" :disabled="v.id === selectedReport.id" @click="showReport(v.id)">版本 {{ v.version }}</button><button class="primary small" :disabled="busy" @click="supplementReport">补充 AI / 生成新版本</button><small>原始事实快照保持不变 · 使用本人当前默认配置</small></div><h2>{{ selectedReport.summary }}</h2><p class="report-lead">{{ selectedReport.market_context }}</p><IdeaReview v-if="selectedReport.idea_review" :review="selectedReport.idea_review" /><h3>研究候选 · {{ selectedReport.candidates.length }} 只</h3><p v-if="!selectedReport.candidates.length">今日无合格模拟候选。</p><div v-for="c in selectedReport.candidates" :key="c.instrument_id" class="report-candidate"><h3>{{ c.name }} {{ c.symbol }}</h3><div v-if="props.publicMode" class="button-row"><button class="text-button small" @click="emit('open-workbench','journals',{kind:'instrument',id:c.instrument_id,report_id:selectedReport.id})">记录判断</button><button class="text-button small" @click="emit('open-workbench','assistant',{kind:'instrument',id:c.instrument_id})">就此证券提问</button></div><p>报价 ¥{{ fmt(c.price) }} · {{ dt(c.quote_time) }} · {{ c.quantity }} 股/份 · 含费 ¥{{ fmt(c.budget.total) }}</p><p>{{ c.rationale }}</p><p><strong>反方观点：</strong>{{ c.counterargument }}</p><p><strong>失效条件：</strong>{{ c.invalidation }}</p></div><h3>风险与缺失数据</h3><ul><li v-for="(r, idx) in [...selectedReport.risk_notes, ...selectedReport.missing_data]" :key="idx">{{ r }}</li></ul><h3>未入选记录</h3><div class="table-scroll"><table class="watch-table exclusion-table"><thead><tr><th>证券</th><th>排除原因</th></tr></thead><tbody><tr v-for="r in selectedReport.excluded" :key="r.instrument_id"><td><button class="security-name" @click="openDetail(r.instrument_id)">{{ r.name }}</button><small>{{ r.instrument_id }}</small></td><td>{{ r.reasons.join('；') }}</td></tr></tbody></table></div><h3>原始证据 · {{ selectedReport.sources.length }} 项</h3><div v-for="s in selectedReport.sources" :key="s.id" class="source-row"><a :href="s.url" target="_blank" rel="noopener">[{{ s.id }}] {{ s.instrument_id }} · {{ s.source }}</a><small>报价 {{ dt(s.quote_time) }} · 采集 {{ dt(s.collected_at) }}</small></div><div class="snapshot-id">事实快照 SHA256 · {{ selectedReport.snapshot_id }}</div></section></template>
          <template v-else><div v-if="page === 'review'" class="stats-grid"><article class="metric-card"><div class="metric-label">已存复盘</div><div class="metric-value">{{ reports.length }}</div></article><article class="metric-card"><div class="metric-label">虚拟账户</div><div class="metric-value">{{ portfolio.accounts.length }}</div></article><article class="metric-card"><div class="metric-label">模拟成交</div><div class="metric-value">{{ portfolio.orders.filter((x: any) => x.status === 'filled').length }}</div></article><article class="metric-card"><div class="metric-label">已存回测</div><div class="metric-value">{{ experiments.length }}</div></article></div><section class="panel"><div class="filter-toolbar"><div class="segmented"><button v-for="m in ['', 'standard', 'full', 'closing']" :key="m" :class="{ selected: reportMode === m }" @click="reportMode = m">{{ m ? modeNames[m] : '全部报告' }}</button></div><div class="toolbar-spacer"></div><div class="search-box"><Search :size="15" /><input v-model="reportSearch" placeholder="搜索日期或研究结论" aria-label="搜索报告" /></div></div><div v-if="!visibleReports.length" class="large-empty"><FileText :size="34" /><h2>等待第一份研究档案</h2><p>标准版覆盖日常研究，完整版记录多周期与历史验证。证据不足会如实写入报告。</p></div><div v-for="r in visibleReports" :key="r.id" class="report-list-row" @click="showReport(r.id)"><span class="report-icon"><FileText :size="22" /></span><div><span class="tag">{{ modeNames[r.mode] }}</span><h3>{{ r.summary }}</h3><small>{{ dt(r.generated_at) }} · {{ r.ai_status === 'fallback' ? '规则型报告' : 'AI解释已校验' }}</small></div><span class="tag">{{ r.candidate_count }} 只候选</span><ChevronRight :size="17" /></div></section></template>
        </template>

        <template v-else-if="page === 'simulation'">
          <!-- 模拟交易：展示虚拟账户、缓存估值、可卖持仓和资金流水。 -->
          <div class="info-strip"><Wallet :size="19" /><span>独立虚拟账户，初始资金由你设置。委托仅使用创建后形成的行情撮合；收盘触发止损在后续K线执行。</span><button class="text-button" :disabled="!canEdit" @click="openModal('account', { name: '', initial_cash: null })">新增账户 <Plus :size="14" /></button></div><div v-if="!portfolio.accounts.length" class="panel large-empty"><Wallet :size="36" /><h2>建立你的第一个模拟账户</h2><p>填写虚拟初始资金，不代表你的真实总资产。</p><button class="primary" :disabled="!canEdit" @click="openModal('account', { name: '我的模拟账户', initial_cash: null })"><Plus :size="16" />创建模拟账户</button></div><section v-for="a in portfolio.accounts" :key="a.id" class="panel account-panel"><div class="panel-heading"><h2>{{ a.name }} <span class="small-badge">虚拟账户</span></h2><div class="button-row"><button v-if="props.publicMode" class="text-button" @click="emit('open-workbench','portfolio',{kind:'account',id:a.id})">组合风险</button><button v-if="props.publicMode" class="text-button" @click="emit('open-workbench','assistant',{kind:'account',id:a.id})">就此账户提问</button><small>创建于 {{ dt(a.created_at) }}</small></div></div><div class="account-metrics"><div><span>总权益（缓存估值）</span><b>¥{{ fmt(a.equity) }}</b></div><div><span>账户现金</span><b>¥{{ fmt(a.cash) }}</b></div><div><span>待买已预留</span><b>¥{{ fmt(a.reserved_buy) }}</b></div><div><span>还可再买</span><b>¥{{ fmt(a.available_cash) }}</b></div><div><span>累计损益</span><b :class="direction(a.pnl)">¥{{ fmt(a.pnl) }}</b></div><div><span>初始虚拟资金</span><b>¥{{ fmt(a.initial_cash) }}</b></div></div><ResearchChart v-if="a.curve.length" kind="equity" :curve="a.curve" /><div class="table-scroll"><table class="watch-table"><thead><tr><th>持仓证券</th><th>持有 / 可卖数量</th><th class="number">含费成本</th><th class="number">缓存市值</th><th class="number">浮动损益</th><th>行情时间</th></tr></thead><tbody><tr v-for="p in a.positions" :key="p.instrument_id"><td><button class="security-name" @click="openDetail(p.instrument_id)">{{ p.name }}</button><small>{{ p.instrument_id }}</small></td><td>{{ p.quantity }} / {{ p.available_quantity }}</td><td class="number">{{ fmt(p.cost) }}</td><td class="number">{{ fmt(p.market_value) }}</td><td :class="['number', direction(p.unrealized_pnl)]">{{ fmt(p.unrealized_pnl) }}</td><td>{{ dt(p.quote_time) }} <span v-if="p.stale" class="tag amber">过期</span></td></tr></tbody></table></div><p v-if="!a.positions.length" class="empty-table">暂无持仓</p></section><section class="panel"><div class="panel-heading"><div><h2>价格规则</h2><p>收盘后判断。15:00 之前启用可以用当天收盘，成交仍等到下一根开盘。</p></div><button class="secondary small" :disabled="!canEdit" @click="openRule()"><Plus :size="14" />启用规则</button></div><div v-if="!(portfolio.rules || []).length" class="empty-table">还没有价格规则。候选、想法和回测只能预填，确认后才启用。</div><div v-else class="table-scroll"><table class="watch-table"><thead><tr><th>证券 / 规则</th><th>状态</th><th>成功次数</th><th>最近判断</th><th>下一交易日</th><th></th></tr></thead><tbody><tr v-for="rule in portfolio.rules" :key="rule.id"><td>{{ rule.name }}<small>{{ ruleKinds[rule.kind] }} · {{ rule.summary }}</small><small v-if="rule.source_note">{{ rule.source_note }}</small></td><td><span class="tag" :class="rule.status === 'active' ? 'green' : rule.status === 'paused' ? 'amber' : ''">{{ ruleStatuses[rule.status] }}</span></td><td>{{ rule.success_count }} / {{ rule.max_orders }}</td><td>{{ rule.last_event ? rule.last_event.bar_date + ' · ' + rule.last_event.reason : '尚未判断' }}</td><td>{{ rule.next_day || '等待下一已完成交易日' }}</td><td><div class="button-row"><button v-if="rule.status === 'active'" class="secondary small" :disabled="!canEdit" @click="ruleAction(rule.id, 'pause')">暂停</button><button v-if="rule.status === 'paused'" class="secondary small" :disabled="!canEdit" @click="ruleAction(rule.id, 'resume')">恢复</button><button v-if="rule.status !== 'stopped'" class="secondary small" :disabled="!canEdit" @click="ruleAction(rule.id, 'stop')">停止</button><button v-if="rule.status !== 'stopped'" class="secondary small" :disabled="!canEdit" @click="ruleAction(rule.id, 'stop', true)">停止并撤单</button></div></td></tr></tbody></table></div></section><section class="panel"><div class="filter-toolbar"><div class="segmented"><button :class="{ selected: accountTab === 'orders' }" @click="accountTab = 'orders'">模拟委托</button><button :class="{ selected: accountTab === 'ledger' }" @click="accountTab = 'ledger'">现金流水</button></div><div class="toolbar-spacer"></div><button class="secondary small" :disabled="busy || !canEdit" @click="runJob('match')"><RefreshCw :size="14" />检查价格规则并撮合</button></div><div class="table-scroll"><table v-if="accountTab === 'orders'" class="watch-table"><thead><tr><th>证券</th><th>方向 / 数量</th><th>限价 / 成交价</th><th>费用</th><th>状态</th><th>创建 / 成交时间</th><th></th></tr></thead><tbody><tr v-for="o in portfolio.orders" :key="o.id"><td>{{ o.name }}<small>{{ o.instrument_id }}</small><small v-if="props.publicMode&&o.status==='filled'" class="mono" :title="o.id">成交标识 {{o.id}}</small></td><td :class="o.side === 'buy' ? 'up' : 'down'">{{ o.side === 'buy' ? '买入' : '卖出' }} {{ o.quantity }}</td><td>{{ fmt(o.limit_price) }} / {{ fmt(o.fill_price) }}</td><td>{{ fmt(o.fees) }}</td><td><span class="tag" :class="o.status === 'filled' ? 'green' : o.status === 'rejected' ? 'red' : ''">{{ statuses[o.status] }}</span><small>{{ o.reason || o.pending_note }}</small><small v-if="o.rule_id">规则 {{ o.rule_id.slice(0, 8) }}</small></td><td>{{ dt(o.created_at) }}<small>{{ o.filled_at ? dt(o.filled_at) : '等待后续完整K线' }}</small></td><td><button v-if="o.status === 'pending'" class="text-button" :disabled="!canEdit" @click="action(async () => { await post('/orders/' + o.id + '/cancel'); await loadPage() })">撤销</button></td></tr></tbody></table><table v-else class="watch-table"><thead><tr><th>时间</th><th>说明</th><th class="number">变动</th><th class="number">余额</th><th>账户</th></tr></thead><tbody><tr v-for="l in portfolio.ledger" :key="l.id"><td>{{ dt(l.ts) }}</td><td>{{ l.description }}</td><td class="number">{{ fmt(l.delta) }}</td><td class="number">{{ fmt(l.balance) }}</td><td class="mono">{{ l.account_id.slice(0, 8) }}</td></tr></tbody></table></div><p v-if="accountTab === 'orders' && !portfolio.orders.length" class="empty-table">暂无模拟委托</p></section>
        </template>

        <template v-else-if="page === 'backtest'">
          <!-- 回测实验：设置策略及证据模式，检查收益、费用敏感性与复现结果。 -->
          <section class="panel"><div class="panel-heading"><div><h2>均线趋势策略</h2><p>收盘信号 → 下一根开盘成交 · 策略版本 2.0.0</p></div><span class="tag">确定性历史撮合</span></div><form class="experiment-form" @submit.prevent="runBacktest"><label class="wide-field">研究证券<select v-model="testParams.instrument_id"><option v-for="r in watchlist.filter(r => ['stock', 'etf'].includes(r.asset_class) && ['SH', 'SZ'].includes(r.market))" :key="r.id" :value="r.id">{{ r.name }} {{ r.symbol }}</option></select></label><label>虚拟初始资金<input type="number" min="1" step="0.01" required v-model.number="testParams.initial_cash" /></label><label>短均线<input type="number" min="2" max="100" required v-model.number="testParams.fast" /></label><label>长均线<input type="number" min="3" max="250" required v-model.number="testParams.slow" /></label><label>样本内比例<input type="number" min="0.5" max="0.9" step="0.05" required v-model.number="testParams.train_ratio" /></label><label>收盘止损比例<input type="number" min="0.01" max="0.5" step="0.01" required v-model.number="testParams.stop_loss_pct" /></label><label>开始日期<input type="date" v-model="testParams.start_date" /></label><label>结束日期<input type="date" v-model="testParams.end_date" /></label><label class="check-label"><input type="checkbox" v-model="testParams.strict" />严格证据模式</label><button class="primary" :disabled="busy || !canEdit"><FlaskConical :size="16" />{{ busy ? '计算中' : '运行回测' }}</button></form><div class="table-footer">严格模式需要逐日历史状态和公司行动证据；取消后仅作为未验证实验，始终保留数据缺口。</div></section>
          <section v-if="selectedBacktest" class="panel backtest-result"><div class="panel-heading"><h2>实验结果 <span :class="['tag', selectedBacktest.validated ? 'green' : 'amber']">{{ selectedBacktest.validated ? '证据完整' : '未验证实验' }}</span></h2><div class="button-row"><button class="secondary small" :disabled="!canEdit" @click="openRule({ instrument_id: selectedBacktest.params.instrument_id, kind: 'ma_forward', fast: selectedBacktest.params.fast, slow: selectedBacktest.params.slow, stop_loss_pct: selectedBacktest.params.stop_loss_pct, source_kind: 'backtest', source_id: selectedBacktest.id })">按此参数向前跟随</button><button class="secondary small" @click="action(async () => { const r = await post('/backtests/' + selectedBacktest.id + '/reproduce'); notify(r.identical ? '重演一致，快照与交易记录校验通过' : '重演不一致，请检查策略版本') })">快照重演</button><button class="secondary small" @click="exportData('/backtests/' + selectedBacktest.id + '/export', 'StockAI回测.json')"><ArrowDownToLine :size="14" />导出</button></div></div><div class="account-metrics"><div><span>全样本收益</span><b :class="direction(selectedBacktest.summary.return_pct)">{{ pct(selectedBacktest.summary.return_pct) }}</b></div><div><span>最大回撤</span><b>{{ fmt(selectedBacktest.summary.max_drawdown_pct) }}%</b></div><div><span>完整交易次数</span><b>{{ selectedBacktest.summary.round_trips }}</b></div><div><span>胜率 / 盈利因子</span><b>{{ fmt(selectedBacktest.summary.win_rate_pct) }}% / {{ fmt(selectedBacktest.summary.profit_factor) }}</b></div></div><ResearchChart kind="equity" :curve="selectedBacktest.full.equity" /><div class="table-scroll"><table class="watch-table"><thead><tr><th>验证区间</th><th>收益</th><th>回撤</th><th>完整交易</th><th>区间</th></tr></thead><tbody><tr v-for="(r, idx) in [selectedBacktest.in_sample, selectedBacktest.out_of_sample]" :key="idx"><td>{{ idx ? '样本外' : '样本内' }}</td><td>{{ pct(r.metrics.return_pct) }}</td><td>{{ fmt(r.metrics.max_drawdown_pct) }}%</td><td>{{ r.metrics.round_trips }}</td><td>{{ r.range[0].slice(0, 10) }} — {{ r.range[1].slice(0, 10) }}</td></tr></tbody></table></div><div class="prose-panel"><h3>成本敏感性</h3><div class="sensitivity-grid"><div v-for="c in selectedBacktest.cost_sensitivity" :key="c.fee_multiplier"><small>{{ c.fee_multiplier }} 倍费用</small><strong>{{ pct(c.metrics.return_pct) }}</strong><small>累计费用 ¥{{ fmt(c.metrics.fees) }}</small></div></div><h3>可复核交易</h3><div class="table-scroll"><table class="watch-table"><thead><tr><th>信号时间</th><th>成交时间</th><th>方向</th><th>数量</th><th>价格</th><th>费用</th><th>现金</th></tr></thead><tbody><tr v-for="(t, idx) in selectedBacktest.full.trades" :key="idx"><td>{{ dt(t.signal_ts) }}</td><td>{{ dt(t.ts) }}</td><td>{{ t.side === 'buy' ? '买' : '卖' }}</td><td>{{ t.quantity }}</td><td>{{ fmt(t.price) }}</td><td>{{ fmt(t.fees) }}</td><td>{{ fmt(t.cash) }}</td></tr></tbody></table></div><h3>解释边界与缺失证据</h3><p v-for="w in selectedBacktest.warnings" :key="w" class="warning-text">{{ w }}</p><small>{{ selectedBacktest.full.execution }}</small><div class="snapshot-id">数据 SHA256 · {{ selectedBacktest.data_hash }}</div></div></section>
          <section class="panel"><div class="panel-heading"><h2>实验档案</h2><span class="muted">{{ experiments.length }} 次</span></div><div v-if="!experiments.length" class="empty-inline"><FlaskConical :size="22" /><span>还没有历史实验。需要先获取足够的真实历史K线。</span></div><div v-for="r in experiments" :key="r.id" class="report-list-row" @click="action(async () => selectedBacktest = await api('/backtests/' + r.id))"><span class="report-icon"><FlaskConical :size="20" /></span><div><strong>{{ r.params.instrument_id }} · MA{{ r.params.fast }} / MA{{ r.params.slow }}</strong><small>{{ dt(r.created_at) }} · {{ r.validated ? '证据完整' : '未验证实验' }}</small></div><span :class="direction(r.summary.return_pct)">{{ pct(r.summary.return_pct) }}</span><ChevronRight :size="16" /></div></section>
        </template>

        <template v-else-if="page === 'jobs'">
          <div class="task-center">
            <div class="task-summary" aria-label="任务状态概览"><article v-for="state in ['running','queued','success','failed']" :key="state"><span>{{statuses[state]}}</span><strong>{{allTasks.filter(j=>j.status===state).length}}</strong></article></div>
            <div class="schedule-grid"><section v-for="p in jobData.schedule.plans" :key="p.kind" class="panel schedule-card"><span :class="['schedule-icon', { gold: p.kind === 'full' }]"><component :is="p.kind === 'full' ? BookOpen : p.kind === 'standard' ? Sparkles : ChartCandlestick" :size="20" /></span><strong>{{ p.label }}</strong><div class="schedule-time">{{ p.time }}<small>北京时间</small></div><p>{{ p.rule }}</p><div class="button-row"><span :class="['tag',p.enabled===false?'':'green']">{{p.enabled===false?'自动计划未开启':jobData.schedule.enabled?'自动计划已开启':'自动计划未开启'}}</span><button class="text-button" :disabled="busy || !canEdit" @click="runJob(p.kind)">立即运行 <ArrowRight :size="14" /></button></div></section></div>
            <div class="info-strip"><Clock3 :size="18" /><span>内建调度 · {{ jobData.schedule.enabled ? '已开启自动计划' : '自动计划未开启，仍可手动运行' }}。只补跑当天到期任务；同日同类报告复用已有归档。{{props.publicMode?'桌面关闭且后台退出后不执行本机任务。':''}}</span><button class="text-button" @click="go('settings')">配置计划</button></div>
            <section class="panel"><div class="panel-heading"><div><h2>任务记录</h2><p>研究报告、模型测试、助手和历史实验{{canMaintain?'，以及共享资料采集':''}}</p></div><div class="button-row"><button v-if="canMaintain" class="secondary small" :disabled="busy" @click="runJob('sync')"><RefreshCw :size="14" />数据同步</button><button class="secondary small" :disabled="busy" @click="loadPage"><RefreshCw :size="14" />刷新</button></div></div>
              <div class="filter-toolbar"><div class="search-box"><Search :size="15"/><input v-model="taskSearch" aria-label="搜索任务" placeholder="搜索任务、错误或编号"/></div><select v-model="taskStatus" aria-label="任务状态"><option value="">全部状态</option><option v-for="state in ['queued','running','success','partial','failed','cancelled','interrupted','missed']" :key="state" :value="state">{{statuses[state]}}</option></select><select v-if="canMaintain&&props.publicMode" v-model="taskScope" aria-label="任务范围"><option value="">全部任务</option><option value="personal">我的任务</option><option value="shared">共享资料任务</option></select></div>
              <div class="table-scroll"><table class="watch-table task-table"><thead><tr><th>任务 / 创建时间</th><th>状态</th><th>开始 / 完成</th><th>模型调用</th><th>结果 / 原因</th><th>操作</th></tr></thead><tbody><tr v-for="j in visibleTasks" :key="j.key" :class="{'task-selected':selectedTaskKey===j.key}"><td><strong>{{modeNames[j.kind]||j.kind}}</strong><small>{{dt(j.created_at)}} · {{j.scope==='shared'?'共享资料':j.automatic?'自动计划':'手动任务'}}</small></td><td><span :class="['tag',['failed','interrupted','missed'].includes(j.status)?'red':j.status==='success'?'green':'amber']"><LoaderCircle v-if="j.status==='running'" class="spin" :size="11"/>{{statuses[j.status]||j.status}}</span></td><td>{{j.started_at?dt(j.started_at):'等待执行'}}<small>{{j.finished_at?dt(j.finished_at):'尚未结束'}}</small></td><td>{{j.scope==='shared'?'不调用模型':j.attempts??'—'}}</td><td class="error-cell">{{j.error||taskResult(j)?.message||(j.status==='success'?'执行完成':j.status==='running'?'正在处理':j.status==='queued'?'等待执行':j.retry_reason||'查看任务详情')}}<small v-if="j.retry_reason&&['failed','cancelled'].includes(j.status)">{{j.retry_reason}}</small></td><td><div class="button-row"><button class="text-button" @click="selectedTaskKey=selectedTaskKey===j.key?'':j.key">{{selectedTaskKey===j.key?'收起':'详情 / 日志'}}</button><button v-if="j.result_route" class="text-button" @click="openTaskResult(j)">{{j.result_label}}</button><button v-if="canRetryTask(j)" class="text-button" :disabled="busy||!canEdit" @click="retryTask(j)">{{j.retry_label||'重试'}}</button></div></td></tr></tbody></table></div><p v-if="!visibleTasks.length" class="empty-table">{{allTasks.length?'没有符合筛选条件的任务':'尚无任务，点击上方「立即运行」创建第一项研究任务。'}}</p>
            </section>
            <section v-if="selectedTask" class="panel task-detail"><div class="panel-heading"><h2>{{modeNames[selectedTask.kind]||selectedTask.kind}} · 任务详情</h2><button class="text-button" @click="selectedTaskKey=''">收起详情</button></div><dl><dt>任务编号</dt><dd>{{selectedTask.id}}</dd><dt>逻辑标识</dt><dd>{{selectedTask.logical_key}}</dd><dt>状态</dt><dd>{{statuses[selectedTask.status]||selectedTask.status}}</dd><dt v-if="selectedTask.error">执行说明</dt><dd v-if="selectedTask.error">{{selectedTask.error}}</dd></dl><details v-if="taskResult(selectedTask)"><summary>查看已保存的任务结果</summary><pre>{{JSON.stringify(taskResult(selectedTask),null,2)}}</pre></details></section>
            <section class="panel"><div class="panel-heading"><h2>{{selectedTask?'当前任务运行日志':'最近运行日志'}}</h2><div class="button-row"><button v-if="selectedTask" class="text-button" @click="selectedTaskKey=''">查看全部日志</button><Terminal :size="17" class="muted"/></div></div><div class="log-view"><p v-if="!taskLogs.length">尚无运行日志。任务创建、开始和完成后会在此记录。</p><div v-for="l in taskLogs" :key="l.scope+':'+l.id" :class="{'log-error':l.level==='error'}"><time>{{dt(l.ts)}}</time><span>{{l.level.toUpperCase()}}</span>{{l.message}}</div></div></section>
          </div>
        </template>

        <template v-else-if="page === 'settings' && config">
          <AppearanceSettings />
          <div class="settings-grid"><section class="panel settings-panel"><div class="panel-heading"><div><h2>本地 AI</h2><p>结构化事实输入，中文解释输出</p></div><Activity :size="18" /></div><div class="form-grid"><label class="span-two">Ollama 地址<input v-model="config.ollama_url" type="url" /></label><label class="span-two">模型名称<input v-model="config.model" /></label><label>调用超时（秒）<input v-model.number="config.ai_timeout" type="number" min="10" max="600" /></label><label class="check-label"><input type="checkbox" v-model="config.ai_enabled" />启用 AI 解释</label></div><div :class="['data-status', { warning: !model.available }]">{{ model.message }}</div><button class="text-button" @click="action(async () => model = await api('/model/health'))"><RefreshCw :size="14" />检查模型</button></section><section class="panel settings-panel"><div class="panel-heading"><div><h2>预算与费用假设</h2><p>全部费用以人民币估算</p></div><Wallet :size="18" /></div><div class="form-grid"><label>单只含费预算（≤1000）<input type="number" min="1" max="1000" v-model.number="config.budget" /></label><label>滑点（基点）<input type="number" min="0" max="500" v-model.number="config.fees.slippage_bps" /></label><label>股票佣金率<input type="number" step="0.00001" v-model.number="config.fees.stock_commission" /></label><label>股票最低佣金<input type="number" step="0.01" v-model.number="config.fees.stock_minimum" /></label><label>ETF 佣金率<input type="number" step="0.00001" v-model.number="config.fees.etf_commission" /></label><label>ETF 最低佣金<input type="number" step="0.01" v-model.number="config.fees.etf_minimum" /></label><label>股票过户费率<input type="number" step="0.00001" v-model.number="config.fees.stock_transfer" /></label><label>股票卖出印花税率<input type="number" step="0.00001" v-model.number="config.fees.stock_stamp_sell" /></label><label class="check-label span-two"><input type="checkbox" v-model="config.fees.confirmed" />已按券商及产品规则核对费率</label><label class="span-two">费用说明<textarea v-model="config.fees.note" rows="2"></textarea></label></div></section><section class="panel settings-panel"><div class="panel-heading"><div><h2>采集与任务计划</h2><p>唯一调度源 · Asia/Shanghai</p></div><Clock3 :size="18" /></div><div class="form-grid"><label>早报<input type="time" v-model="config.morning_time" /></label><label>收盘跟踪<input type="time" v-model="config.closing_time" /></label><label>周日复盘<input type="time" v-model="config.weekly_time" /></label><label class="check-label"><input type="checkbox" v-model="config.scheduler_enabled" />启用内建调度</label><label>最低日成交额（元）<input type="number" min="0" v-model.number="config.min_daily_amount" /></label><label>风险核验有效期（小时）<input type="number" min="1" max="72" v-model.number="config.max_risk_age_hours" /></label></div><p class="help-text">主数据来自交易所渠道，日线主源为东方财富、股票备用源为新浪；接口失败按未知处理。电脑关闭期间不会执行任务。</p></section><section class="panel settings-panel"><div class="panel-heading"><div><h2>访问与安全</h2><p>局域网访问默认关闭</p></div><LockKeyhole :size="18" /></div><div class="form-grid"><label class="check-label span-two"><input type="checkbox" v-model="config.lan_enabled" />开启已鉴权的局域网访问（需重启）</label><label class="span-two">允许的内网来源（CIDR，以逗号分隔）<input :value="config.allowed_networks.join(', ')" @change="config.allowed_networks = ($event.target as HTMLInputElement).value.split(',').map(x => x.trim()).filter(Boolean)" /></label></div><label class="proxy-label">内网反向代理主机名（可选）<input v-model="config.proxy_hostname" placeholder="research.home.arpa" /></label><p class="help-text">当前监听：{{ runtime.runtime_host }}。启用前必须设置管理员密码，防火墙仅放行指定网段与服务端口；Ollama 仍只允许本机连接。</p><div class="button-row"><button class="secondary small" :disabled="!canEdit" @click="openModal('password', { role: 'admin', password: '' })">设置访问密码</button><span class="tag" :class="runtime.lan_auth_configured ? 'green' : 'amber'">{{ runtime.lan_auth_configured ? '鉴权已配置' : '鉴权未配置' }}</span></div></section><section class="panel settings-panel"><div class="panel-heading"><div><h2>邮件通知</h2><p>配置后生效，界面通知始终保存</p></div><Bell :size="18" /></div><div class="form-grid"><label class="check-label span-two"><input type="checkbox" v-model="config.email_enabled" />启用邮件通知</label><label>SMTP 服务器<input v-model="config.smtp_host" /></label><label>SSL 端口<input type="number" v-model.number="config.smtp_port" /></label><label>发件用户<input v-model="config.smtp_user" /></label><label>接收邮箱<input v-model="config.email_to" type="email" /></label></div><p class="help-text">密码只从服务进程环境变量 STOCKAI_SMTP_PASSWORD 读取，不写入配置或传入模型。</p></section><section class="panel settings-panel"><div class="panel-heading"><div><h2>数据管理</h2><p>数据库、研究报告与快照一同备份</p></div><Database :size="18" /></div><div class="button-row"><button class="secondary" :disabled="busy || !canEdit" @click="action(async () => { await post('/backups'); backups = await api('/backups'); notify('备份完成，完整性与关联校验通过') })"><Database :size="15" />创建备份</button><button class="secondary" @click="exportData('/config/export', 'StockAI配置.json')"><ArrowDownToLine :size="15" />导出配置</button><label class="secondary file-button">导入配置<input type="file" accept=".json" :disabled="!canEdit" @change="readFile($event, 'config')" /></label></div><div v-for="b in backups.slice(0, 6)" :key="b.id" class="backup-row"><div><strong>{{ dt(b.created_at) }}</strong><small>{{ fmt(b.size / 1024, 1) }} KB · {{ b.counts?.reports || 0 }} 份报告</small></div><button class="text-button" @click="exportData('/backups/' + b.id + '/download', 'StockAI备份-' + b.id + '.zip')">下载</button><button class="text-button danger-text" :disabled="!canEdit" @click="openModal('restore', { id: b.id, confirmation: '' })">恢复</button></div><p v-if="!backups.length" class="help-text">尚无备份。每周日复盘后也会创建自动备份。</p></section></div>
        </template>
        <footer class="page-footer"><span><ShieldCheck :size="13" />仅供研究与模拟交易 · 不接入真实账户</span><span>StockAI {{ props.publicMode ? (dashboard?.version || "3.1") : "2.0" }}</span></footer>
      </main>
    </div>
    <div v-if="toast" class="toast" role="status"><Check :size="17" />{{ toast }}</div>

    <div v-if="modal" class="modal-scrim"><form class="modal" :class="{ 'wide-modal': modal === 'verify' }" role="dialog" aria-modal="true" :aria-label="({ add: '添加证券', account: '创建模拟账户', order: '模拟委托', rule: '价格规则', password: '访问密码', restore: '恢复备份', verify: '官方证据核验' } as any)[modal]" @submit.prevent="submitModal"><div class="modal-heading"><h2>{{ ({ add: '添加证券', account: '创建模拟账户', order: '创建模拟委托', rule: '启用价格规则', password: '设置访问密码', restore: '恢复备份', verify: '记录官方核验证据' } as any)[modal] }}</h2><button type="button" class="icon-button" @click="modal = ''" aria-label="关闭"><X :size="19" /></button></div><p v-if="modalError" class="inline-error" role="alert">{{ modalError }}</p>
      <template v-if="modal === 'add'"><div class="form-grid"><label>市场<select v-model="form.market"><option v-for="m in ['SH', 'SZ', 'HK', 'US', 'FUND']" :key="m">{{ m }}</option></select></label><label>资产类别<select v-model="form.asset_class"><option value="stock">股票</option><option value="etf">场内ETF</option><option value="fund">场外基金</option><option value="index">指数</option></select></label><label>证券代码<input v-model="form.symbol" required /></label><label>历史/备注名称<input v-model="form.name" required /></label><label class="span-two">分组<input v-model="form.group_name" required /></label></div><p class="help-text">按市场与代码去重。新添加的证券状态为未核验，不直接进入候选。</p></template>
      <template v-if="modal === 'account'"><label>账户名称<input v-model="form.name" required maxlength="80" /></label><label>虚拟初始资金（人民币）<input v-model.number="form.initial_cash" type="number" min="0.01" max="100000000" step="0.01" required placeholder="请自行填写" /></label><p class="help-text">这是一笔虚拟资金，不代表真实账户资产。</p></template>
      <template v-if="modal === 'order'"><label>虚拟账户<select v-model="form.account_id" required><option v-for="a in portfolio.accounts" :key="a.id" :value="a.id">{{ a.name }} · 现金 ¥{{ fmt(a.cash) }}</option></select></label><label>证券<select v-model="form.instrument_id" required><option v-for="r in watchlist.filter(r => ['SH', 'SZ'].includes(r.market) && ['stock', 'etf'].includes(r.asset_class))" :key="r.id" :value="r.id">{{ r.name }} {{ r.symbol }}</option></select></label><div class="form-grid"><label>方向<select v-model="form.side"><option value="buy">模拟买入</option><option value="sell">模拟卖出</option></select></label><label>数量<input type="number" min="1" required v-model.number="form.quantity" /></label><label>限价<input type="number" min="0.001" step="0.001" required v-model.number="form.limit_price" /></label><label v-if="form.side === 'buy'">收盘止损价（可选）<input type="number" step="0.001" v-model.number="form.stop_loss" /></label></div><p class="help-text">提交后进入待成交队列，不会用创建前的历史行情立即成交。买入总成本含费不得超过单只预算。</p></template>
      <template v-if="modal === 'rule'"><label>虚拟账户<select v-model="form.account_id" required><option v-for="a in portfolio.accounts" :key="a.id" :value="a.id">{{ a.name }}</option></select></label><label>证券<select v-model="form.instrument_id" required><option v-for="r in watchlist.filter(r => ['SH', 'SZ'].includes(r.market) && ['stock', 'etf'].includes(r.asset_class))" :key="r.id" :value="r.id">{{ r.name }} {{ r.symbol }}</option></select></label><label>种类<select v-model="form.kind"><option value="close_at_or_below">收盘落到后买入</option><option value="close_at_or_above">收盘升到后卖出</option><option value="close_stop">收盘止损卖出</option><option value="ma_forward">均线向前跟随</option></select></label><div v-if="form.kind !== 'ma_forward'" class="form-grid"><label>{{ form.kind === 'close_stop' ? '止损价' : '触发价' }}<input type="number" min="0.001" step="0.001" required v-model.number="form[form.kind === 'close_stop' ? 'stop_price' : 'trigger_price']" /></label><label>限价<input type="number" min="0.001" step="0.001" required v-model.number="form.limit_price" /></label><label v-if="!(form.kind === 'close_stop' && form.sell_all)">数量<input type="number" min="1" required v-model.number="form.quantity" /></label><label v-if="form.kind === 'close_at_or_below'">收盘止损价（可选）<input type="number" step="0.001" v-model.number="form.stop_loss" /></label><label v-if="form.kind === 'close_stop'" class="check-label">全部可卖<input type="checkbox" v-model="form.sell_all" /></label></div><div v-else class="form-grid"><label>短均线<input type="number" min="2" max="100" required v-model.number="form.fast" /></label><label>长均线<input type="number" min="3" max="250" required v-model.number="form.slow" /></label><label>收盘止损比例<input type="number" min="0.01" max="0.5" step="0.01" required v-model.number="form.stop_loss_pct" /></label></div><div class="form-grid"><label>截止日期（可选）<input type="date" v-model="form.expires_on" /></label><label>成功次数上限（留空用默认）<input type="number" min="1" max="100" v-model.number="form.max_orders" /></label></div><p v-if="form.source_note" class="help-text">{{ form.source_note }}</p><p class="help-text">确认后才启用。15:00 之后启用的规则不使用当天收盘。历史回测成交不会写入模拟账户。</p></template>
      <template v-if="modal === 'password'"><label>角色<select v-model="form.role"><option value="admin">管理员</option><option value="reader">只读访问者</option></select></label><label>访问密码<input type="password" v-model="form.password" minlength="12" maxlength="200" required autocomplete="new-password" /></label><p class="help-text">至少12位。修改密码会使已有局域网会话失效。</p></template>
      <template v-if="modal === 'restore'"><p>此操作将替换当前数据库与配置。程序会先保留恢复前备份，再校验记录数量和关联关系。</p><label>键入“恢复备份”确认<input v-model="form.confirmation" required pattern="恢复备份" autocomplete="off" /></label></template>
      <template v-if="modal === 'verify'"><p class="help-text">请实际打开并核对官方来源。仅粘贴首页链接不能证明交易状态安全，核验记录会带时间保存。</p><div class="form-grid"><label>最新官方名称<input v-model="form.name" required /></label><label>交易状态<select v-model="form.status"><option value="active">正常上市交易</option><option value="suspended">停牌</option><option value="delisted">已退市</option><option value="unknown">未知</option></select></label><label>风险核验<select v-model="form.risk_flag"><option value="clear">已核对，无已知警示</option><option value="st">ST / *ST</option><option value="warning">存在风险公告</option><option value="unknown">未知</option></select></label><label>最小买入数量<input type="number" min="1" v-model.number="form.rules.lot_size" required /></label><label>数量阶梯<input type="number" min="1" v-model.number="form.rules.quantity_step" required /></label><label>价格最小步长<input v-model="form.rules.price_tick" required /></label><label>卖出交收限制<select v-model.number="form.rules.settlement_days"><option :value="1">T+1</option><option :value="0">T+0（须产品支持）</option></select></label><label>常规涨跌幅限制<input type="number" min="0.01" max="0.5" step="0.01" v-model.number="form.rules.limit_pct" required /></label><label class="span-two">官方产品规则链接<input type="url" v-model="form.rules.source_url" required /></label><label class="span-two">官方上市与名称证据<input type="url" v-model="form.evidence.listing_url" required /></label><label class="span-two">官方停复牌状态证据<input type="url" v-model="form.evidence.status_url" required /></label><label class="span-two">官方公告核查证据<input type="url" v-model="form.evidence.announcement_url" required /></label><label class="span-two">人工核对结论<textarea v-model="form.evidence.review_note" rows="3" required></textarea></label><label class="check-label span-two"><input type="checkbox" v-model="form.rules.verified" required />我已核对上述证据与该产品适用的交易规则</label></div></template>
      <div class="modal-actions"><button type="button" class="secondary" :disabled="busy" @click="modal = ''">取消</button><button class="primary" :disabled="busy"><LoaderCircle v-if="busy" class="spin" :size="15" />{{ modal === 'restore' ? '确认恢复' : '保存' }}</button></div>
    </form></div>
  </div>
  <ConfirmDialog :message="confirmText" @choose="settleConfirm" />
</template>
