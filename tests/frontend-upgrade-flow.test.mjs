import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {createRequire} from 'node:module'
import vm from 'node:vm'

const require=createRequire(new URL('../frontend/package.json',import.meta.url))
const ts=require('typescript')
const {computed,ref}=require('vue')
const source=readFileSync(new URL('../frontend/src/SystemSettings.vue',import.meta.url),'utf8').split('</script>')[0]
const parsed=ts.createSourceFile('settings.ts',source.slice(source.indexOf('>')+1),ts.ScriptTarget.Latest,true)
const names=['act','load','loadUpgrade','prepareLocal','downloadLocalPlan','downloadUpgrade','pauseDownload','stopUpgradeFlow']
const functions=parsed.statements.filter(node=>ts.isFunctionDeclaration(node)&&names.includes(node.name?.text)).map(node=>node.getText(parsed)).join('\n')
const code=ts.transpileModule(functions,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText
const headlineDeclarations=parsed.statements.filter(node=>ts.isVariableStatement(node)&&node.declarationList.declarations.some(declaration=>['upgradeStates','tx','upgradeHeadline'].includes(declaration.name.getText(parsed)))).map(node=>node.getText(parsed)).join('\n')
const headlineCode=ts.transpileModule(headlineDeclarations,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText
const plan=changes=>({id:'update-one',component:'desktop',trust:'github-sha256',status:'planned',from_version:'3.2.4',target_version:'3.2.5',confirmation:'bound-to-update-one',release:{package:{size:100}},...changes})

function headline({desktop=true,running='3.2.4',transaction=plan({status:'succeeded'})}={}){
  return vm.runInNewContext(headlineCode+'\nupgradeHeadline.value',{computed,props:{desktop},upgrade:ref({current_version:running,transaction})})
}

function client(options={}){
  const requests=[],intervals=new Map(),ref=value=>({value})
  let current=options.plan||null,timerId=0
  const base={form:'exe',check:{status:'download_available',release:{version:'3.2.5'}}}
  const state={props:{desktop:true,user:{role:'admin'}},sync:ref({}),upgrade:ref({...base,transaction:current}),busy:ref(false),notice:ref(''),error:ref(''),confirmation:ref('old-consent'),upgradeClosed:false,upgradeRead:0,upgradeDownloadTimer:undefined,
    setInterval:fn=>{intervals.set(++timerId,fn);return timerId},clearInterval:id=>intervals.delete(id),setTimeout:(fn,ms)=>setTimeout(fn,ms),Promise,
    api:async path=>path==='/admin/upgrades'?(options.readUpgrade?options.readUpgrade({...base,transaction:current}):{...base,transaction:current}):{},
    post:async(path,body)=>{
      requests.push({path,body})
      if(path==='/admin/upgrades/plan'){current=options.plan||plan();return current}
      if(path.endsWith('/download')){
        current={...current,status:'downloading',downloaded:20,total:100}
        if(options.download)await options.download(state)
        if(options.fail){
          current={...current,status:options.failKeepDownloading?'downloading':options.fail.code==='CANCELLED'?'download_paused':'download_failed'}
          throw options.fail
        }
        current={...current,status:'verified',downloaded:100};return current
      }
      if(path.endsWith('/preflight')){current={...current,status:options.blockers?'preflight_blocked':'waiting_confirmation',blockers:options.blockers||[]};return current}
      if(path.endsWith('/cancel')){current={...current,status:current.status==='downloading'?'download_paused':'cancelled'};return {status:current.status==='download_paused'?'cancel_requested':'cancelled'}}
      throw new Error('Unexpected action: '+path)
    }}
  Object.defineProperty(state,'tx',{get:()=>({value:state.upgrade.value.transaction})})
  vm.runInNewContext(code+'\nglobalThis.prepare=prepareLocal;globalThis.download=()=>downloadUpgrade();globalThis.pause=pauseDownload;globalThis.dispose=()=>{if(typeof stopUpgradeFlow===\'function\')stopUpgradeFlow()};',state)
  return {state,requests,intervals,tick:async()=>{for(const fn of intervals.values())await fn();await new Promise(setImmediate)}}
}

test('one click downloads, verifies and preflights the selected local update without executing it',async()=>{
  const c=client();await c.state.prepare()
  assert.deepEqual(c.requests.map(r=>r.path),['/admin/upgrades/plan','/admin/upgrades/update-one/download','/admin/upgrades/update-one/preflight'])
  assert.equal(c.state.upgrade.value.transaction.status,'waiting_confirmation')
  assert.equal(c.state.confirmation.value,'')
  assert.match(c.state.notice.value,/确认/)
  assert.equal(c.state.busy.value,false)
  assert.equal(c.intervals.size,0)
})

test('download progress is refreshed while duplicate clicks share the current action',async()=>{
  let finish
  const c=client({download:()=>new Promise(resolve=>{finish=resolve})})
  const first=c.state.prepare();await new Promise(setImmediate)
  await c.state.prepare();await c.tick()
  assert.equal(c.requests.filter(r=>r.path.endsWith('/download')).length,1)
  assert.equal(c.state.upgrade.value.transaction.downloaded,20)
  assert.ok(c.intervals.size>0)
  finish();await first
  assert.equal(c.intervals.size,0)
})

test('a dropped download response still waits for the in-flight package',async()=>{
  let finish,reads=0
  const c=client({
    fail:Object.assign(new Error('请求失败，请稍后重试'),{status:500}),
    failKeepDownloading:true,
    download:()=>new Promise(resolve=>{finish=resolve}),
    readUpgrade:snapshot=>{
      if(snapshot.transaction?.status!=='downloading')return snapshot
      reads++
      if(reads>2)return {...snapshot,transaction:{...snapshot.transaction,status:'verified',downloaded:100}}
      return snapshot
    }
  })
  const pending=c.state.prepare();await new Promise(setImmediate);finish()
  await pending
  assert.equal(c.state.error.value,'')
  assert.match(c.state.notice.value,/确认/)
  assert.equal(c.state.upgrade.value.transaction.status,'waiting_confirmation')
  assert.equal(c.requests.some(r=>r.path.endsWith('/preflight')),true)
})

test('a failed checksum leaves a retryable download and never preflights or installs',async()=>{
  const c=client({fail:Object.assign(new Error('校验失败'),{code:'PACKAGE'})});await c.state.prepare()
  assert.equal(c.state.upgrade.value.transaction.status,'download_failed')
  assert.equal(c.state.error.value,'校验失败')
  assert.equal(c.state.notice.value,'')
  assert.equal(c.requests.some(r=>r.path.endsWith('/preflight')||r.path.endsWith('/execute')),false)
  assert.equal(c.intervals.size,0)
})

test('pausing a download does not continue to preflight',async()=>{
  const c=client({fail:Object.assign(new Error('下载已暂停'),{code:'CANCELLED'})});await c.state.prepare()
  assert.equal(c.state.upgrade.value.transaction.status,'download_paused')
  assert.equal(c.state.error.value,'')
  assert.match(c.state.notice.value,/暂停/)
  assert.equal(c.requests.some(r=>r.path.endsWith('/preflight')),false)
  assert.equal(c.intervals.size,0)
})

test('pause during an in-flight download stops waiting for the installer plan',async()=>{
  let finish
  const c=client({download:()=>new Promise(resolve=>{finish=resolve}),plan:plan({status:'downloading'})})
  c.state.upgrade.value.transaction={...c.state.upgrade.value.transaction,status:'downloading'}
  const pending=c.state.pause();await new Promise(setImmediate)
  await pending
  assert.equal(c.requests.some(r=>r.path.endsWith('/cancel')),true)
  assert.equal(c.state.upgrade.value.transaction.status,'download_paused')
  assert.match(c.state.notice.value,/暂停/)
  assert.equal(c.state.error.value,'')
  finish?.()
})

test('a late progress response cannot hide the completed preflight',async()=>{
  let finish,oldProgress
  const c=client({download:()=>new Promise(resolve=>{finish=resolve}),readUpgrade:snapshot=>snapshot.transaction?.status==='downloading'?new Promise(resolve=>{oldProgress=()=>resolve(snapshot)}):snapshot})
  const pending=c.state.prepare();await new Promise(setImmediate);await c.tick()
  finish();await pending
  assert.equal(c.state.upgrade.value.transaction.status,'waiting_confirmation')
  oldProgress();await new Promise(setImmediate)
  assert.equal(c.state.upgrade.value.transaction.status,'waiting_confirmation')
})

test('slow status requests are not stacked by progress polling',async()=>{
  let finish,finishProgress,reads=0
  const c=client({download:()=>new Promise(resolve=>{finish=resolve}),readUpgrade:snapshot=>{
    if(snapshot.transaction?.status!=='downloading')return snapshot
    reads++;return new Promise(resolve=>{finishProgress=()=>resolve(snapshot)})
  }})
  const pending=c.state.prepare();await new Promise(setImmediate)
  await c.tick();await c.tick();await c.tick()
  assert.equal(reads,1)
  finish();await pending;finishProgress();await new Promise(setImmediate)
  assert.equal(c.state.upgrade.value.transaction.status,'waiting_confirmation')
})

test('resuming a paused local download also performs preflight',async()=>{
  const c=client({plan:plan({status:'download_paused'})});await c.state.download()
  assert.deepEqual(c.requests.map(r=>r.path),['/admin/upgrades/update-one/download','/admin/upgrades/update-one/preflight'])
  assert.equal(c.state.upgrade.value.transaction.status,'waiting_confirmation')
})

test('an already verified package is preflighted without downloading it again',async()=>{
  const c=client({plan:plan({status:'verified'})});await c.state.prepare()
  assert.equal(c.requests.some(r=>r.path.endsWith('/download')),false)
  assert.equal(c.state.upgrade.value.transaction.status,'waiting_confirmation')
})

test('preflight blockers are retained for the user and cannot trigger installation',async()=>{
  const c=client({blockers:['有未保存编辑']});await c.state.prepare()
  assert.equal(c.state.upgrade.value.transaction.status,'preflight_blocked')
  assert.equal(c.state.upgrade.value.transaction.blockers[0],'有未保存编辑')
  assert.equal(c.requests.some(r=>r.path.endsWith('/execute')),false)
})

test('leaving the page stops progress polling and prevents the next automatic action',async()=>{
  let finish
  const c=client({download:()=>new Promise(resolve=>{finish=resolve})})
  const pending=c.state.prepare();await new Promise(setImmediate)
  c.state.dispose();assert.equal(c.intervals.size,0)
  finish();await pending
  assert.equal(c.requests.some(r=>r.path.endsWith('/preflight')||r.path.endsWith('/execute')),false)
})

test('a different active target is shown without silently downloading it',async()=>{
  const c=client({plan:plan({target_version:'3.2.3'})});await c.state.prepare()
  assert.equal(c.requests.length,1)
  assert.match(c.state.error.value,/计划/)
})

test('the desktop update action cannot start a local install plan on a Web server',async()=>{
  const c=client();c.state.props.desktop=false;await c.state.prepare()
  assert.equal(c.requests.length,0)
  assert.match(c.state.error.value,/本机桌面/)
})

test('the old desktop process shows the successful update handoff',()=>{
  assert.equal(headline(),'正在退出并打开新版本')
})

test('a successful Web upgrade does not claim that the desktop is restarting',()=>{
  assert.equal(headline({desktop:false}),'升级成功')
})

test('a newer desktop does not claim to restart for a historical successful upgrade',()=>{
  assert.equal(headline({running:'3.2.18',transaction:plan({status:'succeeded',from_version:'3.2.13',target_version:'3.2.14',actual_version:'3.2.14'})}),'升级成功')
})

test('the installed desktop version shows a completed upgrade',()=>{
  assert.equal(headline({running:'3.2.5'}),'升级成功')
  assert.equal(headline({transaction:plan({status:'succeeded',actual_version:'3.2.4'})}),'升级成功')
})

test('upgrade headlines retain the current transaction state',()=>{
  assert.equal(headline({transaction:null}),'')
  assert.equal(headline({transaction:plan({status:'exit_requested'})}),'正在退出并交接升级')
})
