// 多模型信号源参考面板 —— fusion_v2 模式下 4 时序模型聚合为融合票，
// 接入 meta_agent 决策链作为第三票方向源（权重 0.22），仍有三道安全门
// （合成行情禁用 / snapshot 过期 / 可用模型<2 回退单 Chronos）。
//
// ★ 红线（与后端 ts_reference_service 一致）：本页所有信号只用于
//   「开盘对照实时行情，看模型准不准」，绝不参与交易。页面不持有任何
//   下单/风控入口，也不把信号回传决策链。
import { useEffect, useRef, useState } from 'react'
import { fetchTsReference, selftestTsReferenceModel, visionStatus } from '../services/api'
import VisionModelPanel from './VisionModelPanel'

const num = { fontFamily: 'var(--font-num)' }

// 方向 → 颜色 / 文案（中国习惯：红涨绿跌）
function dirStyle(dir) {
  if (dir === 'BUY') return { color: 'var(--red)', label: '看多 ↑' }
  if (dir === 'SELL') return { color: 'var(--green)', label: '看空 ↓' }
  if (dir === 'HOLD') return { color: 'var(--sub)', label: '观望 —' }
  if (dir === 'N/A') return { color: 'var(--sub)', label: '待安装' }
  return { color: 'var(--gold)', label: '异常' }
}

