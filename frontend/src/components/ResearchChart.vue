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
  const pointerColor = mixColor(palette.muted, palette.border, .3)
  const common = {
    animation: false,
    textStyle: { fontFamily: '"Segoe UI Variable Text", Inter, "Microsoft YaHei UI", sans-serif', color: palette.muted, fontSize: 11 },
    tooltip: {
      trigger: 'axis' as const,
      backgroundColor: palette.surface,
      borderColor: palette.border,
      borderWidth: 1,
      padding: [10, 14],
      textStyle: { color: palette.text, fontSize: 12 },
      axisPointer: { type: 'cross' as const, lineStyle: { color: pointerColor, type: 'dashed' as const, width: 1 }, crossStyle: { color: pointerColor, width: 1 }, label: { backgroundColor: palette.surfaceSoft, color: palette.text, borderColor: palette.border, borderWidth: 1 } },
      extraCssText: 'border-radius: 10px; box-shadow: 0 10px 28px rgba(0,0,0,0.12);',
    },
  }
  // 权益曲线按归档估值绘制，横轴使用各点原始时间。
  if (props.kind === 'equity') {
    chart.setOption({ ...common, grid: { left: 65, right: 24, top: 28, bottom: 36 }, xAxis: { type: 'category', data: props.curve.map(x => x.ts.slice(0, 10)), axisLine: { lineStyle: { color: palette.border } }, axisTick: { show: false }, axisLabel: { color: palette.muted } }, yAxis: { type: 'value', scale: true, axisLabel: { color: palette.muted }, splitLine: { lineStyle: { color: palette.border, type: 'dashed', opacity: .7 } } }, series: [{ type: 'line', smooth: .12, data: props.curve.map(x => x.equity===null||x.equity===undefined||!Number.isFinite(Number(x.equity))?null:Number(x.equity)), connectNulls: false, showSymbol: false, lineStyle: { color: palette.accentText, width: 2.2 }, areaStyle: { color: mixColor(palette.accent, palette.surface, .88) } }] }, true)
    return
  }
  const dates = props.bars.map(x => x.ts.slice(0, 16).replace('T', ' '))
  // 总览仅绘制实际收盘价与成交量；细节页继续使用完整 K 线。
  if (props.kind === 'trend') {
    // 缺失或非数字留空，不把空值画成 0。
    const validNumber = (value: unknown) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value)) ? null : Number(value)
    chart.setOption({
      ...common,
      grid: [{ left: 58, right: 24, top: 14, height: '64%' }, { left: 58, right: 24, top: '78%', height: '11%' }],
      axisPointer: { link: [{ xAxisIndex: 'all' }] },
      // 日期只标在下方成交量轴，价格轴不再重复。
      xAxis: [0, 1].map(index => ({ type: 'category', gridIndex: index, data: dates, boundaryGap: false, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { show: index === 1, color: palette.muted, fontSize: 10, hideOverlap: true, formatter: (value: string) => value.slice(5, 10).replace('-', '/') } })),
      yAxis: [{ scale: true, splitNumber: 3, axisLabel: { color: palette.muted, fontSize: 10 }, splitLine: { lineStyle: { color: palette.border, type: 'dashed', opacity: .6 } } }, { gridIndex: 1, show: false, min: 0 }],
      series: [
        // 不复权收盘单独成线；空缺不连线，避免把缺失行情补成趋势。
        { name: '不复权收盘', type: 'line', smooth: .16, data: props.bars.map(bar => validNumber(bar.close)), showSymbol: props.bars.length === 1, symbol: 'circle', symbolSize: 6, connectNulls: false, lineStyle: { color: palette.accentText, width: 2.4 }, itemStyle: { color: palette.accentText }, areaStyle: { color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [{ offset: 0, color: mixColor(palette.accent, palette.surface, .62) }, { offset: .85, color: mixColor(palette.accent, palette.surface, .95) }, { offset: 1, color: palette.surface }]) } },
        // 成交量放在下方副图，与价格共用时间轴。
        { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: props.bars.map(bar => validNumber(bar.volume)), barMaxWidth: 7, itemStyle: { color: mixColor(palette.accentText, palette.surface, .62), borderRadius: [3, 3, 0, 0] } },
      ],
    }, true)
    return
  }
  // 下方副图按用户选择展示 RSI、ATR、MACD 或成交量，与主图共享时间轴。
  const lowerData = props.indicator === 'rsi' ? props.indicators.map(x => x.rsi14) : props.indicator === 'atr' ? props.indicators.map(x => x.atr14) : props.indicator === 'macd' ? props.indicators.map(x => x.macd) : props.bars.map(x => x.volume)
  chart.setOption({ ...common, legend: { data: ['MA5', 'MA20', 'MA60'], top: 2, right: 20, icon: 'roundRect', itemWidth: 12, itemHeight: 3, textStyle: { color: palette.muted, fontSize: 11 } }, axisPointer: { link: [{ xAxisIndex: 'all' }] }, grid: [{ left: 60, right: 24, top: 36, height: '54%' }, { left: 60, right: 24, top: '73%', height: '14%' }], xAxis: [0, 1].map(i => ({ type: 'category', data: dates, gridIndex: i, boundaryGap: true, axisLine: { lineStyle: { color: palette.border } }, axisLabel: { show: i === 1, hideOverlap: true, color: palette.muted }, axisTick: { show: false } })), yAxis: [{ scale: true, axisLabel: { color: palette.muted }, splitLine: { lineStyle: { color: palette.border, type: 'dashed', opacity: .7 } } }, { gridIndex: 1, scale: true, splitNumber: 2, splitLine: { show: false }, axisLabel: { formatter: compact, color: palette.muted } }], dataZoom: [{ type: 'inside', xAxisIndex: [0, 1], start: Math.max(0, 100 - 10000 / props.bars.length), end: 100 }], series: [{ type: 'candlestick', name: '不复权OHLC', data: props.bars.map(x => [x.open, x.close, x.low, x.high]), itemStyle: { color: palette.up, color0: palette.down, borderColor: palette.up, borderColor0: palette.down } }, ...[5, 20, 60].map((n, i) => ({ type: 'line', name: 'MA' + n, data: props.indicators.map(x => x['ma' + n]), showSymbol: false, lineStyle: { width: 1.4, color: ['#d4a24c', '#4f86d6', '#9b72cf'][i] }, connectNulls: false })), { type: props.indicator === 'rsi' || props.indicator === 'atr' ? 'line' : 'bar', xAxisIndex: 1, yAxisIndex: 1, data: lowerData, showSymbol: false, itemStyle: { color: mixColor(palette.accentText, palette.surface, .48), borderRadius: [2, 2, 0, 0] }, lineStyle: { color: palette.accentText, width: 1.6 } }] }, true)
}
// 挂载后首次绘制，并监听容器尺寸变化以调整画布。
onMounted(() => { draw(); observer = new ResizeObserver(() => chart?.resize()); if (root.value) observer.observe(root.value) })
// 数据引用或副图选择变化时，等待 DOM 更新后重绘。
watch(() => [props.bars, props.indicators, props.curve, props.kind, props.indicator, appearancePalette.value], async () => { await nextTick(); draw() }, { deep: false })
// 卸载前断开尺寸监听并销毁 ECharts 实例，释放画布资源。
onBeforeUnmount(() => { observer?.disconnect(); chart?.dispose() })
</script>
<!-- 图表提供可访问说明，缺少真实数据时显示明确空状态。 -->
<template><div class="chart-wrap"><div ref="root" class="chart-canvas" role="img" :aria-label="kind === 'equity' ? '模拟权益曲线' : kind === 'trend' ? '指数不复权收盘价与成交量走势' : '证券K线和技术指标图'"></div><div v-if="!hasData" class="chart-empty"><div class="empty-chart-icon">⌁</div><strong>等待真实K线数据</strong><span>同步行情后，这里将显示价格与指标</span></div></div></template>
