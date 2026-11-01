export const PUBLIC = import.meta.env.MODE === 'public'
let token = PUBLIC ? '' : sessionStorage.getItem('stockai-token') || ''
export function setToken(value: string) { token = value; sessionStorage.setItem('stockai-token', value) }

function url(path: string, method = 'GET') {
  if (!PUBLIC) return '/api' + path
  if (/^\/(auth|admin|me)\//.test(path)) return '/api' + path
  if (method !== 'GET' && /^\/instruments\/.*\/(verify|sync|announcements|bars\/import)$/.test(path)) return '/api/admin' + path
  return '/api/me' + path
}
function headers(options: RequestInit = {}) {
  const csrf = PUBLIC ? document.cookie.split('; ').find(s => /^(?:__Host-stockai_csrf|stockai_dev_csrf)=/.test(s))?.split('=')[1] : ''
  return { 'Content-Type': 'application/json', ...(PUBLIC ? (csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {}) : token ? { Authorization: 'Bearer ' + token } : {}), ...options.headers }
}

export async function api<T = any>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(url(path, options.method), { ...options, credentials: 'same-origin', headers: headers(options) })
  if (!response.ok) {
    const value = await response.json().catch(() => ({}))
    if (PUBLIC && response.status === 401 && value.code === 'LOGIN_REQUIRED') window.dispatchEvent(new Event('stockai-auth-required'))
    const detail = typeof value.detail === 'string' ? value.detail : Array.isArray(value.detail) ? value.detail.map((r: any) => r.msg).join('；') : '请求失败，请稍后重试'
    throw Object.assign(new Error(detail), { code: value.code, status: response.status })
  }
  return response.json()
}
export const post = <T = any>(path: string, value: unknown = {}) => api<T>(path, { method: 'POST', body: JSON.stringify(value) })

export async function download(path: string, name: string, options: RequestInit = {}) {
  const response = await fetch(url(path, options.method), { ...options, credentials: 'same-origin', headers: headers(options) })
  if (!response.ok) { const data = await response.json(); throw new Error(data.detail || '导出失败') }
  const blobUrl = URL.createObjectURL(await response.blob())
  const a = document.createElement('a'); a.href = blobUrl; a.download = name; a.click()
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000)
}
