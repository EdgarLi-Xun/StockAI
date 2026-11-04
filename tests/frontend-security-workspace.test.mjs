import {test} from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import {createRequire} from 'node:module'
import vm from 'node:vm'

const require=createRequire(new URL('../frontend/package.json',import.meta.url))
const ts=require('typescript')
const source=readFileSync(new URL('../frontend/src/App.vue',import.meta.url),'utf8').split('</script>')[0]
const parsed=ts.createSourceFile('app.ts',source.slice(source.indexOf('>')+1),ts.ScriptTarget.Latest,true)
const names=['openDetail','closeDetail','changeTimeframe','submitModal','estimate','readFile']
const functions=parsed.statements.filter(node=>ts.isFunctionDeclaration(node)&&names.includes(node.name?.text)).map(node=>node.getText(parsed)).join('\n')
const code=ts.transpileModule(functions,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText
function client(){
  const requests=[],budgetRequests=[],posts=[],ref=value=>({value})
  const state={props:{publicMode:true},detailId:ref(''),detailLoading:ref(false),detail:ref(null),detailRead:0,budget:ref(null),budgetForm:ref({}),timeframe:ref('1d'),page:ref('dashboard'),location:{hash:'#watchlist'},history:{replaceState(){}},emit(){},error:'',
    modal:ref(''),form:ref({}),busy:ref(false),modalError:ref(''),notify(){},refreshBase:async()=>{},loadPage:async()=>{},
    post:(path,body)=>{posts.push({path,body});return path==='/budget'?new Promise((resolve,reject)=>budgetRequests.push({path,body,resolve,reject})):Promise.resolve({})},
    api:path=>new Promise((resolve,reject)=>requests.push({path,resolve,reject}))}
  state.action=async fn=>{try{await fn()}catch(error){state.error=error.message}}
  vm.runInNewContext(code+'\nglobalThis.open=openDetail;globalThis.close=closeDetail;globalThis.period=changeTimeframe;globalThis.save=submitModal;globalThis.estimate=estimate;globalThis.importFile=readFile;',state)
  return {state,requests,budgetRequests,posts}
}
const detail=(id,price)=>({instrument:{id,name:id,rules:{lot_size:100}},bars:[{close:price}]})

test('switching securities discards a late response from the earlier selection',async()=>{
  const c=client(),first=c.state.open('SH:510300'),second=c.state.open('SZ:159915')
  c.requests[1].resolve(detail('SZ:159915',2));await second
  c.requests[0].resolve(detail('SH:510300',4));await first
  assert.equal(c.state.detail.value.instrument.id,'SZ:159915')
  assert.equal(c.state.budgetForm.value.price,2)
  assert.equal(c.state.detailLoading.value,false)
})
test('returning to the list prevents an in-flight response from reopening the security',async()=>{
  const c=client(),pending=c.state.open('SH:510300')
  c.state.close();c.requests[0].resolve(detail('SH:510300',4));await pending
  assert.equal(c.state.detailId.value,'')
  assert.equal(c.state.detail.value,null)
  assert.equal(c.state.detailLoading.value,false)
})
test('duplicate selection shares loading and a failed read can be retried',async()=>{
  const c=client(),first=c.state.open('SH:510300')
  await c.state.open('SH:510300')
  assert.equal(c.requests.length,1)
  c.requests[0].reject(new Error('temporarily unavailable'));await first
  assert.equal(c.state.detailLoading.value,false)
  const retry=c.state.open('SH:510300');c.requests[1].resolve(detail('SH:510300',4));await retry
  assert.equal(c.state.detail.value.instrument.id,'SH:510300')
})
test('a late period response cannot replace another security',async()=>{
  const c=client(),first=c.state.open('SH:510300');c.requests[0].resolve(detail('SH:510300',4));await first
  const period=c.state.period('1w'),next=c.state.open('SZ:159915')
  c.requests[2].resolve(detail('SZ:159915',2));await next
  c.requests[1].resolve(detail('SH:510300',4.2));await period
  assert.equal(c.state.detail.value.instrument.id,'SZ:159915')
  assert.equal(c.state.timeframe.value,'1d')
})
test('saving official verification reloads the current security evidence',async()=>{
  const c=client(),first=c.state.open('SH:510300');c.requests[0].resolve(detail('SH:510300',4));await first
  c.state.modal.value='verify'
  const save=c.state.save()
  await new Promise(setImmediate)
  assert.equal(c.requests.length,2)
  const verified={...detail('SH:510300',4),instrument:{id:'SH:510300',name:'已核验证券',rules:{lot_size:100,verified:true}}}
  c.requests[1].resolve(verified);await save
  assert.equal(c.state.detail.value.instrument.rules.verified,true)
  assert.equal(c.state.modal.value,'')
})
test('a late budget response cannot appear under another security',async()=>{
  const c=client(),first=c.state.open('SH:510300');c.requests[0].resolve(detail('SH:510300',4));await first
  const estimate=c.state.estimate(),next=c.state.open('SZ:159915')
  assert.equal(c.budgetRequests[0].body.instrument_id,'SH:510300')
  c.requests[1].resolve(detail('SZ:159915',2));await next
  c.budgetRequests[0].resolve({total:405});await estimate
  assert.equal(c.state.detail.value.instrument.id,'SZ:159915')
  assert.equal(c.state.budget.value,null)
  const current=c.state.estimate();c.budgetRequests[1].resolve({total:205});await current
  assert.equal(c.state.budget.value.total,205)
})
test('switching securities while reading a file cancels the historical-bar import',async()=>{
  const c=client(),first=c.state.open('SH:510300');c.requests[0].resolve(detail('SH:510300',4));await first
  let finishRead
  const target={value:'bars.json',files:[{name:'bars.json',text:()=>new Promise(resolve=>{finishRead=resolve})}]}
  const pending=c.state.importFile({target},'bars'),next=c.state.open('SZ:159915')
  c.requests[1].resolve(detail('SZ:159915',2));await next
  finishRead('[{"close":4}]');await pending
  assert.equal(c.posts.length,0)
  assert.equal(target.value,'')
  assert.equal(c.state.detail.value.instrument.id,'SZ:159915')
})
test('historical-bar import uses the selected security and refreshes its chart',async()=>{
  const c=client(),first=c.state.open('SH:510300');c.requests[0].resolve(detail('SH:510300',4));await first
  c.state.timeframe.value='1w'
  const target={value:'bars.json',files:[{name:'bars.json',text:async()=>'[{"close":4.5}]'}]}
  const pending=c.state.importFile({target},'bars');await new Promise(setImmediate)
  assert.equal(c.posts[0].path,'/instruments/SH%3A510300/bars/import')
  assert.equal(c.posts[0].body.timeframe,'1d')
  assert.equal(c.posts[0].body.bars[0].close,4.5)
  assert.equal(c.requests[1].path,'/instruments/SH%3A510300?timeframe=1w')
  c.requests[1].resolve(detail('SH:510300',4.5));await pending
  assert.equal(c.state.detail.value.bars[0].close,4.5)
  assert.equal(target.value,'')
})

const navigationModule={exports:{}}
const navigation=readFileSync(new URL('../frontend/src/workspaceNavigation.ts',import.meta.url),'utf8')
vm.runInNewContext(ts.transpileModule(navigation,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.CommonJS}}).outputText,{module:navigationModule,exports:navigationModule.exports})
test('existing links remain reachable after navigation grouping',()=>{
  const {workspaceGroup,workspaceTabs}=navigationModule.exports
  for(const [route,parent] of [['market','watchlist'],['metals','watchlist'],['journals','reports'],['review','reports'],['portfolio','simulation'],['backtest','experiments'],['models','preferences'],['usage','preferences'],['readiness','preferences'],['system','preferences']]){
    assert.equal(workspaceGroup(route)?.id,parent,route)
    assert.ok(workspaceTabs(route).some(tab=>tab.id===route),route)
  }
})
