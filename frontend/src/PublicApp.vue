<script setup lang="ts">
// 公网入口：管理账号会话、个人模型和偏好，并为研究界面提供已验证身份。
import { computed, defineAsyncComponent, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { Activity, ArrowLeft, ArrowRight, ChartNoAxesCombined, Check, ChevronRight, KeyRound, LoaderCircle, LogOut, Menu, Plus, Settings2, ShieldCheck, Trash2, UserRound } from 'lucide-vue-next'
import CoreApp from './App.vue'
import WorkspaceNav from './components/WorkspaceNav.vue'
import { workspaceTitle } from './workspaceNavigation'
import ConfirmDialog from './components/ConfirmDialog.vue'
import AppearanceSettings from './components/AppearanceSettings.vue'
import { api, post, download, setCsrfCookieName, clearPrivateRequests } from './api'
import { loginCredentialSupport, loadLoginCredentials, saveLoginCredentials, forgetLoginCredentials } from './loginCredentials'
import './public.css'
// 按需加载研究增强界面，保持普通工作台首次加载轻量。
const SystemSettings = defineAsyncComponent(() => import('./SystemSettings.vue'))
const ResearchWorkbench = defineAsyncComponent(() => import('./ResearchWorkbench.vue'))
const AssistantDock = defineAsyncComponent(() => import('./components/AssistantDock.vue'))
const assistantDock=ref<any>(null),assistantContext=ref<{kind:string,id:string,report_id?:string}|null>(null)
const systemSettings=ref<any>(null),core=ref<any>(null)
const workbench = ref<any>(null), workbenchContext = ref<{kind:string,id:string,report_id?:string}|null>(null)
const workbenchViews = new Set(['readiness','usage','alerts','journals','portfolio','experiments'])

// 保存公开账号状态；会话过期或退出时同步清理私密页面缓存。
const user = ref<any>(null), booting = ref(true), busy = ref(false), error = ref(''), notice = ref('')
const draftClient=crypto.randomUUID(),preferencesBaseline=ref('')
const desktopMode=ref(false),needsSetup=ref(false)
const rememberLogin=ref(true)
const rememberCredentials=ref(false),savedCredentials=ref(false),credentialSupport=ref<'native'|'browser'|null>(null)
// 从哈希读取认证或管理页面，同时去除验证链接中的查询参数。
const route = ref(location.hash.slice(1).split('?')[0] || 'dashboard')
const navigationOpen = ref(false), navigationToggle = ref<HTMLButtonElement|null>(null)
const currentTitle = computed(() => workspaceTitle(route.value))
function closeNavigation() { navigationOpen.value = false; void nextTick(() => requestAnimationFrame(() => navigationToggle.value?.focus())) }
// 白名单区分管理和认证页面，其他导航交由核心研究工作台处理。
const management = new Set(['models', 'preferences', 'account', 'admin', 'system', 'assistant', ...workbenchViews])
const authRoutes = new Set(['login', 'register', 'request-reset', 'reset-password', 'verify-email'])
// 将模型、偏好和账号管理页面与核心研究工作台区分开。
const view = computed(() => management.has(route.value) ? route.value : 'workspace')
// 仅接受已定义的认证路由，其他情况使用登录表单。
const authView = computed(() => needsSetup.value ? 'register' : authRoutes.has(route.value) ? route.value : 'login')
const form = ref({ email: '', password: '', invitation: new URLSearchParams(location.hash.split('?')[1]).get('invitation') || '', confirmation: '' })
// 各管理页面共用的服务端快照，登出时统一清空。
const profiles = ref<any[]>([]), preferences = ref<any>(null), users = ref<any[]>([]), invitations = ref<any[]>([]), backups = ref<any[]>([]), siteJobs = ref<any[]>([]), personalJobs = ref<any[]>([])
const editor = ref<any>(null), invite = ref<any>(null), exportPrompt = ref(''), exportPassword = ref('')
// 仅保存可公开的供应商预设，凭据不会进入预设或列表展示字段。
const presets = ref<any>({ cn: { base_url: 'https://api.minimax.cn/v1', model: 'MiniMax-M3.1-Flash' }, global: { base_url: 'https://api.minimax.io/v1', model: 'MiniMax-M3.1-Flash' } })
const fees: Record<string, string> = { stock_commission: '股票佣金率', stock_minimum: '股票最低佣金（元）', etf_commission: 'ETF佣金率', etf_minimum: 'ETF最低佣金（元）', stock_transfer: '股票过户费率', stock_stamp_sell: '股票卖出印花税率', slippage_bps: '模拟滑点（基点）' }
const providerNames: Record<string, string> = { minimax: 'MiniMax', openai_compatible: 'OpenAI 兼容服务', ollama: '服务器 Ollama' }
const jobStates: Record<string, string> = { queued: '等待中', running: '运行中', success: '已完成', failed: '失败', cancelled: '已取消' }
// 将站点和任务时间统一显示为北京时间。
const date = (value: string) => value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
let timer: ReturnType<typeof setInterval>
const confirmText = ref('')
let confirmResolve: ((answer: boolean) => void) | null = null
// 通过共享确认弹窗收集选择，并返回可等待的布尔结果。
function askConfirm(message: string): Promise<boolean> { confirmText.value = message; return new Promise(resolve => { confirmResolve = resolve }) }
// 关闭弹窗并释放对应的确认回调。
function settleConfirm(answer: boolean) { confirmText.value = ''; confirmResolve?.(answer); confirmResolve = null }

// 切换管理页面时清除密码和编辑草稿，再加载目标视图。
async function go(name: string) { const destination=name.split('?')[0]!;if(destination!==route.value&&((view.value==='models'&&editor.value)||(view.value==='preferences'&&JSON.stringify(preferences.value)!==preferencesBaseline.value))&&!await askConfirm('当前有未保存编辑，确认离开并清除临时输入？'))return;if(destination!==route.value&&route.value==='system'&&systemSettings.value&&!await systemSettings.value.guard())return;if(destination!==route.value&&view.value==='workspace'&&core.value&&!await core.value.guard())return;if(destination!==route.value&&workbenchViews.has(route.value)&&workbench.value&&!await workbench.value.guard())return;error.value = ''; notice.value = ''; editor.value = null; form.value.password = ''; if(navigationOpen.value)closeNavigation(); route.value = destination; location.hash = name; void loadView() }
// 同步浏览器历史导航，并按新路由刷新管理数据。
async function hashChanged() { const destination=location.hash.slice(1).split('?')[0]||'dashboard';if(destination!==route.value&&route.value==='system'&&systemSettings.value&&!await systemSettings.value.guard()){history.replaceState(null,'','#'+route.value);return}if(destination!==route.value&&view.value==='workspace'&&core.value&&!await core.value.guard()){history.replaceState(null,'','#'+route.value);return}if(destination!==route.value&&workbenchViews.has(route.value)&&workbench.value&&!await workbench.value.guard()){history.replaceState(null,'','#'+route.value);return}navigationOpen.value=false;route.value=destination;void loadView() }
// 保存从证券、报告或账户传入的研究上下文，再切换到对应研究页面。
function openWorkbench(name:string,context?:{kind:string,id:string,report_id?:string}){if(name==='assistant'){assistantContext.value=context||null;void assistantDock.value?.open();return}workbenchContext.value=context||null;void go(name)}
// 阻止重复提交，统一处理忙碌状态、错误和成功提示。
async function action(fn: () => Promise<any>, message = '') { if (busy.value) return; busy.value = true; error.value = ''; notice.value = ''; try { const value = await fn(); if (message) notice.value = message; return value } catch (e: any) { if(e.code==='STALE_SESSION')return null;error.value = e.message; if (e.code === 'LOGIN_REQUIRED') user.value = null; return null } finally { busy.value = false } }
// 读取当前登录身份，仅将未登录响应视为正常的匿名状态。
async function refreshUser() { try { user.value = await api('/auth/me', { allowAnonymous: true }) } catch (e: any) { if (e.status !== 401) throw e; clearPrivate() } }
// 空白环境先由本人选择数据来源；已有资料或已选择独立使用时直接进入工作台。
async function initialWorkspaceRoute() { const setup=await api('/me/workspace-setup');return setup.required||setup.first_merge?'system':'dashboard' }
async function enterWorkspace() { await go(await initialWorkspaceRoute()) }
// 退出或会话失效时清除模型、账号、备份和表单中的私密临时数据。
function clearPrivate() { clearPrivateRequests();navigationOpen.value=false;assistantContext.value=null;workbenchContext.value=null; user.value = null; profiles.value = []; preferences.value = null; users.value = []; invitations.value = []; backups.value = []; siteJobs.value = []; personalJobs.value = []; editor.value = null; invite.value = null; exportPassword.value = ''; exportPrompt.value = ''; form.value.password = '' }
// 响应接口层的会话失效事件，回到登录页并尝试刷新 CSRF。
async function sessionExpired() { clearPrivate(); route.value = 'login'; location.hash = 'login'; error.value = '登录已失效，请重新登录。'; try { await api('/auth/csrf') } catch { /* 服务暂不可达时保留登录页，下一次重新加载再恢复连接。 */ } }
// 邮箱验证完成后按管理页面加载本人数据或管理员站点资料。
async function loadView() {
  if (!user.value?.verified) return
  try {
    if (view.value === 'models') { profiles.value = await api('/me/model-profiles'); personalJobs.value = (await api('/me/jobs')).jobs.filter((j: any) => j.kind === 'profile_test'); presets.value = await api('/me/model-presets') }
    if (view.value === 'preferences') {preferences.value = await api('/me/preferences');preferencesBaseline.value=JSON.stringify(preferences.value)}
    if (view.value === 'admin' && user.value.role === 'admin') { const r = await Promise.all([api('/admin/users'), api('/admin/invitations'), api('/admin/backups')]); [users.value, invitations.value, backups.value] = r }
  } catch (e: any) { if(e.code!=='STALE_SESSION')error.value = e.message }
}
// 自动填入用户已选择保存的账号；读取失败仍可手动登录。
async function restoreSavedLogin() {
  if(user.value || needsSetup.value || authView.value!=='login')return
  try {
    const saved=await loadLoginCredentials()
    if(!saved || user.value || authView.value!=='login')return
    savedCredentials.value=true;rememberCredentials.value=true
    if(!form.value.email)form.value.email=saved.email
    if(form.value.email.toLowerCase()===saved.email.toLowerCase()&&!form.value.password)form.value.password=saved.password
  } catch { /* 系统凭据库不可用时保留手动登录入口。 */ }
}
async function clearSavedLogin() {
  await forgetLoginCredentials();savedCredentials.value=false;rememberCredentials.value=false;form.value.password=''
}
// 仅在身份验证成功后保存输入；保存失败不影响已经成功的登录。
async function completeLogin(response:any) {
  const email=form.value.email.trim().toLowerCase(),password=form.value.password
  user.value=response.user;needsSetup.value=false
  let message=''
  try {
    if(rememberCredentials.value){await saveLoginCredentials(email,password);savedCredentials.value=true}
    else if(savedCredentials.value)await clearSavedLogin()
  } catch { message='已登录，但账号密码未能保存，请稍后重试。' }
  finally { form.value.password='' }
  await enterWorkspace()
  if(message)notice.value=message
}
// 按当前认证页面提交登录、邀请注册、邮箱验证或密码重置请求。
async function submitAuth() {
  await action(async () => {
    if (needsSetup.value) { const r = await post('/desktop/setup', { email: form.value.email, password: form.value.password, remember: rememberLogin.value }); await completeLogin(r) }
    else if (authView.value === 'login') { const r = await post('/auth/login', { email: form.value.email, password: form.value.password, remember: rememberLogin.value }); await completeLogin(r) }
    else if (authView.value === 'register') { const r = await post('/auth/register', form.value); form.value.password = ''; notice.value = r.message; route.value = 'login'; location.hash = 'login' }
    else if (authView.value === 'request-reset') { const r = await post('/auth/request-reset', { email: form.value.email }); notice.value = r.message }
    else if (authView.value === 'reset-password') { await post('/auth/reset-password', { token: new URLSearchParams(location.hash.split('?')[1]).get('token'), password: form.value.password }); try { await clearSavedLogin() } catch { /* 旧密码不再具有登录权限。 */ } clearPrivate(); await api('/auth/csrf'); history.replaceState(null, '', '#login'); route.value = 'login'; notice.value = '密码已更新，请重新登录。' }
    else if (authView.value === 'verify-email') { await post('/auth/verify-email', { token: new URLSearchParams(location.hash.split('?')[1]).get('token') }); await refreshUser();const next=await initialWorkspaceRoute(); history.replaceState(null, '', '#'+next); route.value = next; notice.value = '邮箱验证成功。现在可以开始研究。' }
  })
}
// 撤销服务端会话，清理本地私密状态并刷新匿名 CSRF。
async function logout() { await action(async () => { await post('/auth/logout'); clearPrivate(); await api('/auth/csrf'); await go('login'); await restoreSavedLogin() }) }
// 复制已有配置或创建新草稿，始终以空白 API Key 输入框开始编辑。
function editProfile(row: any = null) { editor.value = row ? { ...row, api_key: '', remove_key: false } : { name: '', provider: 'minimax', region: 'cn', model: presets.value.cn.model, base_url: presets.value.cn.base_url, api_key: '', active: true, remove_key: false, allow_simulation_summary: false, allow_trade_details: false, context_window: 16384, output_limit: 4096 } }
// 切换供应商时应用对应预设，清空不适用的地址、模型或凭据。
function changeProvider() { const e = editor.value; if (e.provider === 'minimax') { if (!['cn', 'global'].includes(e.region)) e.region = 'cn'; e.base_url = presets.value[e.region === 'global' ? 'global' : 'cn'].base_url; e.model = presets.value[e.region === 'global' ? 'global' : 'cn'].model } else if (e.provider === 'ollama') { e.base_url = ''; e.api_key = ''; e.model = 'qwen3-14b-16k:latest' } else { e.base_url = ''; e.model = '' } }
// 仅提交允许修改的字段，保存后清空明文 Key 并刷新配置列表。
async function saveProfile() { await action(async () => { const e = editor.value; const value = Object.fromEntries(['name', 'provider', 'region', 'base_url', 'model', 'active', 'allow_simulation_summary', 'allow_trade_details', 'context_window', 'output_limit', 'remove_key'].map(k => [k, e[k]])); if (e.api_key) value.api_key = e.api_key; await api('/me/model-profiles' + (e.id ? '/' + e.id : ''), { method: e.id ? 'PUT' : 'POST', body: JSON.stringify(value) }); e.api_key = ''; editor.value = null; await loadView() }, '模型配置已保存。已有任务仍使用提交时的配置版本。') }
// 确认后删除本人配置及可用凭据，并刷新列表。
async function removeProfile(row: any) { if (!await askConfirm('删除「' + row.name + '」及其可用凭据？尚未发出的相关模型调用将停止。')) return; await action(async () => { await api('/me/model-profiles/' + row.id, { method: 'DELETE' }); await loadView() }, '已删除模型配置及凭据。') }
// 确认可能消耗额度后，将结构化连接测试加入个人任务队列。
async function testProfile(row: any) { if (!await askConfirm('发起一次结构化连接测试？这会调用本人所选模型，可能消耗订阅额度。')) return; await action(async () => { await post('/me/model-profiles/' + row.id + '/test'); await loadView() }, '连接测试已加入个人队列，结果会在下方显示。') }
// 保存个人费用、预算和计划参数，后续任务使用新的配置快照。
async function savePreferences() { await action(async () => { preferences.value = await api('/me/preferences', { method: 'PUT', body: JSON.stringify(preferences.value) });preferencesBaseline.value=JSON.stringify(preferences.value) }, '个人偏好已保存。新设置应用于之后提交的任务。') }
// 由管理员创建一次性邀请，并显示最新邀请状态。
async function createInvite() { await action(async () => { invite.value = await post('/admin/invitations'); await loadView() }) }
// 确认目标账号状态变更后提交管理员操作。
async function toggleUser(row: any) { if (!await askConfirm((row.status === 'disabled' ? '恢复' : '停用') + '账号 ' + row.email + '？停用会撤销登录和未执行任务。')) return; await action(async () => { await post('/admin/users/' + row.id + '/status', { disabled: row.status !== 'disabled' }); await loadView() }) }
// 先验证管理员密码，再用单次授权下载指定加密备份，并立即清理密码。
async function exportBackup() { await action(async () => { const id = exportPrompt.value; const r = await post('/admin/backups/' + id + '/reauth', { password: exportPassword.value }); exportPassword.value = ''; await download('/admin/backups/' + id + '/download', 'StockAI-' + id + '.age', { method: 'POST', headers: { 'X-Reauth-Grant': r.grant } }); exportPrompt.value = '' }, '加密备份已导出，请按保留期限管理下载副本。') }
// 提交密码和确认文字执行本人账号删除，再清理认证状态。
async function eraseAccount() { await action(async () => { const r = await post('/me/erase', { password: form.value.password, confirmation: form.value.confirmation }); try { await clearSavedLogin() } catch { /* 已注销账号无法再用旧记录登录。 */ } form.value.password = ''; form.value.confirmation = ''; clearPrivate(); await api('/auth/csrf'); go('login'); notice.value = r.message }) }
const hasDrafts=computed(()=>!!editor.value || (view.value==='preferences'&&JSON.stringify(preferences.value)!==preferencesBaseline.value) || !!core.value?.dirty || !!workbench.value?.dirty || !!systemSettings.value?.dirty || !!assistantDock.value?.dirty)
watch(hasDrafts,value=>{if(user.value?.verified)void post('/me/client-state',{id:draftClient,dirty:value}).catch(()=>{})})
// 挂载时建立 CSRF 与账号状态；用户编辑期间暂停管理数据轮询。
onMounted(async () => { window.addEventListener('hashchange', hashChanged); try { const health=await fetch('/api/health',{credentials:'same-origin'}).then(r=>{if(!r.ok)throw new Error('本机或站点服务暂不可用');return r.json()});desktopMode.value=health.desktop===true;needsSetup.value=desktopMode.value&&!health.initialized;setCsrfCookieName(health.csrf_cookie||'');if(desktopMode.value)providerNames.ollama='本机 Ollama';await api('/auth/csrf'); if(!needsSetup.value)await refreshUser();credentialSupport.value=await loginCredentialSupport();if(!user.value&&!needsSetup.value)await restoreSavedLogin();if(user.value?.verified&&['dashboard','login','register'].includes(route.value)){route.value=await initialWorkspaceRoute();history.replaceState(null,'','#'+route.value)} await loadView() } catch (e: any) { error.value = e.message } finally { booting.value = false }; timer = setInterval(() => { if(user.value?.verified)void post('/me/client-state',{id:draftClient,dirty:hasDrafts.value}).catch(()=>{}); if (user.value && !busy.value && !editor.value && !document.hidden && ['models', 'admin'].includes(view.value)) void loadView() }, 6000) })
// 集中响应 API 层报告的会话失效事件。
onMounted(() => window.addEventListener('stockai-auth-required', sessionExpired))
// 卸载时解除事件与轮询，并结束等待中的确认。
onBeforeUnmount(() => { window.removeEventListener('hashchange', hashChanged); window.removeEventListener('stockai-auth-required', sessionExpired); clearInterval(timer); if(user.value?.verified)void post('/me/client-state',{id:draftClient,dirty:false}).catch(()=>{}); settleConfirm(false) })
</script>

<template>
<!-- 根据连接、登录与邮箱验证状态选择认证表单或个人工作区。 -->
  <div v-if="booting" class="initial-loading"><LoaderCircle class="spin" /><span>正在连接研究空间</span></div>
  <div v-else-if="!user || authView === 'verify-email' && route === 'verify-email' || route === 'reset-password'" class="public-auth">
    <aside class="auth-story"><a class="brand" href="#login"><span class="brand-mark"><ChartNoAxesCombined :size="24" /></span><b>Stock<span>AI</span></b></a><div><span class="eyebrow">YOUR PRIVATE RESEARCH SPACE</span><h1>让研究有积累，<br />让判断有依据。</h1><p>自选、研究报告、模拟交易与历史实验，<br />在属于你的空间里，连接成连续的研究。</p><div class="auth-principles"><span><ShieldCheck :size="17" />个人数据独立保存</span><span><KeyRound :size="17" />使用自己的模型配置</span><span><Activity :size="17" />行情、结论与原始证据关联</span></div></div><small>StockAI · 仅供信息研究与模拟交易</small></aside>
    <main class="auth-form-area"><form class="public-auth-card" @submit.prevent="submitAuth"><span class="eyebrow">STOCKAI RESEARCH</span><h1>{{ needsSetup ? '创建本机研究空间' : { login: '欢迎回来', register: '创建研究空间', 'request-reset': '找回密码', 'reset-password': '设置新密码', 'verify-email': '验证你的邮箱' }[authView] }}</h1><p class="auth-description">{{ needsSetup ? '首次创建本机账号。桌面和 Web 共用同一份数据，无需邀请码或验证邮件。' : authView === 'login' ? '登录后，继续你的个人研究。' : authView === 'register' ? '首发采用邀请注册。每个邀请码仅可使用一次。' : authView === 'verify-email' ? '确认后完成邮箱验证，开启个人研究功能。' : '通过注册邮箱完成密码重置。' }}</p>
      <div v-if="error" class="public-message error" role="alert">{{ error }}</div><div v-if="notice" class="public-message" role="status">{{ notice }}</div>
      <label v-if="['login', 'register', 'request-reset'].includes(authView)">用户名（邮箱）<input v-model="form.email" name="username" type="email" required maxlength="254" autocomplete="username" placeholder="you@example.com" /></label>
      <label v-if="['login', 'register', 'reset-password'].includes(authView)">{{ authView === 'reset-password' ? '新密码' : '密码' }}<input v-model="form.password" name="password" type="password" required minlength="12" maxlength="200" :autocomplete="authView === 'login' ? 'current-password' : 'new-password'" placeholder="至少12个字符" /></label>
      <label v-if="authView === 'register' && !desktopMode">邀请码<input v-model="form.invitation" required maxlength="200" autocomplete="off" placeholder="填写管理员提供的邀请码" /></label>
      <div v-if="needsSetup || authView === 'login'" class="auth-remember"><label><input v-model="rememberLogin" type="checkbox" aria-describedby="remember-login-help" />记住登录状态（30天）</label><p id="remember-login-help">取消勾选后，仅在本次浏览器或客户端会话中保持登录，最长8小时。</p></div>
      <div v-if="(needsSetup || authView === 'login') && credentialSupport" class="auth-remember"><label><input v-model="rememberCredentials" type="checkbox" aria-describedby="remember-credentials-help" />记住用户名和密码</label><p id="remember-credentials-help">{{ credentialSupport==='native' ? '由 Windows 凭据管理器保存，下次登录自动填写。' : '由浏览器密码管理器保存和管理，下次登录自动填写。' }}</p><button v-if="savedCredentials && credentialSupport==='native'" type="button" class="text-button auth-forget" :disabled="busy" @click="action(clearSavedLogin, '已清除本机记住的用户名和密码。')">清除已记住的用户名和密码</button></div>
      <button class="primary auth-submit" :disabled="busy"><LoaderCircle v-if="busy" :size="16" class="spin" />{{ needsSetup ? '创建并进入研究空间' : authView === 'login' ? '登录研究空间' : authView === 'register' ? '注册并发送验证邮件' : authView === 'verify-email' ? '确认验证邮箱' : authView === 'request-reset' ? '发送重置邮件' : '更新密码' }}<ArrowRight v-if="!busy" :size="16" /></button>
      <div v-if="!needsSetup && !desktopMode" class="auth-links"><button v-if="authView === 'login'" type="button" class="text-button" @click="go('register')">使用邀请码注册</button><button v-if="authView === 'login'" type="button" class="text-button" @click="go('request-reset')">忘记密码？</button><button v-else type="button" class="text-button" @click="go('login')"><ArrowLeft :size="13" />返回登录</button></div>
      <p class="auth-footnote">模型配置与默认选择属于本人。账号与模拟交易账户分别管理。</p>
    </form></main>
  </div>
  <div v-else-if="!user.verified" class="pending-verification"><div class="public-auth-card panel"><ShieldCheck :size="34" /><h1>还差一步，验证邮箱</h1><p>已登录 {{ user.email }}。请通过验证邮件开启研究、自选与模型功能。</p><div v-if="error" class="public-message error">{{ error }}</div><div v-if="notice" class="public-message">{{ notice }}</div><div class="button-row"><button class="primary" :disabled="busy" @click="action(async () => { const r = await post('/auth/resend-verification', { email: user.email }); notice = r.message })">重发验证邮件</button><button class="secondary" @click="action(refreshUser)">我已完成验证</button><button class="text-button" @click="logout">退出登录</button></div></div></div>
  <div v-else class="public-workspace">
    <WorkspaceNav :current="route" :open="navigationOpen" :admin="user.role==='admin'" @navigate="go" @close="closeNavigation" />
    <header class="public-userbar" :inert="navigationOpen">
      <div class="workspace-location"><button ref="navigationToggle" class="icon-button workspace-menu-toggle" aria-label="打开导航" :aria-expanded="navigationOpen" aria-controls="primary-workspace-navigation" @click="navigationOpen=!navigationOpen"><Menu :size="21"/></button><span class="workspace-location-root">个人研究</span><ChevronRight :size="14"/><strong>{{ currentTitle }}</strong></div>
      <div class="public-user-actions"><span class="workspace-private-label"><ShieldCheck :size="14"/>私人空间</span><button class="user-chip" :aria-label="'账号管理 · '+user.email" :aria-current="route==='account'?'page':undefined" @click="go('account')"><UserRound :size="16"/><span>{{ user.email }}</span></button><button class="icon-button" aria-label="退出登录" :disabled="busy" @click="logout"><LogOut :size="18"/></button></div>
    </header>
    <div class="public-main" :inert="navigationOpen">
    <SystemSettings ref="systemSettings" v-if="view==='system'" :key="user.id" :user="user" :desktop="desktopMode" @ready="go('dashboard')" />
    <CoreApp ref="core" v-if="view === 'workspace'" :key="user.id" :public-mode="true" :public-user="user" @open-settings="go('preferences')" @open-workbench="openWorkbench" />
    <AssistantDock ref="assistantDock" :key="'assistant-'+user.id" :user="user" :full-page="view==='assistant'" :context="assistantContext" @navigate="go" @clear-context="assistantContext=null"/>
    <KeepAlive><ResearchWorkbench v-if="workbenchViews.has(view)" ref="workbench" :key="user.id" :view="view" :user="user" :context="workbenchContext" :embedded="true" @navigate="go" @clear-context="workbenchContext=null" /></KeepAlive>
    <main v-if="view!=='workspace'&&!workbenchViews.has(view) && view!=='system' && view!=='assistant'" class="personal-content">
      <div class="page-heading"><div><div class="eyebrow">{{ view === 'admin' ? 'SITE ADMINISTRATION' : 'YOUR PERSONAL SPACE' }}</div><h1>{{ { models: '我的模型配置', preferences: '个人研究偏好', account: '账号与数据', admin: '站点管理' }[view] }}</h1><p>{{ view === 'models' ? '按研究场景保存多套配置，使用自己的凭据和数据授权。' : view === 'preferences' ? '设置主题、颜色、研究费用与自动计划。' : view === 'account' ? '管理登录身份和个人研究数据的生命周期。' : '维护账号、共享资料与备份，个人研究仍按本人身份访问。' }}</p></div><button v-if="view === 'models' && !editor && profiles.length" class="primary" @click="editProfile()"><Plus :size="16" />添加配置</button></div>
      <div v-if="error" class="public-message error" role="alert">{{ error }}</div><div v-if="notice" class="public-message" role="status">{{ notice }}</div>

      <AppearanceSettings v-if="view === 'preferences'" />

      <template v-if="view === 'models'">
        <form v-if="editor" class="panel personal-form" @submit.prevent="saveProfile"><div class="panel-heading"><div><h2>{{ editor.id ? '编辑模型配置' : '添加模型配置' }}</h2><p>密钥仅在保存时提交，读取页面只显示掩码。</p></div><button type="button" class="text-button" @click="editor = null">取消</button></div><div class="form-grid"><label>配置名称<input v-model="editor.name" maxlength="80" required placeholder="例如：日常研究" /></label><label>模型服务<select v-model="editor.provider" @change="changeProvider"><option value="minimax">MiniMax 订阅</option><option value="openai_compatible">通用 OpenAI 兼容服务</option><option value="ollama">服务器 Ollama</option></select></label><label v-if="editor.provider === 'minimax'">订阅地区<select v-model="editor.region" @change="changeProvider"><option value="cn">国内站 · 订阅 Key</option><option value="global">国际站 · M Plan Key</option></select></label><label v-if="editor.provider !== 'ollama'">HTTPS 服务地址<input v-model="editor.base_url" type="url" :readonly="editor.provider === 'minimax'" required placeholder="https://provider.example/v1" /></label><label>模型标识<input v-model="editor.model" required maxlength="120" /></label><label v-if="editor.provider !== 'ollama'">{{ editor.id ? '替换 API Key（留空沿用）' : '个人 API Key' }}<input v-model="editor.api_key" type="password" autocomplete="new-password" maxlength="1024" :required="!editor.id && editor.provider !== 'ollama'" placeholder="Key 仅由本人配置使用" /></label><label>上下文窗口（Token）<input v-model.number="editor.context_window" type="number" min="1024" max="1000000" step="1" required /></label><label>助手输出上限（Token）<input v-model.number="editor.output_limit" type="number" min="128" max="4096" step="1" required /></label><label v-if="editor.id && editor.provider !== 'ollama'" class="check-label span-two"><input v-model="editor.remove_key" type="checkbox" />删除现有凭据（未发请求将停止）</label><label class="check-label span-two"><input v-model="editor.active" type="checkbox" />启用此配置</label></div><div class="consent-box"><h3>此配置的数据授权</h3><p>默认发送自选、候选及去标识的研究摘要；不包含账号身份、账户名称、自由备注或凭据。</p><label class="check-label"><input v-model="editor.allow_simulation_summary" type="checkbox" />允许发送虚拟资金与持仓汇总</label><label class="check-label"><input v-model="editor.allow_trade_details" type="checkbox" />允许发送逐笔模拟成交明细</label><small>授权可独立撤销。撤销后，相关尚未发出的请求停止；已有报告保持原样。</small></div><div class="form-actions"><button type="button" class="secondary" @click="editor = null">取消</button><button class="primary" :disabled="busy"><Check :size="16" />保存个人配置</button></div></form>
        <template v-else><section v-if="!profiles.length" class="panel personal-empty"><KeyRound :size="34" /><h2>从一套个人模型配置开始</h2><p>支持 MiniMax 国内站 / 国际站订阅、OpenAI 兼容服务与服务器 Ollama。<br />也可以先使用规则报告，再为原始事实补充 AI。</p><button class="primary" @click="editProfile()"><Plus :size="16" />添加第一套配置</button></section><div class="profile-grid"><article v-for="p in profiles" :key="p.id" class="panel profile-card"><div class="profile-card-top"><span class="profile-icon"><KeyRound :size="20" /></span><span v-if="p.is_default" class="tag green">个人默认</span><span v-else-if="!p.active" class="tag">已停用</span><span v-else-if="p.needs_reconnect" class="tag amber">需要重新连接</span></div><h2>{{ p.name }}</h2><p>{{ providerNames[p.provider] }}{{ p.provider === 'minimax' ? (p.region === 'global' ? ' · 国际站' : ' · 国内站') : '' }}</p><dl><dt>模型</dt><dd>{{ p.model }}</dd><dt>凭据</dt><dd>{{ p.provider === 'ollama' ? '管理员部署的服务器实例' : p.credential_status === 'configured' ? p.key_hint : '尚未配置可用 Key' }}</dd><dt>版本</dt><dd>{{ p.current_version }}</dd></dl><div class="profile-permissions"><span :class="{ allowed: p.allow_simulation_summary }">{{ p.allow_simulation_summary ? '✓' : '—' }} 资金与持仓汇总</span><span :class="{ allowed: p.allow_trade_details }">{{ p.allow_trade_details ? '✓' : '—' }} 模拟成交明细</span></div><div class="button-row"><button class="secondary small" @click="editProfile(p)">编辑</button><button class="secondary small" :disabled="busy || !p.active" @click="testProfile(p)">连接测试</button><button v-if="!p.is_default" class="text-button" :disabled="busy || !p.active" @click="action(async () => { await post('/me/model-profiles/default', { id: p.id }); await loadView() }, '个人默认配置已更新。')">设为默认</button><button class="icon-button" :aria-label="'删除' + p.name" @click="removeProfile(p)"><Trash2 :size="15" /></button></div></article></div><section v-if="personalJobs.length" class="panel personal-list"><div class="panel-heading"><h2>最近连接测试</h2><span class="muted">测试可能消耗模型额度</span></div><div v-for="j in personalJobs.slice(0, 6)" :key="j.id" class="personal-list-row"><span>{{ date(j.created_at) }}</span><span :class="['tag', j.status === 'success' ? 'green' : 'amber']">{{ jobStates[j.status] || j.status }}</span><span>{{ j.result?.message || j.error || '等待服务器执行' }}</span></div></section></template>
        <p class="personal-note">服务器 Ollama 使用站点管理员部署的实例。连接个人电脑、浏览器本机模型不在本版范围内。</p>
      </template>

      <form v-if="view === 'preferences' && preferences" @submit.prevent="savePreferences" class="preferences-layout"><section class="panel personal-form"><div class="panel-heading"><div><h2>预算与费用假设</h2><p>不超过每只证券含费 ¥1,000 的研究门槛</p></div><Settings2 :size="18" /></div><div class="form-grid"><label>单只证券预算（元）<input v-model.number="preferences.budget" type="number" min="1" max="1000" step="0.01" required /></label><label>最低日成交额（元）<input v-model.number="preferences.min_daily_amount" type="number" min="0" max="1000000000000" required /></label><label>风险核验有效期（小时）<input v-model.number="preferences.max_risk_age_hours" type="number" min="1" max="72" required /></label><label v-for="(label, key) in fees" :key="key">{{ label }}<input v-model.number="preferences.fees[key]" type="number" min="0" step="any" required /></label><label class="check-label span-two"><input v-model="preferences.fees.confirmed" type="checkbox" />已核对本人的费用假设</label><label class="span-two">费用说明<textarea v-model="preferences.fees.note" maxlength="500" rows="3" required /></label></div></section><section class="panel personal-form"><div class="panel-heading"><div><h2>个人自动研究计划</h2><p>统一 Asia/Shanghai · 退出登录后仍可按已开启计划执行</p></div><Activity :size="18" /></div><div class="schedule-options"><label class="schedule-option"><input v-model="preferences.morning_enabled" type="checkbox" /><span><strong>标准版早报</strong><small>每天 · 仅补跑当天</small></span><input v-model="preferences.morning_time" type="time" required /></label><label class="schedule-option"><input v-model="preferences.closing_enabled" type="checkbox" /><span><strong>收盘跟踪</strong><small>已确认的交易日</small></span><input v-model="preferences.closing_time" type="time" required /></label><label class="schedule-option"><input v-model="preferences.weekly_enabled" type="checkbox" /><span><strong>深度复盘</strong><small>每周日</small></span><input v-model="preferences.weekly_time" type="time" required /></label></div><div class="consent-box"><h3>自动 AI</h3><label class="check-label"><input v-model="preferences.automatic_ai" type="checkbox" />允许已开启的计划自动调用我的默认模型</label><p>开启后，即使退出登录也可能消耗本人模型额度。关闭时，计划仍可生成规则报告。新账号的所有计划和自动 AI 默认关闭。</p></div><div class="form-actions"><button class="primary" :disabled="busy"><Check :size="16" />保存个人偏好</button></div></section></form>

      <template v-if="view === 'account'"><section class="panel personal-form"><div class="panel-heading"><h2>当前账号</h2><UserRound :size="19" /></div><div class="account-summary"><b>{{ user.email }}</b><span class="tag green">邮箱已验证</span><span class="tag">{{ user.role === 'admin' ? '站点管理员' : '研究用户' }}</span><p>勾选「记住登录状态」可保持登录30天；取消勾选时为临时会话，最长8小时。主动退出、重置密码或停用账号后，已有会话会失效。</p><button v-if="credentialSupport==='native'" class="secondary" :disabled="busy" @click="action(clearSavedLogin, '已清除本机记住的用户名和密码。')">清除本机记住的用户名和密码</button><button class="secondary" @click="action(async () => { const r = await post('/auth/request-reset', { email: user.email }); notice = r.message })">发送密码重置邮件</button></div></section><form class="panel erasure-panel" @submit.prevent="eraseAccount"><h2>注销并删除个人数据</h2><p>立即撤销登录与模型凭据，并删除个人自选、报告、任务、模拟账户与回测。此操作无法撤销。</p><p>历史加密备份最多保留30天。整站恢复会先应用独立注销记录，防止个人数据重新出现。</p><label>当前密码<input v-model="form.password" type="password" autocomplete="current-password" required maxlength="200" /></label><label>请输入：注销并删除我的全部个人数据<input v-model="form.confirmation" required autocomplete="off" /></label><button class="secondary danger-text" :disabled="busy || form.confirmation !== '注销并删除我的全部个人数据'">确认注销账号并删除数据</button></form></template>

      <template v-if="view === 'admin' && user.role === 'admin'"><div class="admin-columns"><section class="panel personal-list"><div class="panel-heading"><div><h2>邀请注册</h2><p>一次使用 · 七天有效 · 邀请码仅创建时显示</p></div><button class="primary small" :disabled="busy" @click="createInvite"><Plus :size="15" />创建邀请</button></div><div v-if="invite" class="invite-result"><strong>新邀请码</strong><code>{{ invite.code }}</code><small>有效至 {{ date(invite.expires_at) }}。请自行将邀请码交给受邀人。</small><button class="text-button" @click="invite = null">已保存，隐藏邀请码</button></div><div v-for="i in invitations.slice(0, 8)" :key="i.id" class="personal-list-row"><span>{{ date(i.created_at) }}</span><span class="tag">{{ i.used_at ? '已使用' : i.revoked_at ? '已撤销' : new Date(i.expires_at) < new Date() ? '已过期' : '有效' }}</span><button v-if="!i.used_at && !i.revoked_at" class="text-button" @click="action(async () => { await api('/admin/invitations/' + i.id, { method: 'DELETE' }); await loadView() })">撤销</button></div></section><section class="panel personal-form"><div class="panel-heading"><div><h2>共享市场资料</h2><p>去重采集所有有效用户自选与公共观察证券</p></div><ShieldCheck :size="19" /></div><div class="admin-shared-actions"><p>证券资料、行情、日历与公开证据由站点统一维护。研究参数和自选分组由每位用户自行管理。</p><div class="button-row"><button class="secondary" :disabled="busy" @click="action(async () => { await post('/admin/jobs', { kind: 'sync' }); await loadView() }, '共享行情刷新已加入站点队列。')">刷新行情与日历</button><button class="secondary" :disabled="busy" @click="action(async () => { await post('/admin/jobs', { kind: 'metals' }); await loadView() })">刷新金银</button><button class="secondary" :disabled="busy" @click="action(async () => { await post('/admin/jobs', { kind: 'context' }); await loadView() })">刷新公开资料</button></div><p>证券的人工核验和历史行情导入，可在研究工作台的证券详情中完成。</p></div></section></div><section class="panel personal-list"><div class="panel-heading"><h2>账号管理</h2><span class="muted">只显示账号状态，不开放他人私人研究或 Key</span></div><div class="table-scroll"><table class="watch-table"><thead><tr><th>邮箱</th><th>角色</th><th>状态</th><th>创建时间</th><th>操作</th></tr></thead><tbody><tr v-for="u in users" :key="u.id"><td>{{ u.email }}</td><td>{{ u.role === 'admin' ? '管理员' : '研究用户' }}</td><td><span :class="['tag', u.status === 'active' ? 'green' : 'amber']">{{ u.status === 'active' ? '正常' : u.status === 'disabled' ? '已停用' : '邮箱待验证' }}</span></td><td>{{ date(u.created_at) }}</td><td><button v-if="u.id !== user.id" class="text-button" :disabled="busy" @click="toggleUser(u)">{{ u.status === 'disabled' ? '恢复账号' : '停用账号' }}</button><span v-else class="muted">当前账号</span></td></tr></tbody></table></div></section><section class="panel personal-list"><div class="panel-heading"><div><h2>加密备份</h2><p>每日生成 · 最多保留30天 · 导出前再次验证管理员密码</p></div><button class="secondary" :disabled="busy" @click="action(async () => { await post('/admin/backups'); await loadView() }, '加密备份已加入站点维护队列。')">创建加密备份</button></div><div v-if="!backups.length" class="personal-empty small-empty"><p>暂无加密备份。请在服务器配置 age 接收公钥后创建。</p></div><div v-for="b in backups.filter(b => b.available)" :key="b.id" class="personal-list-row"><span>{{ date(b.created_at) }}</span><span class="tag green">age 加密</span><small>{{ (b.size / 1024).toFixed(1) }} KB</small><button class="text-button" @click="exportPrompt = b.id; exportPassword = ''">验证密码并导出</button></div><p class="personal-note">整站恢复需停服后使用服务器 CLI，提供解密身份及可信的最新注销记录。页面不替换运行中的数据库。</p></section><section class="panel admin-task-link"><div><h2>任务与采集记录</h2><p>共享采集、个人研究及执行日志统一在任务中心查看。</p></div><button class="secondary" @click="go('jobs')">打开任务中心<ArrowRight :size="16"/></button></section></template>
    </main>
    </div>
    <div v-if="exportPrompt" class="modal-scrim" @click.self="exportPrompt = ''"><form class="modal-card public-export-modal" @submit.prevent="exportBackup"><h2>验证管理员身份</h2><p>本次验证仅授权导出这一份加密备份，五分钟内有效且只能使用一次。</p><label>当前密码<input v-model="exportPassword" type="password" autocomplete="current-password" required maxlength="200" autofocus /></label><p v-if="error" class="inline-error">{{ error }}</p><div class="button-row"><button type="button" class="secondary" @click="exportPrompt = ''; exportPassword = ''">取消</button><button class="primary" :disabled="busy">验证并导出 .age</button></div></form></div>
  </div>
  <ConfirmDialog :message="confirmText" @choose="settleConfirm" />
</template>
