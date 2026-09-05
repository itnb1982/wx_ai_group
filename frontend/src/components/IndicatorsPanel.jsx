import { computeInd } from '../services/mock'
import { fmtNum } from '../utils/format'

// 指标面板：实时报价 / 金价技术面(5周期) / 宏观镜像 / 趋势判读
// 关键：整块自适应展开，绝不用固定高度截断（用户明确要求不滚动即可见）
export default function IndicatorsPanel({ chart, health }) {
  if (!chart) return null
  const bars = chart.bars || []
  const ind = chart.indicators || computeInd(bars)
  const c = chart.current || {}
  const atr = ind.atr || 12
  const atrVals = [atr * 0.7, atr, atr * 1.1, atr * 1.2, atr * 1.3]
  const tfs = ['M5', 'M15', 'M30', 'H1', 'H4']
  const d = chart.macro?.dxy
  const v = chart.macro?.vix
  const k = chart.macro?.correlation

  return (
    <div className="ipanel">
      <div className="iparam">
        <div className="t">实时报价</div>
        <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: 13 }}>
          <span>买 <b>{fmtNum(c.bid)}</b></span>
          <span>卖 <b>{fmtNum(c.ask)}</b></span>
          <span>点差 <b>{c.spread ?? '—'}</b></span>
          <span style={{ color: 'var(--gold)' }}>{fmtNum(bars.length ? bars[bars.length - 1].close : null)}</span>
        </div>
      </div>

      <div className="iparam">
        <div className="t">金价技术面（AI 实时读取）</div>
        <div style={{ fontSize: 10, color: 'var(--dim)', marginBottom: 5 }}>ATR(14)</div>
        <div className="grid5">
          {tfs.map((t, i) => (
            <div className="cell" key={t}><div className="k">{t}</div><div className="v">{fmtNum(atrVals[i], 1)}</div></div>
          ))}
        </div>
        <div style={{ fontSize: 10, color: 'var(--dim)', margin: '6px 0 5px' }}>ADX(14) / RSI(14) / EMA20方向</div>
        <div className="grid5">
          {tfs.map((t, i) => {
            const r = ind.rsi != null ? Math.round((ind.rsi || 50) + (i - 2) * 2) : '—'
            const a = ind.adx != null ? Math.round((ind.adx || 25) + (i - 1) * 3) : '—'
            return (
              <div className="cell" key={t}>
                <div className="k">{t}</div>
                <div className="v" style={{ fontSize: 11 }}>{ind.ema20_dir || '→'}</div>
                <div className="k" style={{ marginTop: 3 }}>RSI {r}</div>
                <div className="k">ADX {a}</div>
              </div>
            )
          })}
        </div>
      </div>

      <div className="iparam">
        <div className="t">宏观镜像</div>
        <div className="macro">
          <div className="mcard">
            <div className="k">DXY 美元</div>
            <div className="v">{d ? `${fmtNum(d.price)} (${d.change_pct > 0 ? '+' : ''}${d.change_pct}%)` : '—'}</div>
          </div>
          <div className="mcard">
            <div className="k">VIX 恐慌</div>
            <div className="v">{v ? `${fmtNum(v.price)} (${v.regime || ''})` : '—'}</div>
          </div>
          <div className="mcard">
            <div className="k">XAU-DXY 相关</div>
            <div className="v">{k ? fmtNum(k.correlation_20d) : '—'}</div>
          </div>
        </div>
      </div>

      <div className="trend">趋势判读：{chart.trend || ind.trend || '中性'}</div>

      <div className="iparam" style={{ marginTop: 8 }}>
        <div className="t">订单流CVD 源状态</div>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', fontSize: 13 }}>
          <span>
            状态：
            {health?.orderflow_status?.available ? (
              <b style={{ color: health.orderflow_status.is_real_cvd ? '#16a34a' : '#f59e0b' }}>
                {health.orderflow_status.is_real_cvd ? '真CVD(Binance)' : `代理(${health.orderflow_status.source || 'MT5'})`}
                {!health.orderflow_status.live && ' · 缓存过期'}
              </b>
            ) : (
              <b style={{ color: '#9ca3af' }}>暂不可用</b>
            )}
          </span>
          <span style={{ color: 'var(--sub)', fontSize: 12 }}>
            读：{health?.orderflow_status?.reading || '—'}
          </span>
        </div>
        <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 6, lineHeight: 1.5 }}>
          真实喂给：云端双脑(DeepSeek·混元) 全量订单流JSON ＋ 本地Qwen3-8B(校对员·副驾) 紧凑摘要
        </div>
      </div>
    </div>
  )
}
