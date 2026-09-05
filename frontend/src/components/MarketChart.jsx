import { useEffect, useRef } from 'react'

const TFS = ['M5', 'M15', 'M30', 'H1', 'H4']

// 行情作战图（左列）：周期切换 + Canvas 蜡烛图（红涨绿跌 + EMA/布林叠加 + AI 作战布防层）
export default function MarketChart({ chart, onSelectTf, currentTf }) {
  const cvRef = useRef(null)
  const bars = chart?.bars || []
  const ind = chart?.indicators || null
  const ai = chart?.ai_defense || null
  const cur = chart?.current || null

  useEffect(() => {
    const cv = cvRef.current
    if (!cv) return
    const dpr = window.devicePixelRatio || 1
    const W = cv.clientWidth
    const H = cv.clientHeight
    cv.width = W * dpr
    cv.height = H * dpr
    const ctx = cv.getContext('2d')
    ctx.scale(dpr, dpr)
    ctx.clearRect(0, 0, W, H)
    if (!bars.length) return

    const volH = H * 0.18
    const priceH = H * 0.78
    const pad = 8
    const highs = bars.map((b) => b.high)
    const lows = bars.map((b) => b.low)
    const max = Math.max(...highs)
    const min = Math.min(...lows)
    const rng = max - min || 1
    const n = bars.length
    const bw = Math.max(2, ((W - pad * 2) / n) * 0.62)
    const xs = (i) => pad + ((W - pad * 2) * i) / n + (W - pad * 2) / n / 2
    const yp = (v) => pad + (1 - (v - min) / rng) * priceH

    // 网格 + 价格刻度
    ctx.strokeStyle = 'rgba(31,45,74,.5)'
    ctx.lineWidth = 1
    for (let g = 0; g <= 4; g++) {
      const y = pad + (priceH * g) / 4
      ctx.beginPath()
      ctx.moveTo(pad, y)
      ctx.lineTo(W - pad, y)
      ctx.stroke()
      ctx.fillStyle = '#5b6e91'
      ctx.font = '10px sans-serif'
      ctx.fillText((max - (rng * g) / 4).toFixed(1), 2, y - 2)
    }

    // 布林/EMA 叠加
    if (ind) {
      const series = (arr) => {
        if (!arr || arr.length < n) return
        ctx.beginPath()
        for (let i = 0; i < n; i++) {
          if (arr[i] == null) continue
          const x = xs(i)
          const y = yp(arr[i])
          i === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y)
        }
        ctx.stroke()
      }
      ctx.strokeStyle = 'rgba(176,123,255,.5)'
      ctx.lineWidth = 1
      series(ind.boll_upper_series)
      series(ind.boll_lower_series)
      ctx.strokeStyle = 'rgba(255,207,77,.8)'
      ctx.lineWidth = 1.4
      series(ind.ema20_series)
    }

    // 蜡烛 + 成交量
    const maxVol = Math.max(...bars.map((b) => b.volume || 0)) || 1
    for (let i = 0; i < n; i++) {
      const b = bars[i]
      const x = xs(i)
      const up = b.close >= b.open
      ctx.strokeStyle = up ? '#ff5c6c' : '#2ee6a0'
      ctx.fillStyle = up ? '#ff5c6c' : '#2ee6a0'
      ctx.beginPath()
      ctx.moveTo(x, yp(b.high))
      ctx.lineTo(x, yp(b.low))
      ctx.stroke()
      const yo = yp(b.open)
      const yc = yp(b.close)
      const top = Math.min(yo, yc)
      ctx.fillRect(x - bw / 2, top, bw, Math.max(1, Math.abs(yc - yo)))
      const vh = ((b.volume || 0) / maxVol) * volH
      ctx.fillStyle = up ? 'rgba(255,92,108,.3)' : 'rgba(46,230,160,.3)'
      ctx.fillRect(x - bw / 2, H - vh, bw, vh)
    }

    // ══════════════════════════════════════════
    // ★ P0-1 AI 作战布防 Overlay：让客户一眼看到"AI 在图上布了什么防"
    // ══════════════════════════════════════════
    const biasColor =
      ai?.net_bias === 'short' ? '255,92,108' : ai?.net_bias === 'long' ? '46,230,160' : '138,160,196'

    // 入场区色带（avg_entry ± range）
    if (ai && ai.avg_entry) {
      const range = Math.max((max - min) * 0.03, (ai.avg_entry || 1) * 0.0015)
      const yTop = yp(ai.avg_entry + range)
      const yBot = yp(ai.avg_entry - range)
      ctx.fillStyle = `rgba(${biasColor},0.10)`
      ctx.fillRect(pad, yTop, W - pad * 2, Math.max(2, yBot - yTop))
      const yE = yp(ai.avg_entry)
      ctx.strokeStyle = `rgba(${biasColor},0.85)`
      ctx.lineWidth = 1.2
      ctx.setLineDash([6, 4])
      ctx.beginPath()
      ctx.moveTo(pad, yE)
      ctx.lineTo(W - pad, yE)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = `rgba(${biasColor},0.95)`
      ctx.font = '10px sans-serif'
      ctx.fillText('AI 入场 ' + ai.avg_entry.toFixed(1), pad + 2, yE - 3)
    }

    // SL / TP 参考线
    const drawHL = (val, color, label) => {
      if (!val) return
      const y = yp(val)
      if (y < pad || y > priceH) return
      ctx.strokeStyle = color
      ctx.lineWidth = 1
      ctx.setLineDash([3, 3])
      ctx.beginPath()
      ctx.moveTo(pad, y)
      ctx.lineTo(W - pad, y)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = color
      ctx.font = '9px sans-serif'
      ctx.fillText(label + ' ' + val.toFixed(1), W - pad - 78, y - 3)
    }
    drawHL(ai?.avg_sl, 'rgba(255,92,108,0.7)', 'AI 止损')
    drawHL(ai?.avg_tp, 'rgba(46,230,160,0.7)', 'AI 止盈')

    // 当前价横线（用 bid 或最后收盘兜底）
    const curPrice = cur?.bid ?? bars[n - 1]?.close
    if (curPrice) {
      const y = yp(curPrice)
      ctx.strokeStyle = 'rgba(255,207,77,0.9)'
      ctx.lineWidth = 1
      ctx.setLineDash([2, 3])
      ctx.beginPath()
      ctx.moveTo(pad, y)
      ctx.lineTo(W - pad, y)
      ctx.stroke()
      ctx.setLineDash([])
      ctx.fillStyle = '#ffcf4d'
      ctx.font = 'bold 9px sans-serif'
      ctx.fillText('现价 ' + curPrice.toFixed(1), W - pad - 70, y - 3)
    }
  }, [bars, ind, currentTf, ai, cur])

  return (
    <div className="mc-wrap">
      {/* 顶部：MT5 主号信息带（替代原"行情作战图·XAUUSD"重复标题） */}
      {chart?.mt5?.login ? (
        <div className="chart-mt5-card" title={chart.mt5.name || ''}>
          <div className="cmc-avt">
            <span>{String(chart.mt5.login).slice(-2)}</span>
          </div>
          <div className="cmc-info">
            <div className="cmc-top">
              <span className="cmc-tag">MT5 主号</span>
              <span className="cmc-login">#{String(chart.mt5.login).replace(/(\d{3})(\d{3})(\d{2,4})/, '$1 $2 $3')}</span>
              <span className="cmc-dot" />
              <span className="cmc-online">行情直连</span>
            </div>
            <div className="cmc-sub">
              <span className="cmc-name">{chart.mt5.name || '主号'}</span>
              <span className="cmc-sep">·</span>
              <span className="cmc-srv">{chart.mt5.server || ''}</span>
            </div>
          </div>
          <div className="cmc-crown" title="行情主号">⚜</div>
        </div>
      ) : null}

      {/* ★ AI 方向徽章（P0-1：净方向 + 多空力度 + 人话判读，客户视角一眼懂） */}
      {ai && ai.total > 0 && (
        <div className={`ai-badge ${ai.net_bias}`}>
          <span className="ab-arrow">{ai.net_bias === 'short' ? '▼' : ai.net_bias === 'long' ? '▲' : '■'}</span>
          <span className="ab-text">AI {ai.net_bias === 'short' ? '看空' : ai.net_bias === 'long' ? '看多' : '中性'}</span>
          <span className="ab-strength">
            <span className="ab-bar">
              <span style={{ width: Math.min(100, ai.bias_strength) + '%' }} />
            </span>
            {ai.bias_strength}%
          </span>
          <span className="ab-read">{ai.ai_read}</span>
        </div>
      )}

      <div className="tabs">
        {TFS.map((tf) => (
          <div key={tf} className={`tab ${tf === currentTf ? 'on' : ''}`} onClick={() => onSelectTf(tf)}>
            {tf}
          </div>
        ))}
      </div>
      <canvas id="chart" ref={cvRef} />
    </div>
  )
}
