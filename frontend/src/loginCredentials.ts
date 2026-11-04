// 仅通过桌面系统凭据库或浏览器密码管理器保存密码。
export type SavedLogin = { email: string; password: string }
type Reply = { ok: boolean; login?: SavedLogin | null; error?: string }
type NativeLogin = {
  read: (done: (value: string) => void) => void
  save: (email: string, password: string, done: (value: string) => void) => void
  clear: (done: (value: string) => void) => void
}
type LoginWindow = Window & {
  stockaiLogin?: NativeLogin
  qt?: { webChannelTransport?: unknown }
  PasswordCredential?: new (data: { id: string; password: string }) => Credential
}
const browser = window as LoginWindow
const usernameKey = 'stockai-login-username'

async function nativeLogin(): Promise<NativeLogin | null> {
  if (browser.stockaiLogin) return browser.stockaiLogin
  if (!browser.qt?.webChannelTransport) return null
  await new Promise<void>(resolve => {
    const done = () => { clearTimeout(timer); window.removeEventListener('stockai-login-ready', done); resolve() }
    const timer = setTimeout(done, 1500)
    window.addEventListener('stockai-login-ready', done, { once: true })
  })
  return browser.stockaiLogin || null
}

async function invoke(call: (done: (value: string) => void) => void): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('本机账号密码保存暂不可用，请重试')), 2500)
    try {
      call(raw => {
        clearTimeout(timer)
        try { const reply = JSON.parse(raw) as Reply; if (!reply.ok) throw new Error(reply.error || '账号密码保存失败'); resolve(reply) }
        catch (error) { reject(error) }
      })
    } catch (error) { clearTimeout(timer); reject(error) }
  })
}

export async function loginCredentialSupport(): Promise<'native' | 'browser' | null> {
  if (await nativeLogin()) return 'native'
  return browser.PasswordCredential && typeof navigator.credentials?.store === 'function' && window.isSecureContext ? 'browser' : null
}

export async function loadLoginCredentials(): Promise<SavedLogin | null> {
  const native = await nativeLogin()
  if (native) return (await invoke(done => native.read(done))).login || null
  const email = localStorage.getItem(usernameKey)
  if (!email) return null
  if (await loginCredentialSupport() === 'browser') {
    try {
      const stored = await navigator.credentials.get({ password: true, mediation: 'silent' } as CredentialRequestOptions) as (Credential & { password?: string }) | null
      if (stored?.type === 'password' && stored.id.toLowerCase() === email.toLowerCase()) return { email, password: stored.password || '' }
    } catch { /* 浏览器拒绝静默读取时仍可自动填写已记住的用户名。 */ }
  }
  return { email, password: '' }
}

export async function saveLoginCredentials(email: string, password: string): Promise<void> {
  const native = await nativeLogin()
  if (native) { await invoke(done => native.save(email, password, done)); return }
  if (!browser.PasswordCredential || typeof navigator.credentials?.store !== 'function' || !window.isSecureContext) throw new Error('请使用浏览器密码管理器保存用户名和密码')
  await navigator.credentials.store(new browser.PasswordCredential({ id: email, password }))
  localStorage.setItem(usernameKey, email)
}

export async function forgetLoginCredentials(): Promise<void> {
  const native = await nativeLogin()
  if (native) { await invoke(done => native.clear(done)); return }
  localStorage.removeItem(usernameKey)
  await navigator.credentials?.preventSilentAccess?.()
}
