import { useEffect, useState, useCallback } from 'react'
import { listAccounts, getStrategy, updateStrategy, setPrimary } from '../services/api'

const AI_MODES = [
  { v: 'dual', t: '多模型协同 (DeepSeek+混元+本地融合)' },
  { v: 'single_ds', t: '仅 DeepSeek' },
  { v: 'single_hy', t: '仅 混元' },
  { v: 'notify_only', t: '仅通知不交易' },
]

const SIZING_MODES = [
  { v: 'smart', t: '智能自适应', desc: '按本金/趋势/同向/信号强度自动算手数' },
  { v: 'fixed', t: '固定比例', desc: '按余额百分比 + ATR 反推（兼容旧逻辑）' },
]

const CAPITAL_SOURCES = [
  { v: 'live', t: '实时余额', desc: '用 MT5 实时余额做风控基准，手数随真实本金缩放（推荐）' },
  { v: 'manual', t: '手动本金', desc: '用下方「基础本金」做基准（小资金账户可锁定参考本金）' },
]

const GATE_MODES = [
  { v: 'off', t: '关闭（不限制）' },
  { v: 'soft', t: '软门（提准非拦截）' },
  { v: 'hard', t: '硬门（严格拦截）' },
]

// 默认值
const DEFAULTS = {
  name: '默认策略',
  ai_mode: 'dual',
  decision_interval: 60,
  max_position_lots: 1.0,
  max_positions: 10,
  max_daily_loss_pct: 5.0,
  max_drawdown_pct: 20.0,
  max_spread_points: 50,
  max_risk_per_trade_pct: 2.0,
  min_confidence: 0.6,
  trade_asian: true,
  trade_european: true,
  trade_american: true,
  auto_evolution: true,
  base_capital: 1000.0,
  sizing_mode: 'smart',
  volatility_factor: 1.0,
  same_direction_decay: 0.5,
  min_lot_per_trade: 0.01,
  max_lot_per_trade: 1.0,
  max_concurrent_same_direction: 3,
  open_interval_seconds: 180,
  capital_source: 'live',  // live=实时余额 / manual=用 base_capital（账户私有，不继承）
  smart_tp_enabled: true,
  tp1_atr_mult: 1.0,
  tp1_close_pct: 0.40,
  tp2_atr_mult: 1.5,
  tp2_close_pct: 0.30,
  tp3_atr_mult: 2.5,
  tp3_close_pct: 0.20,
  breakeven_after_tp1: true,
  breakeven_buffer_points: 0.5,
  trailing_atr_mult: 1.5,
  trailing_activate_after_tp2: true,
  ai_reverse_close_confidence: 0.42,
  follow_leader: true,
  reversal_confirm_cycles: 2,
  basket_tp_amount: 100.0,
  enable_l3_guard: true,
  enable_trailing_sl: true,
  // ===== 第⑤道防线·浮亏熔断（参数与盈利锁利完全独立，客户可分别调整）=====
  enable_hard_loss_cut: true,
  hard_loss_basket_amount: 50.0,
  hard_loss_per_trade_amount: 30.0,
  // ===== 决策质量门控（加法型软门，不破坏既有风控）=====
  regime_open_mode: 'soft',    // off / soft / hard（体制门）
  short_guard_mode: 'soft',    // off / soft / hard（空头约束）
}

