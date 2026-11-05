// 配置 Vue 开发环境、后端代理和本地及公网的独立构建目录。
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { readFileSync, readdirSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join, relative, resolve } from 'node:path'
const version = /VERSION\s*=\s*"([^"]+)"/.exec(readFileSync(new URL('../backend/cloud/__init__.py', import.meta.url),'utf8'))![1]!

export default defineConfig(({ mode }) => ({
  plugins: [vue(),{
    name:'stockai-build-identity',
    // Vite 在 generateBundle 后还会处理 CSS 与入口。核对最终写出的字节，含 HTML 和公共资源。
    writeBundle:{order:'post',sequential:true,handler(options){
      const folder=resolve(options.dir!),files:Record<string,string>={}
      const collect=(directory:string)=>{
        for(const entry of readdirSync(directory,{withFileTypes:true})){
          const path=join(directory,entry.name)
          if(entry.isDirectory())collect(path)
          else if(entry.isFile()){
            const name=relative(folder,path).replaceAll('\\','/')
            if(name!=='version.json')files[name]=createHash('sha256').update(readFileSync(path)).digest('hex')
          }
        }
      }
      collect(folder)
      writeFileSync(join(folder,'version.json'),JSON.stringify({version,files}))
    }},
  }],
  // 开发时将 API 请求转发给本地共享后端。
  server: { port: 5173, proxy: { '/api': 'http://127.0.0.1:8765' } },
  // 两个构建模式使用不同输出目录，ES2022 支持入口中的顶层异步导入。
  build: { chunkSizeWarningLimit: 1100, outDir: mode === 'public' ? 'dist-public' : 'dist', target: 'es2022' },
}))
