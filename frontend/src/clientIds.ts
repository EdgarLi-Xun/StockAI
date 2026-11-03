// 局域网 HTTP 不提供 randomUUID；仍使用 Web Crypto 的安全随机数生成请求标识。
export function installClientIds(target: Crypto = globalThis.crypto) {
  if (typeof target.randomUUID === 'function') return
  Object.defineProperty(target, 'randomUUID', {
    configurable: true,
    writable: true,
    value: (): ReturnType<Crypto['randomUUID']> => {
      const bytes = target.getRandomValues(new Uint8Array(16))
      bytes[6] = (bytes[6] & 0x0f) | 0x40
      bytes[8] = (bytes[8] & 0x3f) | 0x80
      const hex = Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
      return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
    },
  })
}

installClientIds()
