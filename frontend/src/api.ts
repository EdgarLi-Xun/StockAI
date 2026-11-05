// 统一封装本地令牌鉴权、公网同源 Cookie 与 CSRF，以及请求和文件下载。
// 依据构建模式选择本地共享 API 或公网个人 API 的路由规则。
export const PUBLIC = import.meta.env.MODE === 'public'
let token = PUBLIC ? '' : sessionStorage.getItem('stockai-token') || ''
let csrfCookieName = ''
let clientVersion = ''
let privateEpoch = 0
export function setClientVersion(value: string) { clientVersion = value.length > 0 && value.length <= 40 && !/[\r\n]/.test(value) ? value : '' }
export function clearPrivateRequests() { privateEpoch++ }
function checkSession(epoch:number) { if(PUBLIC&&epoch!==privateEpoch)throw Object.assign(new Error('原账号请求已结束'),{code:'STALE_SESSION'}) }
export function setCsrfCookieName(value: string) { csrfCookieName = /^[A-Za-z0-9_-]+$/.test(value) ? value : '' }
// 保存本地客户端的会话令牌，供后续请求携带。
export function setToken(value: string) { token = value; sessionStorage.setItem('stockai-token', value) }

// 将业务路径映射到本地、本人或管理员接口，保留显式认证与管理前缀。
function url(path: string, method = 'GET') {
  if (!PUBLIC) return '/api' + path
  if (/^\/(auth|admin|me|desktop)\//.test(path)) return '/api' + path
  if (method !== 'GET' && /^\/instruments\/.*\/(verify|sync|announcements|bars\/import)$/.test(path)) return '/api/admin' + path
  return '/api/me' + path
}
// 合并 JSON 请求头及鉴权信息，公网写请求使用会话对应的 CSRF Cookie。
function headers(options: RequestInit = {}) {
  const csrf = PUBLIC ? document.cookie.split('; ').find(s => csrfCookieName ? s.startsWith(csrfCookieName + '=') : /^(?:__Host-stockai_csrf|stockai_dev_csrf)=/.test(s))?.split('=')[1] : ''
  return { 'Content-Type': 'application/json', ...(PUBLIC ? { ...(clientVersion ? { 'X-StockAI-Client-Version': clientVersion } : {}), ...(csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {}) } : token ? { Authorization: 'Bearer ' + token } : {}), ...options.headers }
}

// 统一发起同源请求，解析业务错误并在公网会话失效时通知入口组件。
export async function api<T = any>(path: string, options: RequestInit & { allowAnonymous?: boolean } = {}): Promise<T> {
  const epoch=privateEpoch
  const { allowAnonymous = false, ...request } = options
  const response = await fetch(url(path, request.method), { ...request, credentials: 'same-origin', headers: headers(request) })
  checkSession(epoch)
  if (!response.ok) {
    const value = await response.json().catch(() => ({}))
    checkSession(epoch)
    if (PUBLIC && !allowAnonymous && response.status === 401 && value.code === 'LOGIN_REQUIRED') window.dispatchEvent(new Event('stockai-auth-required'))
    const detail = typeof value.detail === 'string' ? value.detail : Array.isArray(value.detail) ? value.detail.map((r: any) => r.msg).join('；') : '请求失败，请稍后重试'
    throw Object.assign(new Error(detail), { code: value.code, status: response.status })
  }
  const value=await response.json();checkSession(epoch);return value
}
// 将常用 JSON 写请求封装为 POST，并复用统一错误处理。
export const post = <T = any>(path: string, value: unknown = {}) => api<T>(path, { method: 'POST', body: JSON.stringify(value) })

// 读取下载响应为临时 Blob 链接，触发保存后及时释放浏览器对象 URL。
export async function download(path: string, name: string, options: RequestInit = {}) {
  const epoch=privateEpoch
  const response = await fetch(url(path, options.method), { ...options, credentials: 'same-origin', headers: headers(options) })
  checkSession(epoch)
  if (!response.ok) { const data = await response.json(); throw new Error(data.detail || '导出失败') }
  const blob=await response.blob();checkSession(epoch)
  const blobUrl = URL.createObjectURL(blob)
  const a = document.createElement('a'); a.href = blobUrl; a.download = name; a.click()
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000)
}
