import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => ({
  plugins: [vue()],
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8765' } },
  build: { chunkSizeWarningLimit: 1100, outDir: mode === 'public' ? 'dist-public' : 'dist', target: 'es2022' },
}))
