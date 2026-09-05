import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { execSync } from 'node:child_process'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const PROJECT_ROOT = path.resolve(__dirname, '..')

// ★ Phase 7.1 版本单一权威源：构建期从项目根 VERSION 读取并注入前端。
//   前端源码里严禁再出现 v1.0.0 这类硬编码字面量——历史上后端 1.0.0 /
//   package.json 0.1.0 / 登录页 v1.0.0 三处互相矛盾，客户报障说不清跑的是哪版。
function readVersion() {
  for (const p of [path.join(PROJECT_ROOT, 'VERSION'), path.join(__dirname, 'VERSION')]) {
    try {
      const raw = fs.readFileSync(p, 'utf-8').trim()
      if (raw) return raw
    } catch { /* 读不到就试下一个，最终兜底，绝不让构建失败 */ }
  }
  return '0.0.0-unknown'
}

function readGitCommit() {
  try {
    return execSync('git rev-parse --short HEAD', { cwd: PROJECT_ROOT }).toString().trim()
  } catch {
    return 'unknown'
  }
}

const APP_VERSION = readVersion()

// 开发时通过代理转发 /api 到后端，彻底规避 CORS；
// 前端统一用相对路径 /api/... 发起请求。
export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(APP_VERSION),
    __BUILD_TIME__: JSON.stringify(new Date().toISOString()),
    __GIT_COMMIT__: JSON.stringify(readGitCommit()),
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8081',
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: 'dist',
    // 不主动清空输出目录：避免 WorkBuddy safe-delete 拦截导致的 trash 失败。
    // Vite 会按文件名直接覆盖写入，旧 hash 资源无引用、无害残留。
    emptyOutDir: false,
  },
})
