import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import vm from 'node:vm'

const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const ts = require('typescript')
const source = readFileSync(new URL('../frontend/src/api.ts', import.meta.url), 'utf8').replaceAll('import.meta.env.MODE', '"public"')
const code = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

function client() {
  const events = [], module = { exports:{} }
  let finish, lastRequest
  const context = { exports:module.exports, module, document:{cookie:''}, window:{dispatchEvent:event=>events.push(event.type)}, Event, fetch:(url, options)=>{ lastRequest={url, options}; return new Promise(resolve=>{finish=resolve}) } }
  vm.runInNewContext(code, context)
  return { api:module.exports, events, lastRequest:()=>lastRequest, respond:(value)=>finish(value) }
}

test('a response from the previous identity cannot populate the new identity', async () => {
  const c=client(), pending=c.api.api('/me/model-profiles')
  c.api.clearPrivateRequests()
  c.respond({ok:true,json:async()=>[{name:'previous identity private profile'}]})
  await assert.rejects(pending, error=>error.code==='STALE_SESSION')
  assert.deepEqual(c.events,[])
})

test('a late unauthorized response cannot sign out the next identity', async () => {
  const c=client(), pending=c.api.api('/me/assistant/sessions')
  c.api.clearPrivateRequests()
  c.respond({ok:false,status:401,json:async()=>({code:'LOGIN_REQUIRED',detail:'登录失效'})})
  await assert.rejects(pending, error=>error.code==='STALE_SESSION')
  assert.deepEqual(c.events,[])
})

test('the current unauthorized identity still receives session-expiry handling', async () => {
  const c=client(), pending=c.api.api('/me/assistant/sessions')
  c.respond({ok:false,status:401,json:async()=>({code:'LOGIN_REQUIRED',detail:'登录失效'})})
  await assert.rejects(pending, error=>error.code==='LOGIN_REQUIRED')
  assert.deepEqual(c.events,['stockai-auth-required'])
})

test('public requests send the health version after it is recorded', async () => {
  const c=client()
  c.api.setClientVersion('3.2.8')
  const pending=c.api.api('/auth/csrf')
  assert.equal(c.lastRequest().options.headers['X-StockAI-Client-Version'], '3.2.8')
  c.respond({ok:true,json:async()=>({})})
  await pending
})

test('invalid or empty health versions are not sent as request headers', async () => {
  const c=client()
  c.api.setClientVersion('3.2.8\nmalformed')
  const rejected=c.api.api('/auth/csrf')
  assert.equal(c.lastRequest().options.headers['X-StockAI-Client-Version'], undefined)
  c.respond({ok:true,json:async()=>({})})
  await rejected
  c.api.setClientVersion('3.2.8')
  c.api.setClientVersion('')
  const pending=c.api.api('/auth/csrf')
  assert.equal(c.lastRequest().options.headers['X-StockAI-Client-Version'], undefined)
  c.respond({ok:true,json:async()=>({})})
  await pending
})

test('an anonymous startup check does not report an expired login', async () => {
  const c=client(), pending=c.api.api('/auth/me', { allowAnonymous:true })
  c.respond({ok:false,status:401,json:async()=>({code:'LOGIN_REQUIRED',detail:'请先登录'})})
  await assert.rejects(pending, error=>error.status===401)
  assert.deepEqual(c.events,[])
})
