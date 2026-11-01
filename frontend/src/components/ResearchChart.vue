<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref, watch, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
echarts.use([CandlestickChart, LineChart, BarChart, GridComponent, TooltipComponent, DataZoomComponent, LegendComponent, CanvasRenderer])
const props = withDefaults(defineProps<{ bars?: any[], indicators?: any[], curve?: any[], kind?: string, indicator?: string }>(), { bars: () => [], indicators: () => [], curve: () => [], kind: 'candle', indicator: 'volume' })
const root = ref<HTMLElement>(); let chart: echarts.ECharts | undefined; let observer: ResizeObserver | undefined
const compact = (v: number) => Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(1) + '亿' : Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + '万' : String(v)
const hasData = computed(() => props.kind === 'equity' ? props.curve.length > 0 : props.bars.length > 0)
function draw() {
  if (!root.value || !hasData.value) { chart?.clear(); return }
  chart ||= echarts.init(root.value)
  const common = { animation: false, textStyle: { fontFamily: 'Inter, Microsoft YaHei, sans-serif', color: '#89929d', fontSize: 11 }, tooltip: { trigger: 'axis' as const, backgroundColor: '#fff', borderColor: '#e5e8ed', textStyle: { color: '#25313a' } } }
  if (props.kind === 'equity') {
    chart.setOption({ ...common, grid: { left: 65, right: 24, top: 28, bottom: 36 }, xAxis: { type: 'category', data: props.curve.map(x => x.ts.slice(0, 10)), axisLine: { lineStyle: { color: '#e9edf0' } }, axisTick: { show: false } }, yAxis: { type: 'value', scale: true, splitLine: { lineStyle: { color: '#eef1f4', type: 'dashed' } } }, series: [{ type: 'line', data: props.curve.map(x => Number(x.equity)), showSymbol: false, lineStyle: { color: '#13856b', width: 2 }, areaStyle: { color: 'rgba(19,133,107,0.07)' } }] }, true)
    return
  }
  const dates = props.bars.map(x => x.ts.slice(0, 16).replace('T', ' '))
  const lowerData = props.indicator === 'rsi' ? props.indicators.map(x => x.rsi14) : props.indicator === 'atr' ? props.indicators.map(x => x.atr14) : props.indicator === 'macd' ? props.indicators.map(x => x.macd) : props.bars.map(x => x.volume)
  chart.setOption({ ...common, legend: { data: ['MA5', 'MA20', 'MA60'], top: 0, right: 20, itemWidth: 14, textStyle: { color: '#89929d', fontSize: 11 } }, axisPointer: { link: [{ xAxisIndex: 'all' }] }, grid: [{ left: 60, right: 24, top: 35, height: '55%' }, { left: 60, right: 24, top: '73%', height: '14%' }], xAxis: [0, 1].map(i => ({ type: 'category', data: dates, gridIndex: i, boundaryGap: true, axisLine: { lineStyle: { color: '#e9edf0' } }, axisLabel: { show: i === 1, hideOverlap: true }, axisTick: { show: false } })), yAxis: [{ scale: true, splitLine: { lineStyle: { color: '#eef1f4', type: 'dashed' } } }, { gridIndex: 1, scale: true, splitNumber: 2, splitLine: { show: false }, axisLabel: { formatter: compact } }], dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - 10000 / props.bars.length), end: 100 }], series: [{ type: 'candlestick', name: '不复权OHLC', data: props.bars.map(x => [x.open, x.close, x.low, x.high]), itemStyle: { color: '#d85e59', color0: '#159477', borderColor: '#d85e59', borderColor0: '#159477' } }, ...[5, 20, 60].map((n, i) => ({ type: 'line', name: 'MA' + n, data: props.indicators.map(x => x['ma' + n]), showSymbol: false, lineStyle: { width: 1.3, color: ['#d0a55a', '#5d87c8', '#a07eb9'][i] }, connectNulls: false })), { type: props.indicator === 'rsi' || props.indicator === 'atr' ? 'line' : 'bar', xAxisIndex: 1, yAxisIndex: 1, data: lowerData, showSymbol: false, itemStyle: { color: '#a4bdb4' }, lineStyle: { color: '#13856b' } }] }, true)
}
onMounted(() => { draw(); observer = new ResizeObserver(() => chart?.resize()); if (root.value) observer.observe(root.value) })
watch(() => [props.bars, props.curve, props.indicator], async () => { await nextTick(); draw() }, { deep: false })
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>
<template><div class="chart-wrap"><div ref="root" class="chart-canvas" role="img" :aria-label="kind === 'equity' ? '模拟权益曲线' : '证券K线和技术指标图'"></div><div v-if="!hasData" class="chart-empty"><div class="empty-chart-icon">⌁</div><strong>等待真实K线数据</strong><span>同步行情后，这里将显示价格与指标</span></div></div></template>
