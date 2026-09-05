import { useEffect, useMemo, useState } from 'react'

// v4 Meta 质量陪审团可视化面板
// 把「本地 Chronos 时序模型如何制衡语义大脑」做成客户一看就懂的可视化
// 数据来自后端 /ai-flow 的 meta_quality（meta_quality.py 实时计算）

const REGIME_META = {
  HIGH:     { label: '高质量 · 让利润奔跑', color: '#00e6a8', glow: '0 0 18px rgba(0,230,168,.55)' },
  MID:      { label: '中质量 · 常规分批',   color: '#3b82f6', glow: '0 0 16px rgba(59,130,246,.45)' },
  LOW:      { label: '低质量 · 啃头皮快出', color: '#f59e0b', glow: '0 0 16px rgba(245,158,11,.45)' },
  VERY_LOW: { label: '极弱 · 谨慎观望',     color: '#ef4444', glow: '0 0 16px rgba(239,68,68,.45)' },
  '':       { label: '评估中…',            color: '#94a3b8', glow: 'none' },
}

function Sparkline({ data }) {
  const W = 340, H = 92, PAD = 8
  const p50 = data?.p50, p10 = data?.p10, p90 = data?.p90
  const ready = Array.isArray(p50) && p50.length > 1
  const [t, setT] = useState(0)
  // 轻微扫描动画：让"AI 在算未来"有呼吸感
  useEffect(() => {
    if (!ready) return
    const id = setInterval(() => setT((v) => (v + 1) % 1000), 60)
    return () => clearInterval(id)
  }, [ready])

  const { band, mid, lastX, lastY, ceilY } = useMemo(() => {
    if (!ready) return {}
    const all = [...(p10 || p50), ...(p90 || p50), ...p50]
    const lo = Math.min(...all), hi = Math.max(...all)
    const span = hi - lo || 1
    const n = p50.length
    const x = (i) => PAD + (i / (n - 1)) * (W - 2 * PAD)
    const y = (v) => H - PAD - ((v - lo) / span) * (H - 2 * PAD)
    const midPts = p50.map((v, i) => `${x(i)},${y(v)}`).join(' ')
    const bandTop = (p90 || p50).map((v, i) => `${x(i)},${y(v)}`)
    const bandBot = (p10 || p50).map((v, i) => `${x(i)},${y(v)}`).reverse()
    const bandPts = [...bandTop, ...bandBot].join(' ')
    const lastX = x(n - 1), lastY = y(p50[n - 1])
    let ceilY
    if (data?.chronos_tp_ceiling) ceilY = y(data.chronos_tp_ceiling)
    return { band: bandPts, mid: midPts, lastX, lastY, ceilY, lo, hi }
  }, [p50, p10, p90, data?.chronos_tp_ceiling, ready])

  if (!ready) {
    return (
      <div className="mq-spark-empty">
        <span className="mq-dot" /> 本地 Chronos 时序模型加载中 / 已降级（SMC·Regime 兜底）
      </div>
    )
  }
  const dash = 4 + (t % 12)
  return (
    <svg className="mq-spark" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
      <defs>
        <linearGradient id="mqBand" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="rgba(0,230,168,.28)" />
          <stop offset="100%" stopColor="rgba(0,230,168,.04)" />
        </linearGradient>
      </defs>
      {/* P10~P90 不确定性带 */}
      <polygon points={band} fill="url(#mqBand)" stroke="rgba(0,230,168,.35)" strokeWidth="1" />
      {/* P50 中枢线 */}
      <polyline points={mid} fill="none" stroke="#00e6a8" strokeWidth="2"
        strokeDasharray={`${dash} 6`} style={{ filter: 'drop-shadow(0 0 4px rgba(0,230,168,.6))' }} />
      {/* 末价点 */}
      <circle cx={lastX} cy={lastY} r="3.2" fill="#eafff8" stroke="#00e6a8" strokeWidth="1.5" />
      {/* Chronos P90 止盈天花板 */}
      {ceilY != null && (
        <g>
          <line x1={PAD} x2={W - PAD} y1={ceilY} y2={ceilY}
            stroke="#f59e0b" strokeWidth="1.2" strokeDasharray="5 4" opacity=".85" />
          <text x={W - PAD} y={ceilY - 4} fill="#f59e0b" fontSize="9"
            textAnchor="end">P90 止盈天花板</text>
        </g>
      )}
    </svg>
  )
}

