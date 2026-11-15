import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 后端地址:开发环境走 Vite proxy(同源无跨域);
// 分离部署构建时经 VITE_API_BASE_URL 注入(design.md 2.2/8.2)。
const backend =
  process.env.VITE_API_BASE_URL && process.env.VITE_API_BASE_URL.trim() !== ''
    ? process.env.VITE_API_BASE_URL.trim()
    : 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: backend, changeOrigin: true },
      '/media': { target: backend, changeOrigin: true },
      '/ws': { target: backend, changeOrigin: true, ws: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['src/**/*.test.ts'],
  },
} as never) // vitest 字段由 vitest 读取,合并进 vite config
