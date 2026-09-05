// 风控事件流 —— 回答客户那个最伤信任的问题：「AI 明明喊了买，为什么没下单？」
//
// 这个面板存在的理由不是"多一个功能"，而是补一个认知缺口：
// 在它出现之前，拦截原因只存在于后端日志里。客户看到的现象是
// 「屏幕上 AI 在分析、在给方向，但一整晚一单没开」——
// 在客户的心智里，「没交易」和「系统挂了」是同一件事。
//
// 两个刻意的设计：
//   1) 空态说的是「系统在正常放行」，不是「暂无数据」。
//      空列表在这里是好消息，绝不能长得像故障。
//   2) 原因排行放在最左边、比时间线更显眼。
//      单条事件只能解释某一瞬间；只有聚合才能暴露"这个账号 80% 的拦截
//      都来自单笔风险超限"这类配置问题——那不是 AI 不干活，是参数配得太紧。
import { useEffect, useState } from 'react'
import { fetchRiskEvents } from '../services/api'
import { useModelReadiness } from '../brand/modelReadiness'

// 降级档位 → 视觉。与后端 platform_health_monitor 的 L0~L3 一一对应。
const DEGRADE_META = {
  L0: { color: 'var(--green)', label: '全速运行', desc: '云端双脑在线，全部能力可用' },
  L1: { color: 'var(--blue)', label: '轻度降级', desc: '单侧云模型异常，另一侧接管' },
  L2: { color: 'var(--gold)', label: '重度降级', desc: '云端不可用，本地接管，手数已下调' },
  L3: { color: 'var(--red)', label: '熔断中', desc: '已停发新开仓；持仓仍由止损/智能平仓守护' },
}

// ★ 审计修复(2026-08-08)：L2 的描述原文是「本地副驾接管」——写死的。
//
// 问题在于那不是一句描述，那是一句**承诺**。客户在最紧张的时刻
// （云端刚失联）看到"本地副驾接管"，会理解成"还有人在替我守着"。
// 可如果客户那台机器压根没装 Ollama，L2 的真实状态是：
// 没有任何 LLM 在岗，只剩 Chronos 时序 + SMC 规则在硬撑。
//
// 把"没人接管"显示成"副驾接管"，比不显示危险得多——
// 它会让运维**放弃本该立刻做的人工介入**。
// 所以 L2 文案必须读运行时就绪度，实况说什么就写什么。
function degradeDesc(name, rd) {
  if (name !== 'L2') return DEGRADE_META[name]?.desc || ''
  if (rd?.qwen) {
    return '云端双脑失联 → 本地副驾 Qwen3-8B 接管出票（已叠加 Chronos 同向校验 + 手数下调三道锁）'
  }
  if (rd?.chronos) {
    return '云端双脑失联 → 本地副驾未就绪，当前仅 Chronos 时序 + SMC 规则托底，建议人工盯盘'
  }
  return '云端双脑失联 → 本地双核均未就绪，仅剩 SMC 规则托底，强烈建议立即人工介入'
}

// 本地托底阵容的就绪灯。只在降级档位（非 L0）出现——
// L0 时云端双脑健在，本地是否在岗对客户不构成决策信息，摆出来只是噪音。
function LocalBackupLamps({ rd }) {
  const items = [
    { on: rd?.qwen, label: 'Qwen3-8B', role: '降级副驾' },
    { on: rd?.chronos, label: 'Chronos-2', role: '时序托底' },
  ]
  return (
    <span className="rk-backup" title="降级时的本地托底阵容">
      <span className="rk-backup-t">本地托底</span>
      {items.map((it) => (
        <span
          key={it.label}
          className="rk-backup-i"
          style={{ color: it.on ? 'var(--green)' : 'var(--dim)' }}
          title={`${it.label} · ${it.role} · ${it.on ? '在岗' : '未就绪'}`}
        >
          <span
            className="rk-backup-dot"
            style={{ background: it.on ? 'var(--green)' : 'var(--dim)' }}
          />
          {it.label}
        </span>
      ))}
    </span>
  )
}

// 事件来源 → 颜色。让客户一眼分清"是风控规则挡的"还是"平台熔断挡的"，
// 这两件事的处理方式完全不同（前者调参数，后者等恢复）。
const STAGE_META = {
  risk_engine: { color: 'var(--gold)', label: '风控' },
  risk_engine_follower: { color: 'var(--purple)', label: '跟号风控' },
  executor: { color: 'var(--blue)', label: '执行节流' },
  degrade_gate: { color: 'var(--red)', label: '平台熔断' },
}

const fmtClock = (iso) => {
  if (!iso) return '--:--'
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return '--:--'
    // ★ 2026-08-10 时区修复：原 'zh-CN' locale 默认 UTC 时区，
    //   后端虽然已发带 +00:00 的 ISO，但 toLocaleTimeString 不带 timeZone
    //   仍按浏览器本地时区展示。统一锁 Asia/Shanghai，与后端 ts 配套。
    return d.toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' }).slice(0, 5)
  } catch {
    return '--:--'
  }
}

