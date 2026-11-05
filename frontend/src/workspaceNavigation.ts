export interface WorkspaceTab { id: string, label: string, admin?: boolean }
export interface WorkspaceItem extends WorkspaceTab { icon: string, tabs?: WorkspaceTab[] }

// 一级导航按研究任务组织；子页面继续使用原地址和离开保护。
export const workspaceSections: { label: string, items: WorkspaceItem[] }[] = [
  { label: '工作空间', items: [
    { id: 'dashboard', label: '今日总览', icon: 'LayoutDashboard' },
    { id: 'watchlist', label: '证券工作台', icon: 'ChartCandlestick', tabs: [
      { id: 'watchlist', label: '自选证券' }, { id: 'market', label: '市场行情' }, { id: 'metals', label: '黄金与白银' },
    ] },
    { id: 'candidates', label: '候选研究', icon: 'Sparkles' },
    { id: 'ideas', label: '想法池', icon: 'NotebookPen' },
    { id: 'reports', label: '研究档案', icon: 'FileText', tabs: [
      { id: 'reports', label: '研究报告' }, { id: 'journals', label: '研究日志' }, { id: 'review', label: '周度复盘' },
    ] },
  ] },
  { label: '策略与记录', items: [
    { id: 'simulation', label: '模拟与组合', icon: 'Wallet', tabs: [
      { id: 'simulation', label: '模拟交易' }, { id: 'portfolio', label: '组合风险' },
    ] },
    { id: 'experiments', label: '实验与回测', icon: 'FlaskConical', tabs: [
      { id: 'experiments', label: '研究实验' }, { id: 'backtest', label: '历史回测档案' },
    ] },
    { id: 'alerts', label: '条件提醒', icon: 'Bell' },
  ] },
  { label: '工具与设置', items: [
    { id: 'jobs', label: '任务中心', icon: 'Clock3' },
    { id: 'preferences', label: '设置', icon: 'Settings2', tabs: [
      { id: 'preferences', label: '外观与偏好' }, { id: 'models', label: '模型配置' }, { id: 'mail', label: '邮件发送', admin: true },
      { id: 'usage', label: '模型用量' }, { id: 'readiness', label: '数据与来源' }, { id: 'system', label: '同步与版本' },
    ] },
  ] },
]

export function workspaceGroup(route: string) {
  return workspaceSections.flatMap(section => section.items).find(item => item.id === route || item.tabs?.some(tab => tab.id === route))
}
export function workspaceTabs(route: string) { return workspaceGroup(route)?.tabs || [] }
export function workspaceTitle(route: string) {
  const item = workspaceGroup(route)
  return item?.tabs?.find(tab => tab.id === route)?.label || item?.label
    || ({ account: '账号与数据', admin: '站点管理', assistant: 'AI 助手' } as Record<string, string>)[route]
    || '研究空间'
}