export default function StrategyRisk() {
  const [accounts, setAccounts] = useState([])
  const [accId, setAccId] = useState('')
  const [cfg, setCfg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState('')
  const [err, setErr] = useState('')
  const [switchingPrimary, setSwitchingPrimary] = useState(false)

  const loadAccounts = useCallback(async () => {
    try {
      const list = await listAccounts()
      const arr = Array.isArray(list) ? list : []
      // 排序：在线+交易启用 优先，再按账户号降序
      arr.sort((a, b) => {
        const sa = (a.is_connected ? 2 : 0) + (a.is_trading_enabled ? 1 : 0)
        const sb = (b.is_connected ? 2 : 0) + (b.is_trading_enabled ? 1 : 0)
        if (sa !== sb) return sb - sa
        return String(b.account_id || '').localeCompare(String(a.account_id || ''))
      })
      setAccounts(arr)
      if (arr.length && !accId) setAccId(arr[0].id)
    } catch (e) { setErr(e.message || '账户加载失败') }
  }, [accId])

  useEffect(() => { loadAccounts() }, [loadAccounts])

  useEffect(() => {
    if (!accId) return
    setCfg(null); setMsg(''); setErr('')
    getStrategy(accId)
      .then((data) => setCfg({ ...DEFAULTS, ...data }))
      .catch((e) => setErr(e.message || '策略加载失败'))
  }, [accId])

  const set = (k, v) => setCfg((c) => ({ ...c, [k]: v }))
  const num = (k, v) => set(k, v === '' ? '' : Number(v))

  // 风控跟随：该号开启跟随且非主号 → 风控/平仓参数锁定（继承主号，实时生效）
  const isPrimary = accounts.find((a) => a.id === accId)?.is_market_primary

  if (!cfg) return (
    <div className="wrap"><div className="panel"><div className="h">策略风控 · AI 决策与风险管理</div><div className="empty">加载中…</div></div></div>
  )

  const locked = !!cfg.follow_leader && !isPrimary

  const save = async () => {
    if (!cfg) return
    setBusy(true); setMsg(''); setErr('')
    try {
      const payload = { ...cfg }
      if (locked) {
        // 跟随主号：仅剔除风控/平仓/熔断继承字段（运行时继承主号）
        // ★ 账号私有字段（本金/手数/AI模式/决策间隔/进化）不剔除 → 每账号独立可改
        const inheritFields = ['min_confidence', 'max_risk_per_trade_pct', 'max_positions',
          'max_position_lots', 'max_daily_loss_pct', 'max_drawdown_pct', 'max_spread_points',
          'trade_asian', 'trade_european', 'trade_american', 'open_interval_seconds',
          'smart_tp_enabled', 'tp1_atr_mult', 'tp1_close_pct', 'tp2_atr_mult', 'tp2_close_pct',
          'tp3_atr_mult', 'tp3_close_pct', 'breakeven_after_tp1', 'breakeven_buffer_points',
          'trailing_atr_mult', 'trailing_activate_after_tp2', 'ai_reverse_close_confidence',
          'reversal_confirm_cycles', 'enable_l3_guard', 'enable_trailing_sl',
          'enable_hard_loss_cut', 'hard_loss_basket_amount', 'hard_loss_per_trade_amount',
          'regime_open_mode', 'short_guard_mode']
        inheritFields.forEach((k) => delete payload[k])
      }
      await updateStrategy(accId, payload)
      setMsg(locked ? '✓ 已保存（跟随主号：风控参数实时继承，本金/手数/AI模式已独立生效）' : '✓ 策略已保存到该账号（手数自适应+智能分批止盈立即生效）')
    } catch (e) { setErr(e.message || '保存失败') }
    finally { setBusy(false) }
  }

  const handleSetPrimary = async () => {
    if (!accId || isPrimary) return
    setSwitchingPrimary(true); setMsg(''); setErr('')
    try {
      await setPrimary(accId)
      setMsg('✓ 已切换主号，页面将刷新…')
      setTimeout(() => loadAccounts(), 1200)
    } catch (e) { setErr(e.message || '切换主号失败') }
    finally { setSwitchingPrimary(false) }
  }

  return (
    <div className="wrap">
      <div className="panel">
        <div className="h">策略风控 · AI 决策与风险管理</div>
        {err && <div className="login-err" style={{ marginBottom: 10 }}>{err}</div>}

        {/* 主号领导 + 跟单 架构说明：让客户一眼看懂下单逻辑 */}
        <div className="strat-topo">
          <div className="strat-topo-ic">⚜</div>
          <div className="strat-topo-body">
            <div className="strat-topo-t">信号主号领导制 · 主号先下、跟号复制</div>
            <div className="strat-topo-s">
              带 <b>⚜ 主号</b> 标记的账号由 AI 决策并<b>先下单</b>；其余账号自动<b>复制</b>同一方向 / 同一止损止盈。
              每个账号的风控<b>完全独立</b>：手数按本账号「风控本金」算、最大持仓笔数各自封顶、同向并发各自限制。
              主号可在「账户管理」里点「设为主号」指定。
            </div>
          </div>
        </div>

        {/* ===== 风控跟随主号 ===== */}
        <div className={`follow-leader-box ${locked ? 'locked' : ''}`}>
          <div className="fl-head">
            <span className="sec-icon">🔗</span>
            <div className="fl-title">风控跟随主号</div>
            <label className="switch" style={{ marginLeft: 'auto' }}>
              <input type="checkbox" checked={!!cfg.follow_leader}
                disabled={isPrimary}
                onChange={(e) => set('follow_leader', e.target.checked)} />
              <span>{isPrimary ? '主号为风控源' : (locked ? '已跟随' : '独立配置')}</span>
            </label>
          </div>
          <div className="fl-body">
            {isPrimary
              ? '当前账号为「信号主号」，是所有跟号的风控源。修改本页参数，开启跟随的跟号会实时同步继承。'
              : (locked
                ? '✓ 本账号已开启跟随主号：下方「智能分批止盈 / 风控熔断」参数实时继承主号，无需单独设置。如需独立微调，请关闭上方开关。'
                : '关闭后，本账号使用下方独立配置（不继承主号）。开启后下方风控/平仓参数自动继承主号、实时同步。')}
            {!isPrimary && (
              <button className="mini-btn" style={{ marginTop: 8, fontSize: 12 }}
                onClick={handleSetPrimary} disabled={switchingPrimary}>
                {switchingPrimary ? '切换中…' : '⚜ 设为主号（切换信号源）'}
              </button>
            )}
          </div>
        </div>

        <div className="strat-layout">
          <div className="strat-side">
            <div className="strat-side-h">
              <span className="strat-side-title">📋 选择账号</span>
              <span className="strat-side-sub">独立配置 · 自动同步</span>
            </div>
            {accounts.length === 0 && (
              <div className="strat-empty">
                <div className="strat-empty-ic">📭</div>
                <div className="strat-empty-t">未找到可配置的账号</div>
                <div className="strat-empty-s">请先在「账户管理」添加 MT5 账号</div>
              </div>
            )}
            {accounts.map((a) => {
              const online = !!a.is_connected
              const trade = !!a.is_trading_enabled
              const pri = !!a.is_market_primary
              return (
                <button
                  key={a.id}
                  className={`strat-acc ${accId === a.id ? 'on' : ''} ${pri ? 'primary' : ''}`}
                  onClick={() => setAccId(a.id)}
                >
                  <div className="strat-acc-row1">
                    <span className={`strat-dot ${online ? 'on' : 'off'}`}></span>
                    <span className="strat-acc-name">{a.name || '未命名'}</span>
                    <span className="strat-acc-badges">
                      {pri && <span className="badge-on" style={{background:'var(--gold)',color:'#000'}}>⚜主号</span>}
                      {online && !pri && <span className="badge-on">在线</span>}
                      {trade && <span className="badge-trade">交易</span>}
                    </span>
                  </div>
                  <div className="strat-acc-row2">
                    MT5 #{a.account_id} · {a.server}
                  </div>
                  {accId === a.id && !pri && (
                    <div style={{ marginTop: 4 }}>
                      <button className="mini-btn" style={{ fontSize: 11, width: '100%' }}
                        onClick={(e) => { e.stopPropagation(); handleSetPrimary() }}
                        disabled={switchingPrimary}>
                        {switchingPrimary ? '切换中…' : '⚜ 设为主号'}
                      </button>
                    </div>
                  )}
                </button>
              )
            })}
          </div>

          <div className="strat-form">
            {/* ========== 1. 基础信息 ========== */}
            <div className="form-section">
              <div className="form-section-h">
                <span className="sec-icon">⚙</span> 基础信息
              </div>
              <div className="form-row">
                <label>策略名称</label>
                <input value={cfg.name || ''} onChange={(e) => set('name', e.target.value)} />
              </div>
              <div className="form-row">
                <label>AI 决策模式</label>
                <select value={cfg.ai_mode || 'dual'} onChange={(e) => set('ai_mode', e.target.value)}>
                  {AI_MODES.map((m) => <option key={m.v} value={m.v}>{m.t}</option>)}
                </select>
              </div>
              <div className="form-row">
                <label>决策间隔（秒）</label>
                <input type="number" value={cfg.decision_interval ?? ''} onChange={(e) => num('decision_interval', e.target.value)} />
              </div>
            </div>

            {/* ========== 2. 本金与手数自适应 ========== */}
            <div className="form-section highlight">
              <div className="form-section-h">
                <span className="sec-icon">💰</span> 本金与手数自适应 <span className="sec-tag">智能</span>
              </div>

              <div className="form-row">
                <label>该账号基础本金 ($)</label>
                <input type="number" value={cfg.base_capital ?? ''} onChange={(e) => num('base_capital', e.target.value)} />
                <div className="hint">AI 据此按比例算单笔风险金额（$1,000~$10,000 自适应）</div>
              </div>

              <div className="form-row">
                <label>手数计算模式</label>
                <select value={cfg.sizing_mode || 'smart'} onChange={(e) => set('sizing_mode', e.target.value)}>
                  {SIZING_MODES.map((m) => <option key={m.v} value={m.v}>{m.t} — {m.desc}</option>)}
                </select>
              </div>

              <div className="form-row">
                <label>手数本金来源</label>
                <select value={cfg.capital_source || 'live'} onChange={(e) => set('capital_source', e.target.value)}>
                  {CAPITAL_SOURCES.map((m) => <option key={m.v} value={m.v}>{m.t} — {m.desc}</option>)}
                </select>
                <div className="hint">选「实时余额」→ 手数按各账号真实本金×风险%自适应（根治 4 账号全开 0.01 手）；选「手动本金」→ 用上方基础本金做基准</div>
              </div>

              <div className="slider-row">
                <label>单笔最大风险 % <b style={{ color: 'var(--acc)' }}>{cfg.max_risk_per_trade_pct}</b></label>
                <input type="range" min="0.25" max="5" step="0.25" value={cfg.max_risk_per_trade_pct ?? 2.0}
                  onChange={(e) => num('max_risk_per_trade_pct', e.target.value)} />
              </div>

              <div className="slider-row">
                <label>趋势强度系数 <b style={{ color: 'var(--acc)' }}>×{cfg.volatility_factor}</b></label>
                <input type="range" min="0.3" max="2.0" step="0.1" value={cfg.volatility_factor ?? 1.0}
                  onChange={(e) => num('volatility_factor', e.target.value)} />
                <div className="hint">&gt;1 强趋势加码 · &lt;1 弱趋势缩手</div>
              </div>

              <div className="slider-row">
                <label>同向持仓衰减 <b style={{ color: 'var(--acc)' }}>×{cfg.same_direction_decay}</b></label>
                <input type="range" min="0" max="0.9" step="0.05" value={cfg.same_direction_decay ?? 0.5}
                  onChange={(e) => num('same_direction_decay', e.target.value)} />
                <div className="hint">同方向第 N 单手数 × (1-此值)^N</div>
              </div>

              <div className="form-row-grid">
                <div>
                  <label>单笔最小手数</label>
                  <input type="number" step="0.01" value={cfg.min_lot_per_trade ?? ''} onChange={(e) => num('min_lot_per_trade', e.target.value)} />
                </div>
                <div>
                  <label>单笔最大手数</label>
                  <input type="number" step="0.01" value={cfg.max_lot_per_trade ?? ''} onChange={(e) => num('max_lot_per_trade', e.target.value)} />
                </div>
                <div>
                  <label>同向最多并发</label>
                  <input type="number" min="1" max="10" value={cfg.max_concurrent_same_direction ?? ''} onChange={(e) => num('max_concurrent_same_direction', e.target.value)} />
                </div>
                <div>
                  <label>同向开仓间隔（秒）</label>
                  <input type="number" min="0" step="10" value={cfg.open_interval_seconds ?? ''} onChange={(e) => num('open_interval_seconds', e.target.value)} />
                  <div className="hint">同一方向连续开仓最小间隔，防趋势中滚动加仓</div>
                </div>
              </div>
            </div>

            {/* ========== 3. 智能分批止盈 ========== */}
            <div className={`form-section highlight ${locked ? 'locked' : ''}`}>
              <div className="form-section-h">
                <span className="sec-icon">🎯</span> 智能分批止盈 <span className="sec-tag">4 级</span>
                <label className="switch" style={{ marginLeft: 'auto' }}>
                  <input type="checkbox" checked={!!cfg.smart_tp_enabled} onChange={(e) => set('smart_tp_enabled', e.target.checked)} />
                  <span>启用</span>
                </label>
              </div>

              {/* 4 级分批可视化条 */}
              <div className={`tp-ladder ${!cfg.smart_tp_enabled ? 'off' : ''}`}>
                <div className="tp-step tp-step-1">
                  <div className="tp-pct">40%</div>
                  <div className="tp-lbl">TP1</div>
                  <div className="tp-mult">{cfg.tp1_atr_mult}×ATR</div>
                  <div className="tp-act">保本单</div>
                </div>
                <div className="tp-line"></div>
                <div className="tp-step tp-step-2">
                  <div className="tp-pct">30%</div>
                  <div className="tp-lbl">TP2</div>
                  <div className="tp-mult">{cfg.tp2_atr_mult}×ATR</div>
                  <div className="tp-act">启动追踪</div>
                </div>
                <div className="tp-line"></div>
                <div className="tp-step tp-step-3">
                  <div className="tp-pct">20%</div>
                  <div className="tp-lbl">TP3</div>
                  <div className="tp-mult">{cfg.tp3_atr_mult}×ATR</div>
                  <div className="tp-act">大盈锁定</div>
                </div>
                <div className="tp-line"></div>
                <div className="tp-step tp-step-4">
                  <div className="tp-pct">10%</div>
                  <div className="tp-lbl">TP4</div>
                  <div className="tp-mult">追踪</div>
                  <div className="tp-act">捕获趋势</div>
                </div>
              </div>

              <div className="form-row-grid">
                <div>
                  <label>TP1 ATR 倍数</label>
                  <input type="number" step="0.1" value={cfg.tp1_atr_mult ?? ''} onChange={(e) => num('tp1_atr_mult', e.target.value)} />
                </div>
                <div>
                  <label>TP2 ATR 倍数</label>
                  <input type="number" step="0.1" value={cfg.tp2_atr_mult ?? ''} onChange={(e) => num('tp2_atr_mult', e.target.value)} />
                </div>
                <div>
                  <label>TP3 ATR 倍数</label>
                  <input type="number" step="0.1" value={cfg.tp3_atr_mult ?? ''} onChange={(e) => num('tp3_atr_mult', e.target.value)} />
                </div>
              </div>

              <div className="divider-h"></div>
              <div className="form-section-sub">保本单 + 追踪止损</div>
              <div className="form-row-grid">
                <div>
                  <label className="switch">
                    <input type="checkbox" checked={!!cfg.breakeven_after_tp1} onChange={(e) => set('breakeven_after_tp1', e.target.checked)} />
                    <span>TP1 后保本</span>
                  </label>
                </div>
                <div>
                  <label>保本缓冲（点）</label>
                  <input type="number" step="0.1" value={cfg.breakeven_buffer_points ?? ''} onChange={(e) => num('breakeven_buffer_points', e.target.value)} />
                </div>
                <div>
                  <label>追踪 ATR 倍数</label>
                  <input type="number" step="0.1" value={cfg.trailing_atr_mult ?? ''} onChange={(e) => num('trailing_atr_mult', e.target.value)} />
                </div>
              </div>
              <div className="form-row">
                <label className="switch">
                  <input type="checkbox" checked={!!cfg.trailing_activate_after_tp2} onChange={(e) => set('trailing_activate_after_tp2', e.target.checked)} />
                  <span>TP2 触发后才激活追踪（推荐）</span>
                </label>
              </div>
              <div className="form-row">
                <label>AI 反向平仓置信度阈值</label>
                <input type="number" step="0.05" min="0" max="1" disabled={locked}
                  value={cfg.ai_reverse_close_confidence ?? ''} onChange={(e) => num('ai_reverse_close_confidence', e.target.value)} />
                <div className="hint">AI 反向决策 + 置信度 ≥ 此值（需连续 {cfg.reversal_confirm_cycles} 轮同向确认防抖）→ 全平反手。平仓=防守：方向翻转即止损，门槛低于开仓(0.58)</div>
              </div>

              <div className="divider-h"></div>
              <div className="form-section-sub">智能平仓增强 · L2 反转防抖 / L3 篮子护盾</div>

              <div className="form-row">
                <label className="switch">
                  <input type="checkbox" checked={!!cfg.enable_trailing_sl} disabled={locked}
                    onChange={(e) => set('enable_trailing_sl', e.target.checked)} />
                  <span>追踪止损 / 早期保本（防由赢转亏）</span>
                </label>
              </div>

              <div className="form-row-grid">
                <div>
                  <label>反转确认轮数</label>
                  <input type="number" min="1" max="5" disabled={locked}
                    value={cfg.reversal_confirm_cycles ?? ''} onChange={(e) => num('reversal_confirm_cycles', e.target.value)} />
                  <div className="hint">连续 N 轮反向同向才平仓，防黄金假反转反复洗</div>
                </div>
                <div>
                  <label>篮子浮盈阈值 ($)</label>
                  <input type="number" step="10" disabled={locked}
                    value={cfg.basket_tp_amount ?? ''} onChange={(e) => num('basket_tp_amount', e.target.value)} />
                  <div className="hint">所有持仓浮盈合计达此值 → 全平锁利重开{!isPrimary && cfg.follow_leader ? '（跟号按本金等比缩放）' : ''}</div>
                </div>
                <div>
                  <label className="switch">
                    <input type="checkbox" checked={!!cfg.enable_l3_guard} disabled={locked}
                      onChange={(e) => set('enable_l3_guard', e.target.checked)} />
                    <span>L3 篮子护盾</span>
                  </label>
                </div>
              </div>
            </div>

            {/* ========== 3.5 决策质量门控（体制门 + 空头约束·加法型软门） ========== */}
            <div className={`form-section highlight ${locked ? 'locked' : ''}`}>
              <div className="form-section-h">
                <span className="sec-icon">🚦</span> 决策质量门控 <span className="sec-tag">提准非拦截</span>
              </div>
              <div className="hint" style={{ marginBottom: 8 }}>
                加法型软门：默认「软门」只抬高低质量信号的置信门槛（提准非拦截），不砍交易笔数；
                切到「硬门」才硬性拦截（仅强趋势放开开单 / 仅体制转空+哨兵确认才放空）。关闭=完全不限制。
                跟随主号时本区继承主号设置、不可单独修改。
              </div>

              <div className="form-row">
                <label>体制门（开单趋势过滤）</label>
                <select value={cfg.regime_open_mode || 'soft'} disabled={locked} onChange={(e) => set('regime_open_mode', e.target.value)}>
                  {GATE_MODES.map((m) => <option key={m.v} value={m.v}>{m.t}</option>)}
                </select>
                <div className="hint">仅允许在「强趋势体制」放开开单。软门=弱体制抬升置信门槛；硬门=弱体制（区间/高波动）禁止开单。</div>
              </div>

              <div className="form-row">
                <label>空头约束（放空过滤）</label>
                <select value={cfg.short_guard_mode || 'soft'} disabled={locked} onChange={(e) => set('short_guard_mode', e.target.value)}>
                  {GATE_MODES.map((m) => <option key={m.v} value={m.v}>{m.t}</option>)}
                </select>
                <div className="hint">除非体制转空 + 反转哨兵确认，否则不放空。软门=非空头体制对 SELL 施加置信惩罚；硬门=仅体制转空且哨兵未判谷底才允许 SELL。</div>
              </div>
            </div>

            {/* ========== 4. 风控熔断 ========== */}
            <div className={`form-section ${locked ? 'locked' : ''}`}>
              <div className="form-section-h">
                <span className="sec-icon">🛡</span> 风控熔断
              </div>
              <div className="form-row-grid">
                <div>
                  <label>最大持仓笔数</label>
                  <input type="number" min="1" max="50" value={cfg.max_positions ?? ''} onChange={(e) => num('max_positions', e.target.value)} />
                  <div className="hint">该账号最多同时持有多少笔仓位（独立于其他账号，硬上限）</div>
                </div>
                <div>
                  <label>最大持仓手数</label>
                  <input type="number" step="0.01" value={cfg.max_position_lots ?? ''} onChange={(e) => num('max_position_lots', e.target.value)} />
                </div>
                <div>
                  <label>单日最大亏损 %</label>
                  <input type="number" step="0.1" value={cfg.max_daily_loss_pct ?? ''} onChange={(e) => num('max_daily_loss_pct', e.target.value)} />
                </div>
                <div>
                  <label>最大回撤 %</label>
                  <input type="number" step="0.1" value={cfg.max_drawdown_pct ?? ''} onChange={(e) => num('max_drawdown_pct', e.target.value)} />
                </div>
              </div>

              {/* ===== 第⑤道防线·浮亏熔断（参数独立于盈利锁利，客户可分别调整）===== */}
              <div className="form-divider">第⑤道防线 · 浮亏熔断（AI 沉默时的机械兜底）</div>
              <div className="form-row">
                <label className="switch">
                  <input type="checkbox" checked={!!cfg.enable_hard_loss_cut} disabled={locked}
                    onChange={(e) => set('enable_hard_loss_cut', e.target.checked)} />
                  <span>浮亏熔断总开关</span>
                </label>
                <div className="hint">
                  开启后，下面两条「浮亏止损规则」才会生效。即便 AI 没发出任何平仓信号，
                  系统也会靠机械规则兜住亏损，防止小亏拖成大亏。关闭则完全交给 AI 决策。
                </div>
              </div>
              <div className="form-row-grid">
                <div>
                  <label>篮子浮亏熔断阈值 ($)</label>
                  <input type="number" step="5" min="0" disabled={locked}
                    value={cfg.hard_loss_basket_amount ?? ''} onChange={(e) => num('hard_loss_basket_amount', e.target.value)} />
                  <div className="hint">
                    该账号<b>所有持仓浮亏合计</b> ≤ -此金额（即亏这么多刀）→ 立刻把全部持仓平仓止损。
                    <b>完全独立于「篮子盈利锁利阈值」，两者可以设成不同数字。</b>
                    例：设为 50 表示总亏 50 刀就全部清仓。
                  </div>
                </div>
                <div>
                  <label>单笔浮亏熔断阈值 ($)</label>
                  <input type="number" step="5" min="0" disabled={locked}
                    value={cfg.hard_loss_per_trade_amount ?? ''} onChange={(e) => num('hard_loss_per_trade_amount', e.target.value)} />
                  <div className="hint">
                    任意<b>一笔</b>持仓浮亏 ≤ -此金额 → 立刻平掉这一笔（其余不动）。
                    专门防止单笔订单无限死扛。与「篮子浮亏阈值」分开设置，
                    例：设为 30 表示某一单亏 30 刀就单独砍掉它。
                  </div>
                </div>
                <div>
                  <label className="switch" style={{ opacity: 0.5 }}>
                    <input type="checkbox" checked readOnly disabled />
                    <span>与盈利锁利分开</span>
                  </label>
                  <div className="hint">
                    本区块的浮亏阈值与「智能平仓增强」里的「篮子盈利锁利阈值」是<b>两套独立参数</b>，
                    互不影响。你想锁利设多少、想止损设多少，各自调各自的。
                  </div>
                </div>
              </div>
              <div className="form-row-grid">
                <div>
                  <label>最大点差（点）</label>
                  <input type="number" value={cfg.max_spread_points ?? ''} onChange={(e) => num('max_spread_points', e.target.value)} />
                </div>
                <div>
                  <label>最小置信度</label>
                  <input type="number" step="0.01" min="0" max="1" value={cfg.min_confidence ?? ''} onChange={(e) => num('min_confidence', e.target.value)} />
                </div>
                <div></div>
              </div>
              <div className="form-divider">交易时段</div>
              <div className="toggles">
                {[['trade_asian', '亚盘'], ['trade_european', '欧盘'], ['trade_american', '美盘']].map(([k, t]) => (
                  <label key={k} className="switch">
                    <input type="checkbox" checked={!!cfg[k]} onChange={(e) => set(k, e.target.checked)} />
                    <span>{t}</span>
                  </label>
                ))}
                <label className="switch">
                  <input type="checkbox" checked={!!cfg.auto_evolution} onChange={(e) => set('auto_evolution', e.target.checked)} />
                  <span>策略自进化</span>
                </label>
              </div>
            </div>

            <div className="form-actions sticky">
              {msg && <span className="ok-msg">{msg}</span>}
              {err && <span className="login-err" style={{ marginRight: 'auto' }}>{err}</span>}
              <button className="mini-btn primary" onClick={save} disabled={busy}>
                {busy ? '保存中…' : '保存策略 · 立即生效'}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
