import { useEffect, useState, useCallback } from 'react'
import MarketClock from './MarketClock'
import KpiBar from './KpiBar'
import MarketChart from './MarketChart'
import IndicatorsPanel from './IndicatorsPanel'
import AITheater from './AITheater'
import AccountGrid from './AccountGrid'
import HeroAI from './HeroAI'
import LiveStatusBar from './LiveStatusBar'
import MetaQualityPanel from './MetaQualityPanel'
import RiskEventPanel from './RiskEventPanel'
import { fetchSession, fetchChart, fetchAccounts, fetchSystemHealth, fetchAIFlow, visionStatus, AuthError } from '../services/api'
import { mockSession, mockChart, mockAccounts } from '../services/mock'
import VisionModelPanel from './VisionModelPanel'

const REFRESH = 3000

export default function Dashboard({ onLogout }) {
  // 401（token 失效）：直接退回登录，绝不降级成假数据
  const guard = (e) => { if (e instanceof AuthError && onLogout) onLogout() }
  const [tf, setTf] = useState('M15')
  const [chart, setChart] = useState(null)
  const [chartLive, setChartLive] = useState('')   // '' | 'ok' | 'err'
  const [chartErr, setChartErr] = useState('')
  const [lastUpdate, setLastUpdate] = useState(null)

  // 市场时钟（独立降级）
  const [session, setSession] = useState(null)
  const [accounts, setAccounts] = useState(null)
  const [health, setHealth] = useState(null)
  // AI 实时状态（auto_loop + key_sources），用于作战图顶部的紧凑横条
  // loading=true 表示还没拿到首次真实数据，避免把初始值渲染成「AI已停止/未配置」
  const [liveStatus, setLiveStatus] = useState({ loading: true, auto_loop: { running: false, cycles: 0 }, key_sources: {} })
  // v4 Meta 质量陪审团（本地时序模型制衡）可视化数据
  const [metaQuality, setMetaQuality] = useState(null)
  // 视觉第四票（知觉模型 qwen2.5vl:3b）在岗/读图状态
  const [vision, setVision] = useState(null)

  // ★ 2026-08-09 轮询雪崩根因修复 ★
  // 原实现 setInterval(load, 3000) 不等待上一轮完成，而单轮 load 是串行 await
  // 四个接口、实测约 10s（仅 accounts 一个就 9s）。3s 一发 → 稳定状态下有 3~4 轮
  // 同时在飞，每轮又在后端并发 4 路 MT5 IPC → 互相争抢 → 越来越慢 → 请求持续
  // 堆积，页面表现为"全屏红条、界面瘫痪"。
  // 现改为两点：
  //   ① 四个接口并发拉取（彼此完全独立，本无串行必要）——单轮耗时从"四者之和"
  //      降为"四者最大值"；
  //   ② 本轮全部结束后才排下一轮（自调度 setTimeout 取代 setInterval）——
  //      任何情况下同一时刻只有一轮在飞，从机制上杜绝堆积。
  useEffect(() => {
    let cancelled = false
    let timer = null

    const load = async () => {
      await Promise.allSettled([
        fetchSession()
          .then((v) => { if (!cancelled) setSession(v) })
          .catch((e) => { guard(e); if (!cancelled) setSession(mockSession()) }),
        fetchAccounts()
          .then((v) => { if (!cancelled) setAccounts(v) })
          .catch((e) => {
            guard(e)
            // 不回退到假账户数据：保留上次真实数据，仅首次失败时置空，避免误导
            // （用函数式更新，避免闭包里读到永远为 null 的初始 accounts）
            if (!cancelled) setAccounts((prev) => prev || { portfolio: null, accounts: [] })
          }),
        fetchSystemHealth()
          .then((v) => { if (!cancelled) setHealth(v) })
          .catch(() => { /* 健康检查非关键：失败时静默 */ }),
        // AI 实时状态：每轮都更新（cycles/last_cycle 来自 _auto_loop，60秒一轮）
        // 单独设置 10s 超时，避免大 debate 数据偶发卡住拖累整轮轮询
        fetchAIFlow({ timeout: 10000 })
          .then((af) => {
            if (cancelled || !af) return
            if (af.live_status) {
              // 去掉 loading 标记，首次成功后即按真实状态渲染
              setLiveStatus({ ...af.live_status, loading: false })
            }
            if (af.meta_quality) setMetaQuality(af.meta_quality)
          })
          .catch(() => {
            // 非关键：拉取失败时静默，保留上次真实状态，绝不回退到初始假状态
            if (!cancelled) setLiveStatus((prev) => ({ ...prev, loading: false }))
          }),
        // 视觉第四票（知觉模型）：每轮都更新，非关键，失败静默保留上次值
        visionStatus()
          .then((v) => { if (!cancelled) setVision(v) })
          .catch(() => { /* 非关键：保留上次真实状态 */ }),
      ])
      if (!cancelled) timer = setTimeout(load, REFRESH)
    }

    load()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [])

  // 行情图（周期切换 / 定时刷新）
  // 关键修复：绝不回退到随机假 K 线（交易系统高危）。
  // 成功→显示真实 MT5 数据并记录更新时间；失败→保留上次真实数据并标记"中断重试"，绝不伪造行情。
  const loadChart = useCallback(async (t) => {
    try {
      const c = await fetchChart(t)
      setChartLive('ok')
      setChart(c)
      setChartErr('')
      setLastUpdate(new Date())
    } catch (e) {
      guard(e)
      setChartLive('err')
      setChartErr('行情数据中断，正在重试…')
    }
  }, [])

  // 同上：自调度取代 setInterval，避免行情接口偶发变慢时请求堆积
  useEffect(() => {
    let cancelled = false
    let timer = null
    const tick = async () => {
      await loadChart(tf)
      if (!cancelled) timer = setTimeout(tick, REFRESH)
    }
    tick()
    return () => { cancelled = true; if (timer) clearTimeout(timer) }
  }, [tf, loadChart])

  const sessionData = session || mockSession()
  const accountsData = accounts || { portfolio: null, accounts: [] }
  const chartData = chart

  return (
    <div className="wrap">
      {/* ★ Hero：系统身份（纯AI定位） */}
      <HeroAI />

      {/* 顶栏：KPI + 市场时钟 */}
      <div className="top">
        <KpiBar portfolio={accountsData.portfolio} />
        <MarketClock session={sessionData} health={health} />
      </div>

      {/* 模块 C 行情作战图 */}
      <div className="panel">
        <div className="h">
          行情作战图 · XAUUSD
          <span className={`live ${chartLive === 'err' ? 'mock' : ''}`}>
            {chartLive === 'err' ? '中断重试' : (chartLive === 'ok' ? '实时' : '连接中')}
          </span>
          {lastUpdate && (
            <span style={{ fontSize: 11, marginLeft: 8, color: 'var(--sub)' }}>
              更新 {lastUpdate.toLocaleTimeString()}
            </span>
          )}
        </div>
        {/* ── AI 实时状态横条（紧贴作战图标题下） ── */}
        <LiveStatusBar liveStatus={liveStatus} compact />
        <div className="mc">
          {chartData ? (
            <MarketChart chart={chartData} onSelectTf={setTf} currentTf={tf} />
          ) : (
            <div className="chart-loading">正在连接 MT5 行情…</div>
          )}
          <IndicatorsPanel chart={chartData} health={health} />
        </div>
        {chartLive === 'err' && (
          <div className="login-err" style={{ marginTop: 8 }}>{chartErr}</div>
        )}
      </div>

      {/* 模块 B AI 工作剧场（视觉第四票作为第 6 张模型卡片放进擂台网格） */}
      <AITheater vision={vision} />

      {/* 模块 B2 v4 Meta 质量陪审团 · 本地时序模型制衡（客户一看就懂的 AI 工作可视化） */}
      <MetaQualityPanel data={metaQuality} />

      {/* 模块 B2.5 视觉第四票 · 知觉模型在岗可视化（增强信号第5路，非关键展示） */}
      <VisionModelPanel data={vision} mode="compact" />

      {/* 模块 B3 风控事件流 · 决策溯源（Phase 4）
          位置是刻意的：紧跟在「AI 说了什么」之后、「账户结果」之前。
          这三块连起来读就是一条完整因果链——
          AI 给了方向 → 质量陪审团打了分 → 风控放行或拦截 → 账户上出现（或没出现）这笔单。
          此前这条链在第三环断掉，客户只能看到「AI 很忙但账户没动静」。
          degrade 直接复用 Dashboard 已有的 health 轮询，不额外发请求。 */}
      <RiskEventPanel degrade={health?.degrade} />

      {/* 模块 A 弹性账户网格 */}
      <AccountGrid data={accountsData} />
    </div>
  )
}
