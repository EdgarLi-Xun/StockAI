// 前端引导入口：加载全局样式并根据构建模式选择应用。
import { createApp } from 'vue'
import './style.css'

// 公网与本地使用不同根组件，动态导入使各模式按需加载。
const module = import.meta.env.MODE === 'public' ? await import('./PublicApp.vue') : await import('./App.vue')
// 在模式专属样式之后应用统一的工作台视觉规范。
await import('./workspace.css')
createApp(module.default).mount('#app')
