// 桌面与 Web 共用一份导航；配置、研究记录和任务各有唯一入口。
export const workspaceSections = [
  { label: '行情与研究', items: [
    { id: 'dashboard', label: '研究总览', icon: 'LayoutDashboard' },
    { id: 'watchlist', label: '自选证券', icon: 'Star' },
    { id: 'market', label: '市场行情', icon: 'ChartCandlestick' },
    { id: 'metals', label: '黄金与白银', icon: 'Globe2' },
    { id: 'candidates', label: '候选研究', icon: 'Sparkles' },
    { id: 'reports', label: '研究报告', icon: 'FileText' },
  ] },
  { label: '策略与记录', items: [
    { id: 'simulation', label: '模拟交易', icon: 'Wallet' },
    { id: 'portfolio', label: '组合风险', icon: 'ChartNoAxesCombined' },
    { id: 'experiments', label: '历史实验', icon: 'FlaskConical' },
    { id: 'journals', label: '研究日志', icon: 'BookOpen' },
    { id: 'alerts', label: '条件提醒', icon: 'Bell' },
    { id: 'review', label: '周度复盘', icon: 'CalendarDays' },
  ] },
  { label: '工具与设置', items: [
    { id: 'readiness', label: '数据就绪度', icon: 'ShieldCheck' },
    { id: 'jobs', label: '任务中心', icon: 'Clock3' },
    { id: 'models', label: '模型配置', icon: 'SlidersHorizontal' },
    { id: 'usage', label: '模型用量', icon: 'Activity' },
    { id: 'preferences', label: '个人偏好', icon: 'Settings2' },
    { id: 'system', label: '同步与版本', icon: 'RefreshCw' },
  ] },
]

export function workspaceTitle(route: string) {
  return workspaceSections.flatMap(section => section.items).find(item => item.id === route)?.label
    || ({ account: '账号与数据', admin: '站点管理', assistant: 'AI 助手', backtest: '历史回测档案' } as Record<string, string>)[route]
    || '研究空间'
}