function DegradeBadge({ degrade, rd }) {
  const name = String(degrade?.level_name || 'L0').toUpperCase()
  const meta = DEGRADE_META[name] || DEGRADE_META.L0
  const mult = degrade?.lot_multiplier
  const desc = degradeDesc(name, rd)
  // L2 且本地副驾缺位 —— 这不是普通降级，是"降级预案本身也失效了"。
  // 用红色而非档位原本的金色，因为此刻的真实风险等级高于 L2 的名义等级。
  const risky = name === 'L2' && !rd?.qwen
  const color = risky ? 'var(--red)' : meta.color
  return (
    <span
      className="rk-degrade"
      style={{ color, borderColor: color }}
      title={`${desc}${degrade?.reason ? ' · ' + degrade.reason : ''}`}
    >
      <span className="rk-degrade-dot" style={{ background: color }} />
      {name} {meta.label}
      {risky && <span className="rk-degrade-warn">副驾缺位</span>}
      {typeof mult === 'number' && mult < 1 && (
        <span className="rk-degrade-mult">手数 {(mult * 100).toFixed(0)}%</span>
      )}
    </span>
  )
}

export default function RiskEventPanel({ degrade }) {
  const rd = useModelReadiness()
  const [events, setEvents] = useState([])
  const [top, setTop] = useState([])
  const [err, setErr] = useState('')
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const data = await fetchRiskEvents('', 40)
        if (!alive) return
        setEvents(Array.isArray(data?.events) ? data.events : [])
        setTop(Array.isArray(data?.top_reasons) ? data.top_reasons : [])
        setErr('')
      } catch (e) {
        if (!alive) return
        setErr(e?.message || '风控事件获取失败')
      } finally {
        if (alive) setLoaded(true)
      }
    }
    let timer = null
    const tick = async () => {
      await load()
      if (alive) timer = setTimeout(tick, 15000)
    }
    tick()
    // 15s 一轮：拦截事件不是行情，不需要秒级刷新。
    // 刻意不搭 ai-flow 那趟 3s 的车 —— 那条链路已被两个组件重复轮询。
    return () => { alive = false; if (timer) clearTimeout(timer) }
  }, [])

  const maxCount = top.length ? Math.max(...top.map((r) => r.count || 0)) : 0

  return (
    <div className="panel rk-panel">
      <div className="h">
        风控事件流
        <span className="rk-sub">为什么没开单 · 每一次拦截都留痕</span>
        <DegradeBadge degrade={degrade} rd={rd} />
        {String(degrade?.level_name || 'L0').toUpperCase() !== 'L0' && (
          <LocalBackupLamps rd={rd} />
        )}
      </div>

      {err && <div className="rk-err">数据获取失败：{err}</div>}

      {/* 降级中把托底说明摊开写。徽标的 title 需要悬停才看得见，
          而降级时段恰恰是运维最需要"一眼看懂现在靠什么在撑"的时候。 */}
      {String(degrade?.level_name || 'L0').toUpperCase() !== 'L0' && (
        <div
          className="rk-degrade-note"
          style={{
            borderLeftColor:
              String(degrade?.level_name).toUpperCase() === 'L2' && !rd.qwen
                ? 'var(--red)'
                : 'var(--gold)',
          }}
        >
          {degradeDesc(String(degrade?.level_name || 'L0').toUpperCase(), rd)}
        </div>
      )}

      <div className="rk-body">
        {/* 左：原因排行 */}
        <div className="rk-rank">
          <div className="rk-col-h">拦截原因排行（近 {events.length} 条）</div>
          {top.length === 0 && (
            <div className="rk-empty">
              {loaded ? '近期没有信号被拦截 —— 风控在正常放行' : '加载中…'}
            </div>
          )}
          {top.map((r) => (
            <div key={r.code} className="rk-rank-row">
              <div className="rk-rank-label" title={r.code}>{r.label}</div>
              <div className="rk-rank-bar">
                <div
                  className="rk-rank-fill"
                  style={{ width: `${maxCount ? (r.count / maxCount) * 100 : 0}%` }}
                />
              </div>
              <div className="rk-rank-num">{r.count}</div>
            </div>
          ))}
        </div>

        {/* 右：事件时间线 */}
        <div className="rk-list">
          <div className="rk-col-h">最近拦截明细</div>
          {events.length === 0 && (
            <div className="rk-empty rk-empty-ok">
              {loaded
                ? '✓ 近期没有被拦截的信号 —— 系统在正常放行，这是好消息'
                : '加载中…'}
            </div>
          )}
          {events.map((e) => {
            const sm = STAGE_META[e.stage] || { color: 'var(--dim)', label: e.stage || '其他' }
            return (
              <div key={e.id} className="rk-item" style={{ borderLeftColor: sm.color }}>
                <div className="rk-item-h">
                  <span className="rk-tm">{fmtClock(e.ts)}</span>
                  <span className="rk-stage" style={{ color: sm.color, borderColor: sm.color }}>
                    {sm.label}
                  </span>
                  {(e.labels || []).slice(0, 2).map((lb, i) => (
                    <span key={i} className="tagx t-risk">{lb}</span>
                  ))}
                  {e.direction && (
                    <span className={`tagx ${e.direction === 'BUY' ? 't-buy' : 't-sell'}`}>
                      {e.direction}
                    </span>
                  )}
                  {e.degrade_level && e.degrade_level !== 'L0' && (
                    <span className="tagx t-loss">{e.degrade_level}</span>
                  )}
                  {typeof e.intended_lots === 'number' && (
                    <span className="rk-lots">拟开 {e.intended_lots} 手</span>
                  )}
                </div>
                <div className="rk-reason">{e.reasons || '（无描述）'}</div>
              </div>
            )
          })}
        </div>
      </div>
    </div>
  )
}
