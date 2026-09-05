import { useEffect, useState } from 'react'

/**
 * 市场时钟：按"当前服务器盘口"显示
 * - 交易中：主标"亚盘正常交易中 / 欧盘 / 美盘"，副标"已开 1 天 4 小时 38 分"
 * - 休市：主标"周末休市"，副标距下个开盘倒计时
 * - 故障报警：右上角红点 + 模块名
 */
const PHASE_STYLE = {
  1: { color: '#7dd3fc', icon: '🌏', tagline: '亚洲交易时段' },  // 亚盘 - 浅蓝
  2: { color: '#fbbf24', icon: '🌍', tagline: '欧洲交易时段' },  // 欧盘 - 金黄
  3: { color: '#a78bfa', icon: '🌎', tagline: '美洲交易时段' },  // 美盘 - 紫
  0: { color: '#9ca3af', icon: '💤', tagline: '市场休市' },
}

function pad2(n) { return n < 10 ? '0' + n : '' + n }

function formatOpenSince(sec) {
  if (sec <= 0) return ''
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (d > 0) return `已开 ${d} 天 ${h} 小时 ${m} 分`
  if (h > 0) return `已开 ${h} 小时 ${m} 分 ${s} 秒`
  return `已开 ${m} 分 ${s} 秒`
}

function formatCountdown(sec) {
  if (sec <= 0) return '00:00:00'
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  const s = sec % 60
  if (d > 0) return `${d}天 ${pad2(h)}:${pad2(m)}:${pad2(s)}`
  return `${pad2(h)}:${pad2(m)}:${pad2(s)}`
}

export default function MarketClock({ session, health }) {
  // 本地秒级跳秒：每秒强制刷新一次 openSince/countdown 显示
  const [tick, setTick] = useState(0)
  useEffect(() => {
    const t = setInterval(() => setTick(x => x + 1), 1000)
    return () => clearInterval(t)
  }, [])

  if (!session) return <div className="panel clock">加载中…</div>

  const open = session.is_open
  const phase = session.phase_code ?? (open ? 1 : 0)
  const style = PHASE_STYLE[phase] || PHASE_STYLE[0]
  void tick  // 触发重渲染

  // 故障报警：health.faults 非空
  const hasFault = health && health.faults && health.faults.length > 0
  const faultMods = hasFault ? health.faults.map(f => f.module).join(' / ') : null

  return (
    <div className={`panel clock ${open ? 'open' : 'closed'}`}
         style={{ borderColor: hasFault ? '#ef4444' : (open ? style.color : '#475569') }}>
      {hasFault && (
        <div className="fault-badge" title={health.faults.map(f => f.message).join('\n')}>
          <span className="fault-dot" />
          故障：{faultMods}
        </div>
      )}
      <div className="phase-row">
        <span className={`dot ${open ? 'open' : 'closed'}`}
              style={{ background: open ? style.color : '#9ca3af',
                       boxShadow: open ? `0 0 12px ${style.color}` : 'none' }} />
        <span className="state" style={{ color: open ? style.color : '#cbd5e1' }}>
          {open ? style.icon + ' ' : ''}{session.phase_full || (open ? '交易中' : '休市中')}
        </span>
      </div>
      <div className="cd-sub">
        {open ? (
          <span className="open-since">{formatOpenSince(session.open_since_sec || 0)}</span>
        ) : (
          <>
            <span className="cd-label">距下个开盘</span>
            <span className="cd-time">{formatCountdown(session.countdown_to_open_sec || 0)}</span>
          </>
        )}
      </div>
      <div className="meta">
        {session.broker} · {session.timezone} · 服务器时间 {session.server_time}
        <br />
        数据源：{session.source === 'mt5' ? 'MT5 实时' : '静态兜底'}
        {open && session.countdown_to_close_sec > 0 ? (
          <span> · 距美盘收尾 {formatCountdown(session.countdown_to_close_sec)}</span>
        ) : null}
      </div>
    </div>
  )
}
