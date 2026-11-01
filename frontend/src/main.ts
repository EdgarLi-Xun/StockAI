import { createApp } from 'vue'
import './style.css'

const module = import.meta.env.MODE === 'public' ? await import('./PublicApp.vue') : await import('./App.vue')
createApp(module.default).mount('#app')
