import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '/static': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    // vendor-echarts 约 634 kB：ECharts 三个报告页共用、已单独拆 chunk 利于长缓存，
    // 超过 Vite 默认 500 kB 阈值会持续刷告警。该体积已知且可接受，显式放宽阈值。
    chunkSizeWarningLimit: 700,
    rollupOptions: {
      output: {
        // ECharts 体积大且三个报告页共用，单独拆分为稳定 chunk 便于长缓存
        manualChunks(id) {
          if (id.includes('node_modules')) {
            if (id.includes('echarts') || id.includes('zrender')) return 'vendor-echarts'
            if (id.includes('@tanstack')) return 'vendor-query'
            if (id.includes('react-router') || id.includes('react-dom') || id.includes('/react/')) {
              return 'vendor-react'
            }
          }
          return undefined
        },
      },
    },
  },
})