// ─────────────────────────────────────────────────────────────
// 融合票面板（fusion_v2 第四票）：把 4 时序模型怎么加权聚合成一道方向，
// 用「实时权重条 + 算式」透明呈现，让客户看懂本地算力如何参与决策。
// ─────────────────────────────────────────────────────────────
function FusionPanel({ fv }) {
  if (!fv) return null
  const ds = dirStyle(fv.direction)
  const score = fv.score || 0
  const posPct = Math.round((score + 1) * 50)
  return (
    <div style={{
      marginTop: 12, padding: '12px 14px', borderRadius: 8,
      background: 'linear-gradient(135deg, rgba(46,230,160,.06), rgba(80,160,255,.06))',
      border: '1px solid rgba(120,200,255,.25)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12.5, color: 'var(--sub)', fontWeight: 700 }}>融合票（第四票 · 权重 0.22）</span>
        <span style={{
          fontSize: 17, fontWeight: 900,
          color: fv.available ? ds.color : 'var(--sub)',
        }}>{fv.available ? ds.label : '融合票不可用'}</span>
        {fv.available && (
          <span style={{ fontSize: 11, color: 'var(--sub)', fontFamily: 'var(--font-num)' }}>
            强度 {score > 0 ? '+' : ''}{score.toFixed(2)} · 置信 {(fv.confidence || 0).toFixed(2)} · 系数 ×{fv.weight_scale || 1} · {fv.model_count} 模型
          </span>
        )}
        <span style={{
          marginLeft: 'auto', fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
          color: fv.agree ? '#2ee6a0' : '#ffcf4d',
          background: fv.agree ? 'rgba(46,230,160,.12)' : 'rgba(255,207,77,.12)',
          border: `1px solid ${fv.agree ? 'rgba(46,230,160,.32)' : 'rgba(255,207,77,.32)'}`,
        }}>{fv.agree ? '模型同向' : '模型分歧'}</span>
      </div>

      {fv.available && (
        <div style={{ marginTop: 10, position: 'relative', height: 8, background: 'var(--line)', borderRadius: 4 }}>
          <div style={{ position: 'absolute', left: '50%', top: -3, bottom: -3, width: 1, background: 'var(--dim)' }} />
          <div style={{
            position: 'absolute', top: 0, bottom: 0,
            left: score >= 0 ? '50%' : `${posPct}%`,
            width: `${Math.abs(score) * 50}%`,
            background: score >= 0 ? 'var(--red)' : 'var(--green)', borderRadius: 4,
          }} />
        </div>
      )}

      {fv.note && <div style={{ marginTop: 8, fontSize: 11, color: 'var(--sub)', lineHeight: 1.5 }}>{fv.note}</div>}

      {/* 各模型融合权重明细（实时权重条） */}
      {fv.available && fv.per_model && fv.per_model.length > 0 && (
        <div style={{ marginTop: 10, display: 'flex', flexDirection: 'column', gap: 5 }}>
          <div style={{ fontSize: 10.5, color: 'var(--sub)' }}>各模型融合权重（按近期命中率加权）</div>
          {fv.per_model.map((pm, i) => {
            const pds = dirStyle(pm.direction)
            return (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 11 }}>
                <span style={{ width: 120, color: 'var(--txt)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{pm.name}</span>
                <span style={{ ...pds, fontWeight: 700, width: 56 }}>{pds.label}</span>
                <div style={{ flex: 1, height: 5, background: 'var(--line)', borderRadius: 3, overflow: 'hidden' }}>
                  <div style={{ width: `${Math.round((pm.qw || 0) * 100)}%`, height: '100%', background: 'var(--blue)' }} />
                </div>
                <span style={{ fontFamily: 'var(--font-num)', color: 'var(--sub)', width: 44, textAlign: 'right' }}>qw {(pm.qw || 0).toFixed(2)}</span>
              </div>
            )
          })}
        </div>
      )}

      <div style={{ marginTop: 10, fontSize: 10.5, color: 'var(--dim)', lineHeight: 1.5 }}>
        算式：方向强度 = Σ(方向 × 质量权重 × 置信) / Σ(质量权重 × 置信)，阈值 ±0.30 判 BUY/SELL。
        仅 1 个模型可用时回退单 Chronos；合成行情 / snapshot 过期则融合票作废。
      </div>
    </div>
  )
}

function ModelCard({ m }) {
  const ds = dirStyle(m.direction)
  const acc = m.color || 'var(--blue)'
  const hit = m.hit_rate == null ? '—' : (m.hit_rate * 100).toFixed(0) + '%'
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)

  const handleTest = async () => {
    setTesting(true)
    setTestResult(null)
    try {
      const r = await selftestTsReferenceModel(m.name)
      setTestResult(r)
    } catch (e) {
      setTestResult({ available: false, error: e.message || '请求失败' })
    } finally {
      setTesting(false)
    }
  }

  return (
    <div className="panel" style={{
      borderTop: `2px solid ${acc}`, padding: 14,
      display: 'flex', flexDirection: 'column',
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontWeight: 700, color: acc, fontSize: 14 }}>{m.name}</span>
        <span style={{ ...num, fontWeight: 800, fontSize: 15, color: ds.color }}>{ds.label}</span>
      </div>

      {m.available ? (
        <>
          {/* 置信度条 */}
          <div style={{ marginTop: 10, fontSize: 11, color: 'var(--sub)' }}>置信度</div>
          <div style={{ height: 6, background: 'var(--line)', borderRadius: 4, marginTop: 3, overflow: 'hidden' }}>
            <div style={{ width: `${Math.round((m.confidence || 0) * 100)}%`, height: '100%', background: acc }} />
          </div>
          <div style={{ ...num, marginTop: 2, fontSize: 11, color: 'var(--sub)', textAlign: 'right' }}>
            {(m.confidence || 0).toFixed(2)}
          </div>

          {/* 预测目标价 + 区间 */}
          <div style={{ marginTop: 10, display: 'flex', gap: 14, fontSize: 12 }}>
            <div>
              <div style={{ color: 'var(--sub)' }}>预测末端价</div>
              <div style={{ ...num, color: 'var(--txt)', fontSize: 15, fontWeight: 700 }}>
                {m.pred_end != null ? m.pred_end.toFixed(2) : '—'}
              </div>
            </div>
            {m.lo != null && m.hi != null && (
              <div>
                <div style={{ color: 'var(--sub)' }}>预测区间</div>
                <div style={{ ...num, color: 'var(--sub)', fontSize: 13 }}>
                  {m.lo.toFixed(2)} ~ {m.hi.toFixed(2)}
                </div>
              </div>
            )}
          </div>

          {/* 滚动命中率（用户对照实盘看准不准的核心指标） */}
          <div style={{ marginTop: 10, fontSize: 11, color: 'var(--sub)', borderTop: '1px solid var(--line)', paddingTop: 8 }}>
            近 {m.hit_window ?? '—'} 次刷新命中
            <span style={{ ...num, color: acc, fontWeight: 700 }}>
              {' '}{hit} ({m.hits ?? 0}/{m.hit_window ?? '—'})
            </span>
          </div>
        </>
      ) : (
        <div style={{ marginTop: 12, fontSize: 12, color: 'var(--gold)', flex: 1 }}>
          {m.direction === 'ERROR' ? '运行时异常：' + (m.error || '') : (m.error || '未运行 / 未安装')}
        </div>
      )}

      {/* 自检按钮 + 结果 */}
      <div style={{ marginTop: 'auto', paddingTop: 12 }}>
        <button
          onClick={handleTest}
          disabled={testing}
          style={{
            width: '100%', padding: '8px 0', fontSize: 13, fontWeight: 600,
            border: `1px solid ${testing ? 'var(--line)' : acc}`, borderRadius: 6,
            background: testing ? 'var(--panel2)' : 'transparent',
            color: testing ? 'var(--dim)' : acc,
            cursor: testing ? 'not-allowed' : 'pointer',
            transition: 'all .15s ease',
          }}
          onMouseEnter={(e) => {
            if (!testing) {
              e.currentTarget.style.background = acc
              e.currentTarget.style.color = '#0b0c0f'
            }
          }}
          onMouseLeave={(e) => {
            if (!testing) {
              e.currentTarget.style.background = 'transparent'
              e.currentTarget.style.color = acc
            }
          }}
        >
          {testing ? '检测中…' : '检测'}
        </button>
        {testResult && (
          <div style={{
            marginTop: 8, fontSize: 11, lineHeight: 1.5,
            color: testResult.available ? 'var(--green)' : 'var(--gold)',
          }}>
            {testResult.available
              ? `✓ 正常 · ${testResult.latency_ms}ms · ${testResult.direction || ''} ${testResult.pred_end != null ? testResult.pred_end.toFixed(2) : ''}`
              : `✗ 异常 · ${testResult.error || '未知错误'}`}
          </div>
        )}
      </div>
    </div>
  )
}

export default function SignalReference() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [stale, setStale] = useState(false)
  const [vision, setVision] = useState(null)
  const alive = useRef(true)

  useEffect(() => {
    alive.current = true
    let timer = null
    const load = async () => {
      try {
        // 红线：行情/信号请求失败绝不回退 mock，保留上次真实值 + 标"中断重试"
        // 同时并发展示视觉第四票（第5路增强），两者互不阻塞
        const [dRes, vRes] = await Promise.allSettled([fetchTsReference(), visionStatus()])
        if (!alive.current) return
        if (dRes.status === 'fulfilled') {
          setData(dRes.value); setErr(''); setStale(false)
        } else {
          setStale(true); setErr('请求失败，保留上次数据，正在重试')
        }
        if (vRes.status === 'fulfilled') setVision(vRes.value)
      } finally {
        if (alive.current) timer = setTimeout(load, 5000)
      }
    }
    load()
    return () => { alive.current = false; if (timer) clearTimeout(timer) }
  }, [])

  const updatedAgo = data && data.updated_at
    ? Math.max(0, Math.round(Date.now() / 1000 - data.updated_at)) + 's 前'
    : '—'
  const ds = data ? dirStyle('HOLD') : null

  return (
    <div style={{ padding: 16, width: '100%', boxSizing: 'border-box' }}>
      {/* 标题 + 红线徽章 */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        <h2 style={{ margin: 0, fontSize: 20, color: 'var(--txt)' }}>多模型信号源参考面板</h2>
        <span style={{
          border: '1px solid var(--green)', color: 'var(--green)',
          borderRadius: 999, padding: '3px 12px', fontSize: 12, fontWeight: 700,
        }}>✓ fusion_v2 · 4时序融合票(权重0.22)</span>
        {stale && <span style={{ color: 'var(--gold)', fontSize: 12 }}>{err}</span>}
      </div>

      {/* 实时价 + 状态行 */}
      <div className="panel" style={{ marginTop: 14, display: 'flex', alignItems: 'center', gap: 24, flexWrap: 'wrap' }}>
        <div>
          <div style={{ fontSize: 11, color: 'var(--sub)' }}>实时 {data?.symbol || 'XAUUSD'} ({data?.tf || 'H1'})</div>
          <div style={{ ...num, fontSize: 26, fontWeight: 800, color: 'var(--gold)' }}>
            {data?.last_price != null ? data.last_price.toFixed(2) : '—'}
          </div>
        </div>
        <div style={{ display: 'flex', gap: 18, fontSize: 12, color: 'var(--sub)' }}>
          <span>状态：<b style={{ color: data?.live ? 'var(--green)' : 'var(--sub)' }}>{data?.live ? '实时行情' : '行情离线(缓存)'}</b></span>
          <span>更新：<b style={{ ...num }}>{updatedAgo}</b></span>
          <span>预测跨度：<b style={{ ...num }}>{data?.horizon ?? '—'} 根</b></span>
        </div>
      </div>

      {/* 架构角色说明：让用户一眼看懂四个模型各干什么 */}
      <div style={{
        marginTop: 12, padding: '10px 14px', fontSize: 12, lineHeight: 1.7,
        color: 'var(--sub)', background: 'var(--panel2)', borderRadius: 8,
        borderLeft: '3px solid var(--blue)',
      }}>
        {data?.note || '云端双脑(DeepSeek+混元)=方向锚；本地 Chronos-2=风险区间；NumPy 终审器=安全阀；下方四时序模型=fusion_v2 融合票(权重0.22)作为第三票方向源，三道安全门(合成行情禁用/snapshot过期/可用模型<2回退单Chronos)。'}
      </div>

      {/* 融合票面板：4 时序模型 → 一道加权方向，透明呈现 */}
      <FusionPanel fv={data?.fusion_vote} />

      {/* 视觉第四票（第5路增强）：渲染 H4/M15 图表→视觉模型识别结构→融合提方向准确率 */}
      <VisionModelPanel data={vision} mode="signal" />

      {/* 模型卡片网格 */}
      {!data && <div style={{ marginTop: 16, color: 'var(--sub)' }}>正在加载模型预测…（首次加载权重约需十几秒）</div>}
      {data && (
        <div style={{
          marginTop: 14,
          display: 'grid',
          gridTemplateColumns: 'repeat(4, 1fr)',
          gap: 12,
        }}>
          {data.models && data.models.length > 0
            ? data.models.map((m) => <ModelCard key={m.name} m={m} />)
            : <div style={{ color: 'var(--sub)' }}>暂无可用模型（可能仍在加载或权重缺失）</div>}
        </div>
      )}

      {/* 页脚红线重申 */}
      <div style={{ marginTop: 18, fontSize: 11, color: 'var(--dim)', lineHeight: 1.6 }}>
        本面板四时序模型在 fusion_v2 模式下作为<strong>融合票（权重 0.22）</strong>接入 meta_agent 决策链第三票方向源（chronos-2 风险区间功能仍保留给 smart_exit）。
        三道安全门：<strong>合成行情禁用 / snapshot 过期 / 可用模型&lt;2 回退单 Chronos</strong>。不在主路径但融合票 = 0.22 票权重。
        是否提升为主判据需另作独立评估。
      </div>
    </div>
  )
}
