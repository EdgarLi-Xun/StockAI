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
  let finish
  const context = { exports:module.exports, module, document:{cookie:''}, window:{dispatchEvent:event=>events.push(event.type)}, Event, fetch:()=>new Promise(resolve=>{finish=resolve}), __STOCKAI_VERSION__:'test' }
  vm.runInNewContext(code, context)
  return { api:module.exports, events, respond:(value)=>finish(value) }
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
