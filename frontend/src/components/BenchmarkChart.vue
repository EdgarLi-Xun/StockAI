<script setup lang="ts">
// 比较策略、含费买入持有与可用价格指数，只绘制共同有效日期的归一化曲线。
import { computed,ref } from 'vue'
// 接收归档实验结果及其买入持有与指数基准。
const props=defineProps<{result:any}>()
// 选择全区间、样本内或样本外，各区间独立设置归一化起点。
const part=ref('full')
// 按共同有效交易日对齐曲线并映射为 SVG 路径，没有共同区间时返回空值。
const chart=computed(()=>{
  const result=props.result,bench=result.benchmarks, initial=result.summary.initial_cash||result.full.metrics.initial_cash
  const strategy=result[part.value]?.equity||[],hold=bench?.buy_hold[part.value]?.equity||[]
  const idx=part.value==='full'?bench?.index:bench?.index?.subperiods?.[part.value]
  const observed=new Map<string,any>((idx?.points||[]).map((p:any)=>[p.day,p]))
  const held=new Map<string,any>(hold.map((p:any)=>[p.ts.slice(0,10),p]))
// 只保留策略、买入持有及已选择指数都有数据的日期，不插值补造缺失价格。
  const selected=strategy.filter((p:any)=>held.has(p.ts.slice(0,10))&&(!observed.size||observed.has(p.ts.slice(0,10))))
  if(!selected.length)return null
  const first=selected[0],firstHold=held.get(first.ts.slice(0,10)).equity
// 策略和买入持有分别以共同起点权益归一化为 100，保留费用与剩余现金影响。
  const curves=[{name:'策略',color:'#277752',values:selected.map((p:any)=>p.equity/first.equity*100)},{name:'买入持有',color:'#bb9652',values:selected.map((p:any)=>held.get(p.ts.slice(0,10)).equity/firstHold*100)}]
  if(observed.size)curves.push({name:'价格指数（观察）',color:'#8c91bd',values:selected.map((p:any)=>observed.get(p.ts.slice(0,10)).index)})
// 根据全部可见曲线计算纵轴范围，并留出最小边距以免平坦曲线除以零。
  const all=curves.flatMap(c=>c.values);const low=Math.min(100,...all),high=Math.max(100,...all),padding=Math.max((high-low)*.15,.5),min=low-padding,max=high+padding
// 将归一化数值映射到固定 SVG 视口，横轴按共同观测点等距排列。
  const y=(v:number)=>195-(v-min)/(max-min)*165,x=(i:number)=>52+i/Math.max(selected.length-1,1)*680
  return {curves:curves.map(c=>({...c,path:c.values.map((v:number,i:number)=>(i?'L':'M')+x(i).toFixed(2)+','+y(v).toFixed(2)).join(' ')})),first:first.ts.slice(0,10),last:selected.at(-1).ts.slice(0,10),y100:y(100),top:max.toFixed(1),bottom:min.toFixed(1),count:selected.length,initial}
})
</script>
<!-- 区间切换后同步显示图例、共同区间和观察口径，缺少数据时明确提示。 -->
<template><div class="benchmark-chart"><div class="benchmark-toolbar"><b>共同起点归一化 · 100</b><label>独立区间<select v-model="part"><option value="full">全区间</option><option value="in_sample">样本内</option><option value="out_of_sample">样本外</option></select></label></div><template v-if="chart"><svg viewBox="0 0 760 240" role="img" aria-label="策略、买入持有及可用价格指数的共同区间归一化曲线"><line x1="52" x2="732" :y1="chart.y100" :y2="chart.y100" stroke="#d8e2db" stroke-dasharray="4 4"/><text x="8" y="35">{{chart.top}}</text><text x="8" y="197">{{chart.bottom}}</text><text x="52" y="226">{{chart.first}}</text><text x="732" y="226" text-anchor="end">{{chart.last}}</text><path v-for="c in chart.curves" :key="c.name" :d="c.path" fill="none" :stroke="c.color" stroke-width="2"><title>{{c.name}} · 末值 {{c.values.at(-1)?.toFixed(2)}}</title></path></svg><div class="benchmark-legend"><span v-for="c in chart.curves" :key="c.name"><i :style="{background:c.color}"></i>{{c.name}} {{c.values.at(-1)?.toFixed(2)}}</span></div><p>共同有效区间 {{chart.first}} 至 {{chart.last}}，{{chart.count}} 个观测点。各子区间独立起算；曲线归一化不代表费用、收益口径或风险敞口相同。</p></template><p v-else>暂无共同有效区间，不绘制基准曲线。</p></div></template>
<style scoped>/* 基准图独立设置边框、坐标与图例，窄屏时允许工具栏换行。 */
.benchmark-chart{margin:24px 0;border:1px solid #e4ebe5;border-radius:7px;padding:18px 16px}.benchmark-toolbar{display:flex;justify-content:space-between;align-items:center;gap:12px;font-size:12px}.benchmark-toolbar b{font-weight:500}.benchmark-toolbar label{display:flex;align-items:center;gap:8px;color:#8b998e}.benchmark-toolbar select{font-size:11px;padding:6px;width:auto}.benchmark-chart svg{width:100%;max-height:300px;margin-top:15px}.benchmark-chart text{font:11px system-ui;fill:#9aa69d}.benchmark-legend{display:flex;justify-content:center;flex-wrap:wrap;gap:20px;font-size:11px;color:#708476}.benchmark-legend span{display:flex;align-items:center;gap:7px}.benchmark-legend i{width:15px;height:3px;display:block}.benchmark-chart p{font-size:10px;line-height:1.9;color:#98a398;margin:17px 0 0}@media(max-width:700px){.benchmark-chart{padding:14px 10px}.benchmark-toolbar{font-size:10px;flex-wrap:wrap}.benchmark-legend{font-size:9px;gap:10px}.benchmark-chart p{font-size:9px}}</style>
