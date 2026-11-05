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
