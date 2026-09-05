import { fmtMoney, fmtNum, holding } from '../utils/format'

// 哈希 login → 0~360 色相（同账号永远同色，4 个账户天然区分）
function hue(login) {
  const s = String(login || '')
  let h = 0
  for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) & 0xffffffff
  return Math.abs(h) % 360
}

// MT5 数字账号加空格分段（6位/7位/9位）便于客户一眼看清
function fmtLogin(login) {
  const s = String(login || '')
  if (s.length <= 4) return s
  if (s.length <= 7) return s.slice(0, 3) + ' ' + s.slice(3)
  return s.slice(0, 3) + ' ' + s.slice(3, 6) + ' ' + s.slice(6)
}

// 友好截断服务器名（去掉 `-Demo` 等冗余）
function fmtServer(srv) {
  if (!srv) return ''
  return String(srv).replace(/-Demo$/i, '').replace(/-Live$/i, '')
}

// 账户网格：1~N 弹性 + MT5 账号显著标识 + 每账户独特配色
export default function AccountGrid({ data }) {
  if (!data) return <div className="panel"><div className="h">账户网格（1~N 弹性）</div>加载中…</div>
  const p = data.portfolio || {}
  const accounts = data.accounts || []
  const totalEquity = accounts.reduce((s, a) => s + (a.equity || a.balance || 0), 0) || 1
  return (
    <div className="panel">
      <div className="h">
        实时 MT5 账户拓扑
        <span className={`live ${data.__mock ? 'mock' : ''}`}>{data.__mock ? '降级' : '实时'}</span>
        <span className="panel-h-sub">1~N 弹性网格 · 头像+MT5账号固定标识 · 每账户独特色相</span>
      </div>
      <div className="grid">
        {accounts.map((a) => {
          const positions = a.positions || []
          const h = hue(a.login)
          const share = ((a.balance || 0) / totalEquity * 100)
          const shareTxt = share >= 10 ? share.toFixed(1) : share.toFixed(2)
          const posHtml = positions.length
            ? positions.map((po, i, arr) => (
                <div className="posrow" key={i} style={i === arr.length - 1 ? { borderBottom: 'none' } : undefined}>
                  <span className="tk">{po.type === 'buy' ? 'BUY' : 'SELL'} {po.volume} #{po.ticket}</span>
                  <span className="ht">持仓 {holding(po.holding_minutes)}</span>
                  <span className={`pn ${po.profit >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(po.profit)}</span>
                </div>
              ))
            : <div className="empty">当前无持仓</div>
          return (
            <div className={`acct ${a.is_primary ? 'primary' : ''}`} key={a.id} style={{ '--accent': h }}>
              <div className="acct-bar"></div>
              {a.is_primary && <div className="acct-crown" title="主号">⚜</div>}
              <div className="hd">
                <div className="acct-avt" title={`MT5 账号 #${a.login}`}>
                  <span>{String(a.login).slice(-2)}</span>
                </div>
                <div className="acct-info">
                  <div className="acct-name-row">
                    <span className="nm">{a.name}</span>
                    {a.is_primary && <span className="badge b-pri">主号</span>}
                  </div>
                  <div className="acct-meta">
                    <span className="mt5-tag">MT5</span>
                    <span className="login">#{fmtLogin(a.login)}</span>
                    <span className="srv-dot">·</span>
                    <span className="srv">{fmtServer(a.server)}</span>
                  </div>
                </div>
                <div className="acct-flags">
                  <span className={`badge ${a.is_connected ? 'b-on' : 'b-off'}`}>
                    <span className={`dot ${a.is_connected ? 'on' : 'off'}`}></span>
                    {a.is_connected ? '在线' : '离线'}
                  </span>
                  <span className={`badge ${a.is_trading ? 'b-tr' : 'b-off'}`}>
                    {a.is_trading ? '交易✓' : '暂停'}
                  </span>
                </div>
              </div>
              <div className="bal">{fmtNum(a.balance)}</div>
              <div className="acct-share">
                <div className="share-bar"><div className="share-fill" style={{ width: share + '%' }}></div></div>
                <div className="share-label">占组合 {shareTxt}%</div>
              </div>
              <div className="metrics">
                <div className="mt"><div className={`v ${a.today_profit >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(a.today_profit)}</div><div className="k">今日盈利</div></div>
                <div className="mt"><div className={`v ${a.hist_profit >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(a.hist_profit)}</div><div className="k">历史盈利</div></div>
                <div className="mt"><div className={`v ${(a.float_pnl || 0) >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(a.float_pnl || 0)}</div><div className="k">当前浮盈</div></div>
                <div className="mt"><div className="v">{a.today_orders}</div><div className="k">今日订单</div></div>
                <div className="mt"><div className="v">{a.hist_orders}</div><div className="k">历史订单</div></div>
              </div>
              <div className="poslist">{posHtml}</div>
            </div>
          )
        })}
      </div>
      <div className="acct-footer">
        <div className="aft-stat">账号数 <b>{p.account_count}</b></div>
        <div className="aft-sep"></div>
        <div className="aft-stat">在线 <b className="pos">{p.online}</b></div>
        <div className="aft-sep"></div>
        <div className="aft-stat">交易中 <b className="pos">{p.trading}</b></div>
        <div className="aft-sep"></div>
        <div className="aft-stat">总持仓 <b>{p.total_positions}</b></div>
        <div className="aft-sep"></div>
        <div className="aft-stat">组合权益 <b className="hi">{fmtMoney(totalEquity)}</b></div>
      </div>
    </div>
  )
}
