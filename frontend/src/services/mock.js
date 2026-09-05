// 降级模拟数据（后端未连时前端仍能完整展示布局）
export function mockSession() {
  return {
    server_time: '2026-08-02 22:25:00',
    timezone: 'GMT+3 (夏令时/DST)',
    broker: 'STARTRADER',
    is_open: false,
    state: 'weekend',
    label: '周末休市',
    countdown_sec: 47 * 3600 + 35 * 60,
    countdown_label: '距开市 47:35:00',
    next_event: '开市',
    source: 'mock',
  }
}

export function mockChart(tf) {
  const bars = []
  let p = 4000 + Math.random() * 50
  const n = 120
  for (let i = 0; i < n; i++) {
    const o = p
    const c = o + (Math.random() - 0.48) * 12
    const h = Math.max(o, c) + Math.random() * 6
    const l = Math.min(o, c) - Math.random() * 6
    bars.push({ open: o, high: h, low: l, close: c, volume: Math.round(200 + Math.random() * 800), time: '' })
    p = c
  }
  return {
    symbol: 'XAUUSD',
    tf,
    bars,
    current: { bid: bars[n - 1].close - 0.3, ask: bars[n - 1].close + 0.3, spread: 18 },
    server_time: '',
    macro: {
      dxy: { price: 104.35, change_pct: 0.12 },
      vix: { price: 18.5, regime: 'normal' },
      correlation: { correlation_20d: -0.85, signal: 'dxy_down_gold_up' },
    },
    trend: '偏多（趋势可信）· 美元走弱利好黄金',
  }
}

export function mockAccounts() {
  const names = ['星迈主号', '星迈副号A', '星迈副号B', '模拟盘C']
  return {
    portfolio: {
      account_count: 4, online: 4, trading: 3, total_balance: 1003580.12,
      total_equity: 1008720.33, today_profit: 3240.5, hist_profit: 58300.4, total_positions: 7,
    },
    accounts: names.map((nm, i) => ({
      id: 'a' + i, name: nm, login: '161009' + (3000 + i), server: 'STARTRADER',
      is_primary: i === 0, is_connected: true, is_trading: i !== 3,
      balance: 250000 + i * 3000, equity: 251000 + i * 3200, margin_level: 320 + i * 10,
      today_profit: 800 + i * 120, hist_profit: 14000 + i * 1100,
      today_orders: 8 + i, hist_orders: 120 + i * 30,
      position_count: i % 2 ? 2 : 1,
      positions: Array.from({ length: i % 2 ? 2 : 1 }, (_, j) => ({
        ticket: 90000 + i * 10 + j, type: j % 2 ? 'sell' : 'buy', volume: 0.04,
        open_price: 4000 + Math.random() * 40, current_price: 4010 + Math.random() * 20,
        sl: 3990, tp: 4030, profit: (Math.random() - 0.3) * 60, swap: 0,
        open_time: new Date(Date.now() - (i * 37 + j * 12) * 60000).toISOString(),
        holding_minutes: i * 37 + j * 12,
      })),
    })),
  }
}

// 纯前端指标（mock 用；真实场景由后端 indicators 返回）
export function computeInd(bars) {
  if (!bars || bars.length < 20) return {}
  const c = bars.map((b) => b.close)
  let e = c[0]
  const ema = [e]
  for (let i = 1; i < c.length; i++) {
    e = c[i] * (2 / 21) + e * (19 / 21)
    ema.push(e)
  }
  const last = ema[ema.length - 1]
  const prev = ema[ema.length - 3]
  return {
    ema20: last,
    ema20_dir: last > prev ? '↑' : last < prev ? '↓' : '→',
    rsi: 55 + Math.random() * 15,
    adx: 25 + Math.random() * 10,
    trend: '偏多（趋势可信）',
  }
}
