// 数据来源决定加入或创建空间，避免新环境误建空白空间。
export type DataSource = 'independent' | 'desktop' | 'web' | 'publish' | 'legacy'

export function parseRecovery(text: string) {
  let value: any
  try { value = JSON.parse(text) } catch { throw new Error('恢复材料不是有效的 JSON，请选择原设备导出的恢复材料文件。') }
  if (!value || value.format !== 'stockai-sync-recovery/1' ||
      typeof value.space_id !== 'string' || !/^[0-9a-f]{32}$/.test(value.space_id) ||
      typeof value.key !== 'string' || !value.key || !value.anchor ||
      !Number.isInteger(value.anchor.sequence) || value.anchor.sequence < 0 ||
      typeof value.anchor.hash !== 'string' || !/^[0-9a-f]{64}$/.test(value.anchor.hash)) {
    throw new Error('请选择 StockAI 导出的同步恢复材料；普通备份、登录信息或配置文件不能用于加入空间。')
  }
  return value
}

export function syncConnection(source: DataSource | '', form: Record<string, any>) {
  if (!['desktop', 'web', 'publish'].includes(source)) throw new Error('请先选择已有环境或创建同步空间。')
  const create = source === 'publish'
  return { ...form, create, recovery: create ? undefined : parseRecovery(form.recovery || '') }
}