export default function MetaQualityPanel({ data }) {
  const regime = (data?.regime || '').toUpperCase()
  const rm = REGIME_META[regime] || REGIME_META['']
  // ★ Phase 4 命名归一：后端权威字段名是 q_score / chronos_vote
  //   （见 backend/app/services/decision_snapshot.py），而历史 meta_quality
  //   通道给的是 q / chronos_dir。这里两边都认。
  //   为什么不直接改成只认新名：meta_quality 是行情侧算出来的实时值，
  //   provenance 是决策侧冻结的快照，两条通道都要能喂这个面板；
  //   一刀切换名字会让其中一条静默变成"--"。
  const q = typeof data?.q_score === 'number' ? data.q_score
    : typeof data?.q === 'number' ? data.q : null
  const dir = data?.chronos_vote || data?.chronos_dir || 'NEUTRAL'
  const dirLabel = dir === 'BUY' ? '看多' : dir === 'SELL' ? '看空' : '中性'
  const dirColor = dir === 'BUY' ? '#ff4d5e' : dir === 'SELL' ? '#16d39a' : '#94a3b8'
  const avail = data?.chronos_available

  return (
    <div className="panel mq-panel">
      <div className="h">
        AI 信号质量陪审团
        <span className="mq-sub">本地 Chronos 时序模型 · 制衡语义大脑</span>
        <span className="mq-tag" style={{ color: rm.color, borderColor: rm.color, boxShadow: rm.glow }}>
          {rm.label}
        </span>
      </div>

      <div className="mq-body">
        {/* 左：质量分 + 方向 */}
        <div className="mq-left">
          <div className="mq-qscore">
            <div className="mq-qscore-num" style={{ color: rm.color, textShadow: rm.glow }}>
              {q != null ? (q * 100).toFixed(0) : '--'}<span className="mq-qscore-pct">分</span>
            </div>
            <div className="mq-qscore-label">信号质量 Q</div>
          </div>
          <div className="mq-gauge">
            <div className="mq-gauge-fill" style={{
              width: `${q != null ? Math.min(100, q * 100) : 0}%`,
              background: rm.color, boxShadow: rm.glow,
            }} />
            <div className="mq-gauge-th" style={{ left: '35%' }} />
            <div className="mq-gauge-th" style={{ left: '50%' }} />
            <div className="mq-gauge-th" style={{ left: '70%' }} />
          </div>
          <div className="mq-dir" style={{ borderColor: dirColor, color: dirColor }}>
            Chronos 方向（方向锚）：{dirLabel}
            {avail ? '' : '（模型降级）'}
          </div>
        </div>

        {/* 中：Chronos 未来分位数预报动画 */}
        <div className="mq-mid">
          <div className="mq-mid-h">Chronos 未来价格分位数预报（P10–P50–P90）</div>
          <Sparkline data={data} />
          <div className="mq-mid-foot">
            <span>末价 {data?.last_price != null ? data.last_price.toFixed(2) : '--'}</span>
            <span>P50→ {data?.p50_final != null ? data.p50_final.toFixed(2) : '--'}</span>
            <span>P90→ {data?.p90_final != null ? data.p90_final.toFixed(2) : '--'}</span>
            <span>不确定 {(data?.uncertainty != null ? (data.uncertainty * 100).toFixed(2) : '--')}%</span>
          </div>
        </div>

        {/* 右：判定依据（客户一看就懂） */}
        <div className="mq-right">
          <div className="mq-right-h">判定依据</div>
          {data?.notes && data.notes.length ? (
            <ul className="mq-notes">
              {data.notes.slice(0, 4).map((n, i) => <li key={i}>{n}</li>)}
            </ul>
          ) : (
            <div className="mq-notes-empty">本地模型尚未产出判定（等待行情主号数据）</div>
          )}
          <div className="mq-ceiling">
            动态止盈天花板：
            <b style={{ color: '#f59e0b' }}>
              {data?.chronos_tp_ceiling != null ? data.chronos_tp_ceiling.toFixed(2) : '—'}
            </b>
            {regime === 'HIGH' && <span className="mq-ceiling-hi"> ｜ HIGH：让利润奔跑到 P90 再全平</span>}
          </div>
          <div className="mq-ceiling" style={{ color: 'var(--green)', borderColor: 'rgba(46,230,160,.35)' }}>
            ★ Chronos 为融合方向锚：非锚模型与锚反向时权重置 0（仅同向弱票加成）；锚观望则融合票直接 HOLD。
          </div>
          {/* ★ 2026-08-17：TimesFM 风险区间交叉验证（模型科学规划 · 第二把尺子） */}
          {(() => {
            const ct = data?.cross_ts
            if (!ct || !ct.available) {
              return (
                <div className="mq-ceiling" style={{ color: 'var(--dim)', borderColor: 'rgba(148,163,184,.25)' }}>
                  TimesFM 交叉验证：待接入/降级（不影响决策，区间验证缺席）
                </div>
              )
            }
            const tone = ct.agreement === 'high'
              ? { c: '#2ee6a0', txt: '区间一致 · 不确定性低', bg: 'rgba(46,230,160,.08)' }
              : ct.agreement === 'mid'
                ? { c: '#f59e0b', txt: '区间中等分歧 · 谨慎', bg: 'rgba(245,158,11,.08)' }
                : { c: '#ff4d5e', txt: '区间显著分歧 · 止盈已收紧', bg: 'rgba(255,77,94,.08)' }
            return (
              <div className="mq-ceiling" style={{ color: tone.c, borderColor: tone.c, background: tone.bg }}>
                <b>TimesFM 交叉验证</b>：{tone.txt}
                <span style={{ opacity: .8 }}>
                  （分歧度 {(ct.divergence ?? 0).toFixed(2)} · Chronos P90 {ct.c_p90} vs TimesFM P90 {ct.t_p90}）
                </span>
                {ct.note ? <div style={{ fontSize: 10.5, marginTop: 2, opacity: .75 }}>{ct.note}</div> : null}
              </div>
            )
          })()}
        </div>
      </div>
    </div>
  )
}
