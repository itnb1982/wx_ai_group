import { useEffect, useState, useRef } from 'react'
import Login from './components/Login.jsx'
import Sidebar from './components/Sidebar.jsx'
import Dashboard from './components/Dashboard.jsx'
import AccountManage from './components/AccountManage.jsx'
import StrategyRisk from './components/StrategyRisk.jsx'
import ApiKeyManage from './components/ApiKeyManage.jsx'
import LicenseActivate from './components/LicenseActivate.jsx'
import SystemManage from './components/SystemManage.jsx'
import SignalReference from './components/SignalReference.jsx'
import LicenseBanner from './components/LicenseBanner.jsx'
import { getToken } from './services/api.js'

export default function App() {
  const [token, setToken] = useState(getToken())
  const [user, setUser] = useState(null)
  const [view, setView] = useState('dashboard')

  // ─────────────────────────────────────────────────────────────
  // 后端心跳红警（根治"后端无声死亡"）：独立定时器每 5s ping /api/health，
  // 后端挂了/超时/返回异常 → 立刻全屏红色横幅告警，让客户第一时间知道系统挂了。
  // 与 Dashboard 主刷新解耦：哪怕主刷新被卡住，心跳仍能独立报警。
  // ─────────────────────────────────────────────────────────────
  const [backendDown, setBackendDown] = useState(false)
  const [backendDownSince, setBackendDownSince] = useState(null)
  const downRef = useRef(false)
  useEffect(() => {
    let cancelled = false
    let timer = null
    const ping = async () => {
      const controller = new AbortController()
      // ★ 2026-08-09：3s 太紧。Chronos GPU 推理会短暂占用事件循环，health 请求
      // 排队偶发 >3s，导致前端误报红条。放宽到 8s：真死机 8s 没响应再报警，
      // 仍足够敏感；偶发慢请求不再炸屏。
      const to = setTimeout(() => controller.abort(), 8000)
      let ok = false
      try {
        const r = await fetch('/api/health', { signal: controller.signal })
        clearTimeout(to)
        ok = r.ok
      } catch (e) {
        clearTimeout(to)
        ok = false
      }
      if (cancelled) return
      if (ok && downRef.current) {
        // 恢复
        downRef.current = false
        setBackendDown(false)
        setBackendDownSince(null)
      } else if (!ok && !downRef.current) {
        downRef.current = true
        setBackendDown(true)
        setBackendDownSince(new Date())
      }
      if (!cancelled) timer = setTimeout(ping, 5000)
    }
    ping()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [])

  useEffect(() => {
    const onLogout = () => { setToken(''); setUser(null) }
    window.addEventListener('auth:logout', onLogout)
    return () => window.removeEventListener('auth:logout', onLogout)
  }, [])

  const doLogout = () => { setToken(''); setUser(null) }

  if (!token) {
    return <Login onOk={(u) => { setToken(getToken()); setUser(u) }} />
  }

  return (
    <div className="app-shell">
      {backendDown && (
        <div className="backend-alert">
          <span className="backend-alert-icon">⚠</span>
          <span className="backend-alert-text">
            系统后端已断开连接！交易防线（AI决策 / 止损 / 锁利）全部停止 —
            {backendDownSince ? `自 ${backendDownSince.toLocaleTimeString()} 起` : ''}
            正在尝试自动重连…
          </span>
          <span className="backend-alert-sub">如长时间未恢复，请检查后端进程（supervisor）是否运行</span>
        </div>
      )}
      <Sidebar view={view} onSelect={setView} onLogout={doLogout} user={user} />
      <main className="app-main">
        {/* 授权提示条：sticky 横幅，随内容排布、滚动时吸顶，**绝不遮挡下方内容**。
            授权失效只停开新仓，客户的持仓还在市场里，必须保持可看可平仓。
            刻意放在 app-main 内而非 app-shell 外：app-shell 是 flex 行容器，
            插在里面会被当成第三列把布局撑歪。 */}
        <LicenseBanner onGoActivate={() => setView('license')} />
        {view === 'dashboard' && <Dashboard onLogout={doLogout} />}
        {view === 'accounts' && <AccountManage />}
        {view === 'strategy' && <StrategyRisk />}
        {view === 'keys' && <ApiKeyManage />}
        {view === 'system' && <SystemManage />}
        {view === 'signals' && <SignalReference />}
        {view === 'license' && <LicenseActivate />}
      </main>
    </div>
  )
}
