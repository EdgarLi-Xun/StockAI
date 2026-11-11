export const number = (value: unknown, digits = 2) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value))
  ? '—' : Number(value).toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
export const percent = (value: unknown) => value === null || value === undefined || value === '' || !Number.isFinite(Number(value))
  ? '—' : (Number(value) > 0 ? '+' : '') + Number(value).toFixed(2) + '%'
export const direction = (value: unknown) => Number(value) > 0 ? 'up' : Number(value) < 0 ? 'down' : ''
export const dateTime = (value: string | undefined) => !value ? '尚无记录' : Number.isNaN(new Date(value).getTime()) ? value
  : new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date(value))

/** 已记录持仓在前，其次置顶收藏；同级保持原来的顺序。 */
export function preferHoldings(rows: any[]): any[] {
  const rank = (row: any) => {
    const quantity = row.position?.quantity
    const cost = row.position?.cost_price
    const held = typeof quantity === 'number' && quantity > 0 && cost !== null && cost !== undefined && cost !== ''
    if (held && row.pinned) return 0
    if (held) return 1
    if (row.pinned) return 2
    return 3
  }
  return rows.map((row, index) => ({ row, index, rank: rank(row) })).sort((a, b) => a.rank - b.rank || a.index - b.index).map(item => item.row)
}

function positive(value: unknown) {
  const number = Number(value)
  return Number.isFinite(number) && number > 0 ? number : 0
}

function weekKey(value: string) {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value.slice(0, 10)
  const day = new Date(date)
  day.setDate(date.getDate() + 4 - (date.getDay() || 7))
  const yearStart = new Date(day.getFullYear(), 0, 1)
  return day.getFullYear() + '-' + Math.ceil((((day.getTime() - yearStart.getTime()) / 86400000) + 1) / 7)
}

/** 图上补今天的最新价。不写入已完成日线，研究仍用收盘 K 线。 */
export function withLiveBar(bars: any[] = [], spot: any, timeframe = '1d') {
  if (!spot || spot.kind !== 'spot' || !positive(spot.price) || !spot.quote_time) return bars
  const last = bars[bars.length - 1]
  if (last?.ts && String(spot.quote_time) <= String(last.ts)) return bars
  const price = positive(spot.price)
  const open = positive(spot.open) || positive(spot.prev_close) || price
  const high = Math.max(positive(spot.high) || price, price, open)
  const low = Math.min(...[positive(spot.low), price, open].filter(value => value > 0))
  const live = { ts: spot.quote_time, open, high, low, close: price, volume: positive(spot.volume), provisional: true }
  if (timeframe === '1w' && last && weekKey(String(last.ts)) === weekKey(String(spot.quote_time))) {
    return [...bars.slice(0, -1), { ...last, ts: spot.quote_time, high: Math.max(positive(last.high), high), low: Math.min(positive(last.low) || low, low), close: price, volume: positive(last.volume) + live.volume, provisional: true }]
  }
  return [...bars, live]
}
