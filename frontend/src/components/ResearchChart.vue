<script setup lang="ts">
// 按需绘制不复权价格与指标图或模拟权益曲线，并管理图表尺寸和销毁。
import { computed, onMounted, onBeforeUnmount, ref, watch, nextTick } from 'vue'
import * as echarts from 'echarts/core'
import { CandlestickChart, LineChart, BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent, DataZoomComponent, LegendComponent } from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { appearancePalette, mixColor } from '../appearance'
echarts.use([CandlestickChart, LineChart, BarChart, GridComponent, TooltipComponent, DataZoomComponent, LegendComponent, CanvasRenderer])
// 统一接收 K 线、技术指标与权益数据，并为缺失序列提供空列表。
const props = withDefaults(defineProps<{ bars?: any[], indicators?: any[], curve?: any[], kind?: string, indicator?: string }>(), { bars: () => [], indicators: () => [], curve: () => [], kind: 'candle', indicator: 'volume' })
const root = ref<HTMLElement>(); let chart: echarts.ECharts | undefined; let observer: ResizeObserver | undefined
// 将成交量等大数值转换为中文万、亿单位。
const compact = (v: number) => Math.abs(v) >= 1e8 ? (v / 1e8).toFixed(1) + '亿' : Math.abs(v) >= 1e4 ? (v / 1e4).toFixed(0) + '万' : String(v)
// 按图表类型判断是否存在可绘制的数据。
const hasData = computed(() => props.kind === 'equity' ? props.curve.length > 0 : props.bars.length > 0)
// 复用图表实例，按权益或 K 线模式生成配置，缺少数据时清空画布。
function draw() {
  if (!root.value || !hasData.value) { chart?.clear(); return }
  chart ||= echarts.init(root.value)
  const palette = appearancePalette.value
  const common = { animation: false, textStyle: { fontFamily: 'Inter, Microsoft YaHei, sans-serif', color: palette.muted, fontSize: 11 }, tooltip: { trigger: 'axis' as const, backgroundColor: palette.surface, borderColor: palette.border, textStyle: { color: palette.text } } }
  // 权益曲线按归档估值绘制，横轴使用各点原始时间。
  if (props.kind === 'equity') {
    chart.setOption({ ...common, grid: { left: 65, right: 24, top: 28, bottom: 36 }, xAxis: { type: 'category', data: props.curve.map(x => x.ts.slice(0, 10)), axisLine: { lineStyle: { color: palette.border } }, axisTick: { show: false }, axisLabel: { color: palette.muted } }, yAxis: { type: 'value', scale: true, axisLabel: { color: palette.muted }, splitLine: { lineStyle: { color: palette.border, type: 'dashed' } } }, series: [{ type: 'line', data: props.curve.map(x => x.equity===null||x.equity===undefined||!Number.isFinite(Number(x.equity))?null:Number(x.equity)), connectNulls: false, showSymbol: false, lineStyle: { color: palette.accentText, width: 2 }, areaStyle: { color: mixColor(palette.accent, palette.surface, .9) } }] }, true)
    return
  }
  const dates = props.bars.map(x => x.ts.slice(0, 16).replace('T', ' '))
  // 下方副图按用户选择展示 RSI、ATR、MACD 或成交量，与主图共享时间轴。
  const lowerData = props.indicator === 'rsi' ? props.indicators.map(x => x.rsi14) : props.indicator === 'atr' ? props.indicators.map(x => x.atr14) : props.indicator === 'macd' ? props.indicators.map(x => x.macd) : props.bars.map(x => x.volume)
  chart.setOption({ ...common, legend: { data: ['MA5', 'MA20', 'MA60'], top: 0, right: 20, itemWidth: 14, textStyle: { color: palette.muted, fontSize: 11 } }, axisPointer: { link: [{ xAxisIndex: 'all' }] }, grid: [{ left: 60, right: 24, top: 35, height: '55%' }, { left: 60, right: 24, top: '73%', height: '14%' }], xAxis: [0, 1].map(i => ({ type: 'category', data: dates, gridIndex: i, boundaryGap: true, axisLine: { lineStyle: { color: palette.border } }, axisLabel: { show: i === 1, hideOverlap: true, color: palette.muted }, axisTick: { show: false } })), yAxis: [{ scale: true, axisLabel: { color: palette.muted }, splitLine: { lineStyle: { color: palette.border, type: 'dashed' } } }, { gridIndex: 1, scale: true, splitNumber: 2, splitLine: { show: false }, axisLabel: { formatter: compact, color: palette.muted } }], dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - 10000 / props.bars.length), end: 100 }], series: [{ type: 'candlestick', name: '不复权OHLC', data: props.bars.map(x => [x.open, x.close, x.low, x.high]), itemStyle: { color: palette.up, color0: palette.down, borderColor: palette.up, borderColor0: palette.down } }, ...[5, 20, 60].map((n, i) => ({ type: 'line', name: 'MA' + n, data: props.indicators.map(x => x['ma' + n]), showSymbol: false, lineStyle: { width: 1.3, color: ['#d0a55a', '#5d87c8', '#a07eb9'][i] }, connectNulls: false })), { type: props.indicator === 'rsi' || props.indicator === 'atr' ? 'line' : 'bar', xAxisIndex: 1, yAxisIndex: 1, data: lowerData, showSymbol: false, itemStyle: { color: mixColor(palette.accentText, palette.surface, .45) }, lineStyle: { color: palette.accentText } }] }, true)
}
// 挂载后首次绘制，并监听容器尺寸变化以调整画布。
onMounted(() => { draw(); observer = new ResizeObserver(() => chart?.resize()); if (root.value) observer.observe(root.value) })
// 数据引用或副图选择变化时，等待 DOM 更新后重绘。
watch(() => [props.bars, props.indicators, props.curve, props.kind, props.indicator, appearancePalette.value], async () => { await nextTick(); draw() }, { deep: false })
// 卸载前断开尺寸监听并销毁 ECharts 实例，释放画布资源。
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>
<!-- 图表提供可访问说明，缺少真实数据时显示明确空状态。 -->
<template><div class="chart-wrap"><div ref="root" class="chart-canvas" role="img" :aria-label="kind === 'equity' ? '模拟权益曲线' : '证券K线和技术指标图'"></div><div v-if="!hasData" class="chart-empty"><div class="empty-chart-icon">⌁</div><strong>等待真实K线数据</strong><span>同步行情后，这里将显示价格与指标</span></div></div></template>
