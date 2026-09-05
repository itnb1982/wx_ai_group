import { fmtMoney, fmtNum } from '../utils/format'

// 顶部资金总览 KPI
export default function KpiBar({ portfolio }) {
  const p = portfolio || {}
  return (
    <div className="panel">
      <div className="h">资金总览 <span className="live">实时</span></div>
      <div className="kpis">
        <div className="kpi">
          <div className="v">{fmtNum(p.total_balance)}</div>
          <div className="l">总权益</div>
        </div>
        <div className="kpi">
          <div className="v">{fmtNum(p.total_equity)}</div>
          <div className="l">总净值</div>
        </div>
        <div className="kpi">
          <div className={`v ${p.today_profit >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(p.today_profit)}</div>
          <div className="l">今日盈利</div>
        </div>
        <div className="kpi">
          <div className={`v ${p.hist_profit >= 0 ? 'pos' : 'neg'}`}>{fmtMoney(p.hist_profit)}</div>
          <div className="l">历史总盈利</div>
        </div>
      </div>
      <div style={{ fontSize: 11, color: 'var(--sub)', marginTop: 8 }}>
        账号数：{p.account_count} · 在线：{p.online} · 已启用：{p.trading} · 总持仓：{p.total_positions}
      </div>
    </div>
  )
}
