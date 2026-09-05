// AI 工作剧场：本地多模型融合擂台 / 决策溯源辩论 / 进化时间线 / 交易执行流
// 全部从后端 /api/dashboard/ai-flow 拉取真实数据，不再使用硬编码假数据
//
// ★ 2026-08-19 云端永久弃用 + 定稿P0落地后的四层分工：
//   感知(视觉7B + Chronos锚) → 裁决(本地加权, 唯一终审) → 校验(Qwen3-8B 校对/仓管/L2平仓) → 执行(规则)。
//   弱时序模型(TimesFM/Time-MoE/Moirai)观测化(qw=0)；Qwen3-8B 已移出方向投票。
//   云端双脑辩论降级为「决策溯源」子区块，云模型停用(cloud_enabled=false)时自动折叠并提示。
import { useEffect, useRef, useState } from 'react'
import { fetchAIFlow } from '../services/api'
// 角色文案/就绪度收口在后端（见 brand/modelReadiness.js 的约束 1）。
// 这里复用同一个模块级单例 store，不会额外增加轮询。
import { useModelReadiness } from '../brand/modelReadiness.js'

// ★ Phase 4：新增 chronos 分支。原来的 fallback 是 'meta'，
//   未知 who 会被静默画成紫色终裁气泡 —— 本地模型的意见会被伪装成最终裁决，
//   这比不显示更糟。现在 fallback 保持 'meta' 但 chronos 被显式接住。
const whoClass = (w) => {
  if (w === 'ds') return 'ds'
  if (w === 'hy') return 'hy'
  if (w === 'chronos') return 'chronos'
  if (w === 'meta') return 'meta'
  if (w === 'fusion') return 'fusion'
  if (w === 'qwen') return 'qwen'
  if (w === 'copilot') return 'copilot'
  if (w === 'execute') return 'execute'
  // 4 个本地时序模型统一用 chronos 色系（绿色时序派）
  if (w && w.startsWith('ts_')) return 'chronos'
  return 'meta'
}
const stanceText = (it) => {
  if (it.who === 'meta') return '终裁'
  if (it.who === 'fusion') return '融合'
  if (it.who === 'copilot') return '副驾'
  if (it.who === 'qwen') return '校对'
  if (it.who === 'execute') return '已执行'
  if (it.stance) return it.stance
  return it.decision === 'BUY' ? '看多' : it.decision === 'SELL' ? '看空' : '观望'
}
const fmtTime = (iso) => {
  if (!iso) return ''
  try {
    const d = new Date(iso)
    if (Number.isNaN(d.getTime())) return ''
    return d.toLocaleTimeString('zh-CN', { hour12: false, timeZone: 'Asia/Shanghai' })
  } catch {
    return ''
  }
}
const fmtClock = (iso) => {
  if (!iso) return '--:--'
  const t = fmtTime(iso)
  return t ? t.slice(0, 5) : '--:--'
}
const weightText = (it) => {
  if (!it) return ''
  if (it.who === 'meta') return `置信 ${it.confidence || 0}%`
  if (it.who === 'execute') return `${it.confidence || 0}% 置信`
  if (it.who === 'fusion') return `w=${it.weight || 0} · 置信 ${it.confidence || 0}%`
  if (it.who === 'qwen') return it.confidence ? `置信 ${it.confidence}%` : '结构校对'
  // ★ Chronos/本地时序模型不产生"置信度"，它给的是分位区间。权重为 0 时意味着本轮
  //   根本没参与加权（服务未就绪），此时显示"置信 0%"会让客户误以为
  //   模型看空到极点——必须显式说"未参与"。
  if (it.who === 'chronos' || (it.who && it.who.startsWith('ts_'))) {
    return it.available === false ? '本轮未参与加权' : `w=${it.weight || 0}`
  }
  return `w=${it.weight || 0} · 置信 ${it.confidence || 0}%`
}
const cleanReasoning = (r, decision, confidence, revised) => {
  const text = (r || revised || '').trim()
  if (!text) return `${decision} @ ${confidence}%`
  // 后端偶发解析失败/空响应时可能残留不可读占位符，前端兜底显示决策方向
  if (/模型未返回内容|模型输出无法解析|模型返回空内容/.test(text)) {
    return `${decision} @ ${confidence}%`
  }
  return text
}
const dirText = (d) => (d === 'BUY' ? '做多 ▲' : d === 'SELL' ? '做空 ▼' : '观望')

// ─────────────────────────────────────────────────────────────
// 本地校对员（Qwen3-8B）印章
//
// 三态严禁合并——这是这个组件里最容易犯的错：
//   skipped 是「压根没查」，clean 是「查过没事」。
//   把 skipped 画成绿色，等于模型挂了却告诉客户一切正常，
//   属于最坏的一类可视化。所以 skipped 一律画灰。
// ─────────────────────────────────────────────────────────────
const PROOFREAD_TONE = {
  clean:   { c: '#2ee6a0', bg: 'rgba(46,230,160,.12)', bd: 'rgba(46,230,160,.35)', txt: '已核对' },
  issues:  { c: '#ffcf4d', bg: 'rgba(255,207,77,.12)', bd: 'rgba(255,207,77,.38)', txt: '有疑点' },
  skipped: { c: '#5b6e91', bg: 'rgba(91,110,145,.10)', bd: 'rgba(91,110,145,.28)', txt: '未核对' },
}

function ProofreadStamp({ pr }) {
  if (!pr) return null
  const t = PROOFREAD_TONE[pr.status] || PROOFREAD_TONE.skipped
  const issues = Array.isArray(pr.issues) ? pr.issues : []
  const severity = (pr.severity || 'none').toLowerCase()
  const sevTone =
    severity === 'major' ? { c: '#ff6b6b', txt: '严重' } :
    severity === 'minor' ? { c: '#ffcf4d', txt: '轻微' } :
                            { c: '#5b6e91', txt: '无' }
  // ★ 2026-08-17 修复：skipped 有三种语义，必须区分展示，否则客户误以为模型挂了。
  //   hold_skip  = 本轮 HOLD 决策（无方向/止损可核）→ 设计行为，中性文案
  //   unavailable= 校对模型不可用/调用失败 → 如实告警（黄）
  //   default   = 尚无决策快照 → 灰
  const isHoldSkip = pr.status === 'skipped' && (pr.skip_reason === 'hold' || pr.decision === 'HOLD')
  const isUnavail = pr.status === 'skipped' && (pr.skip_reason === 'unavailable' || pr.skip_reason === 'no_snapshot' || !isHoldSkip)
  const title =
    pr.status === 'clean'
      ? '本地 Qwen3-8B 已核对该决策的结构一致性，未发现问题'
      : pr.status === 'issues'
        ? `本地 Qwen3-8B 发现 ${issues.length} 处疑点（仅提示，不改变交易方向）`
        : isHoldSkip
          ? '本轮 AI 选择观望（HOLD），没有方向/止损需要核对，校对员按设计跳过'
          : '本地校对员不可用或尚未产出（不影响交易，仅提示）'

  return (
    <div>
      <span
        title={title}
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 5,
          fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
          color: isHoldSkip ? '#8ea3c9' : t.c,
          background: isHoldSkip ? 'rgba(142,163,201,.10)' : t.bg,
          border: `1px solid ${isHoldSkip ? 'rgba(142,163,201,.30)' : t.bd}`,
        }}
      >
        <span style={{ width: 6, height: 6, borderRadius: '50%', background: isHoldSkip ? '#8ea3c9' : t.c, display: 'inline-block' }} />
        Qwen 校对 · {isHoldSkip ? '本轮观望·无需核对' : t.txt}
        {pr.latency_ms != null && (
          <span style={{ color: 'var(--dim)', fontFamily: 'var(--font-num)' }}>{Math.round(pr.latency_ms)}ms</span>
        )}
      </span>

      {pr.status === 'issues' && (
        <div style={{ marginTop: 6, padding: '7px 9px', borderRadius: 8, background: 'rgba(255,207,77,.07)', border: '1px solid rgba(255,207,77,.22)' }}>
          <div style={{ fontSize: 11, color: '#ffcf4d', marginBottom: 3 }}>
            疑点等级：<b style={{ color: sevTone.c }}>{sevTone.txt}</b>
            {pr.blocked && <b style={{ color: '#ff6b6b', marginLeft: 8 }}>· 已拦截并降级 HOLD</b>}
          </div>
          {issues.length > 0 && (
            <ul style={{ margin: '2px 0', paddingLeft: 16, fontSize: 11, color: '#ffe0a3', lineHeight: 1.7 }}>
              {issues.map((s, i) => <li key={i}>{s}</li>)}
            </ul>
          )}
          {pr.action && (
            <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4, lineHeight: 1.6 }}>
              <b style={{ color: '#9fb3d9' }}>处置措施：</b>{pr.action}
            </div>
          )}
        </div>
      )}
      {pr.status !== 'issues' && pr.action && (
        <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4 }}>{pr.action}</div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// 方向 → 颜色 / 文案（中国习惯：红涨绿跌）
// ─────────────────────────────────────────────────────────────
const modelDirStyle = (d) => {
  if (d === 'BUY') return { color: 'var(--red)', label: '看多 ▲' }
  if (d === 'SELL') return { color: 'var(--green)', label: '看空 ▼' }
  if (d === 'HOLD') return { color: 'var(--sub)', label: '观望 —' }
  if (d === 'TIMEOUT') return { color: '#ffcf4d', label: '超时' }
  if (d === 'ERROR') return { color: 'var(--red)', label: '异常' }
  if (d === 'N/A') return { color: 'var(--sub)', label: '待安装' }
  return { color: 'var(--gold)', label: String(d) }
}

// 单个本地模型气泡
function ModelBubble({ m }) {
  const ds = modelDirStyle(m.direction)
  const acc = m.color || 'var(--blue)'
  const isQwen = m.key === 'qwen'
  const hit = m.hit_rate == null ? '—' : (m.hit_rate * 100).toFixed(0) + '%'
  return (
    <div style={{
      borderTop: `2px solid ${acc}`, borderRadius: 8, padding: '10px 12px',
      background: 'var(--panel2)', display: 'flex', flexDirection: 'column', gap: 6,
      opacity: m.available ? 1 : 0.62,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontWeight: 700, color: acc, fontSize: 13 }}>{m.name}</span>
        <span style={{
          fontSize: 9.5, padding: '1px 6px', borderRadius: 10, fontWeight: 700,
          color: m.device === 'GPU' ? '#b07bff' : 'var(--blue)',
          background: m.device === 'GPU' ? 'rgba(176,123,255,.12)' : 'rgba(80,160,255,.10)',
          border: `1px solid ${m.device === 'GPU' ? 'rgba(176,123,255,.32)' : 'rgba(80,160,255,.30)'}`,
        }}>{m.device}</span>
      </div>
      <div style={{ fontSize: 10.5, color: 'var(--sub)' }}>{m.role}</div>

      {isQwen ? (
        <div style={{ fontSize: 11, color: 'var(--sub)', lineHeight: 1.6 }}>
          {m.available
            ? <><span style={{ color: '#2ee6a0' }}>● 在岗</span>{m.warmed ? ' · 已预热' : ' · 冷态'} · 校对 <b style={{ color: 'var(--txt)' }}>{m.proofread_runs || 0}</b> 次</>
            : <><span style={{ color: '#5b6e91' }}>● 未就绪</span>{m.reason ? ` · ${m.reason}` : ''}</>}
          <div style={{ marginTop: 2 }}>副驾放行(L2)：<b style={{ color: 'var(--txt)' }}>{m.copilot_allowed || 0}</b> 次</div>
        </div>
      ) : (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span style={{ ...ds, fontWeight: 800, fontSize: 14 }}>{ds.label}</span>
            {m.score != null && (
              <span style={{ fontFamily: 'var(--font-num)', fontSize: 11, color: 'var(--sub)' }}>
                强度 {m.score > 0 ? '+' : ''}{m.score.toFixed(2)}
              </span>
            )}
          </div>
          {m.available && (
            <div style={{ height: 5, background: 'var(--line)', borderRadius: 3, overflow: 'hidden' }}>
              <div style={{ width: `${Math.round((m.confidence || 0) * 100)}%`, height: '100%', background: acc }} />
            </div>
          )}
          <div style={{ display: 'flex', gap: 12, fontSize: 10.5, color: 'var(--sub)', flexWrap: 'wrap' }}>
            {m.available
              ? <>
                  <span>置信 {(m.confidence || 0).toFixed(2)}</span>
                  {m.hit_rate != null && <span style={{ color: acc }}>命中 {hit}</span>}
                  {m.lo != null && m.hi != null && (
                    <span>区间 {m.lo.toFixed(1)}~{m.hi.toFixed(1)}</span>
                  )}
                </>
              : <span style={{ color: '#ffcf4d' }}>
                  {m.direction === 'ERROR' ? '异常' : m.direction === 'TIMEOUT' ? '超时' : (m.direction === 'N/A' ? '待安装/加载中' : '未运行')}
                </span>}
          </div>
        </>
      )}
    </div>
  )
}

// 视觉模型气泡（动态显示后端 VISION_MODEL 名——3b→7b 升级后保持同步）
// ★ 2026-08-16 修复①：硬编码 "Vision(3b)" / "qwen2.5vl:3b" 没跟后端 VISION_MODEL 同步，
//   后端升级 3b→7b 但前端仍显示旧名 → 用户误判 GPU 不工作。动态从 vision.model 取。
// ★ 2026-08-16 修复②（用户纠正 GPU 编号）：本机 Windows 视角 GPU0=核显(接显示器)、
//   GPU1=第一张3060Ti(qwen3:8b 校对员)、GPU2=第二张3060Ti(视觉 7b)。
//   Vision 视觉实例跑在 **GPU2**（非 CUDA/nvidia-smi 的 0/1 编号）。
function VisionBubble({ vision }) {
  const v = vision?.vote || {}
  const ds = modelDirStyle(v.direction)
  const h4 = modelDirStyle(v.h4_dir)
  const m15 = modelDirStyle(v.m15_dir)
  const m5 = modelDirStyle(v.m5_dir)
  const acc = 'var(--gold)'
  const available = !!v.available
  const m5Silent = !(v.m5_conf > 0)
  // 提取模型短标签："qwen2.5vl:7b" → "7b" / "qwen3-vl:4b" → "4b"
  const _modelRaw = String(vision?.model || "")
  const _modelTag = (() => {
    const m = _modelRaw.match(/^[\w.\-]+:(\w+)$/)
    return m ? m[1] : (_modelRaw || "?")
  })()
  return (
    <div style={{
      borderTop: `2px solid ${acc}`, borderRadius: 8, padding: '10px 12px',
      background: 'var(--panel2)', display: 'flex', flexDirection: 'column', gap: 6,
      opacity: available ? 1 : 0.62,
    }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span title={_modelRaw} style={{ fontWeight: 700, color: acc, fontSize: 13 }}>
          Vision({_modelTag})
        </span>
        <span style={{
          fontSize: 9.5, padding: '1px 6px', borderRadius: 10, fontWeight: 700,
          color: '#5fd0c9', background: 'rgba(95,208,201,.12)', border: '1px solid rgba(95,208,201,.32)',
        }}>GPU2</span>
      </div>
      <div style={{ fontSize: 10.5, color: 'var(--sub)', lineHeight: 1.5, whiteSpace: 'normal', wordBreak: 'break-word' }}>
        H4/M15/M5 三周期结构识别（第5路增强）
      </div>

      {available ? (
        <>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span style={{ ...ds, fontWeight: 800, fontSize: 14 }}>{ds.label}</span>
            {v.score != null && (
              <span style={{ fontFamily: 'var(--font-num)', fontSize: 11, color: 'var(--sub)' }}>
                强度 {v.score > 0 ? '+' : ''}{v.score.toFixed(2)}
              </span>
            )}
          </div>
          <div style={{ height: 5, background: 'var(--line)', borderRadius: 3, overflow: 'hidden' }}>
            <div style={{ width: `${Math.round((v.confidence || 0) * 100)}%`, height: '100%', background: acc }} />
          </div>
          <div style={{ display: 'flex', gap: 12, fontSize: 10.5, color: 'var(--sub)', flexWrap: 'wrap' }}>
            <span>置信 {(v.confidence || 0).toFixed(2)}</span>
            <span style={{ color: h4.color }}>H4 {h4.label.replace(/[▲▼]/, '')}</span>
            <span style={{ color: m15.color }}>M15 {m15.label.replace(/[▲▼]/, '')}</span>
            {m5Silent
              ? <span style={{ color: 'var(--dim)' }}>M5 沉默</span>
              : <span style={{ color: m5.color }}>M5 {m5.label.replace(/[▲▼]/, '')}</span>}
          </div>
        </>
      ) : (
        // ★ 2026-08-16 修复：原逻辑只看 last_err 非空就显示"运行异常"——但 last_err
        //   是历史错误缓存（上次失败原因），最近一轮推理可能已恢复。改为综合判断：
        //   started=True 且 ok_runs/runs 有进展 → "读图中"；否则才是"运行异常"。
        (() => {
          const started = !!vision?.started
          const runs = Number(vision?.runs || 0)
          const okRuns = Number(vision?.ok_runs || 0)
          const lastErr = String(vision?.last_err || "")
          if (started && runs > 0 && okRuns > 0) {
            return <span style={{ color: '#5fd0c9', fontSize: 11 }}>读图中（最近 OK）</span>
          }
          if (lastErr && (!started || okRuns === 0)) {
            return <span style={{ color: '#ffcf4d', fontSize: 11 }}>运行异常: {lastErr.slice(0, 36)}</span>
          }
          if (started) {
            return <span style={{ color: '#5fd0c9', fontSize: 11 }}>读图中（首次）</span>
          }
          return <span style={{ color: '#ffcf4d', fontSize: 11 }}>待安装/加载中</span>
        })()
      )}
    </div>
  )
}

// 融合法官（fusion_v2 第四票）
function FusionJudge({ vote, proofread }) {
  if (!vote) return null
  const ds = modelDirStyle(vote.direction)
  const score = vote.score || 0
  // 归一化强度 → 进度条（0.5 = 0，右偏=多头，左偏=空头）
  const posPct = Math.round((score + 1) * 50)
  return (
    <div style={{
      marginTop: 8, padding: '8px 10px', borderRadius: 8,
      background: 'linear-gradient(135deg, rgba(46,230,160,.06), rgba(80,160,255,.06))',
      border: '1px solid rgba(120,200,255,.25)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: 'var(--sub)', fontWeight: 700 }}>融合法官</span>
        <span style={{
          fontSize: 18, fontWeight: 900,
          color: vote.available ? ds.color : 'var(--sub)',
        }}>{vote.available ? ds.label : '融合票不可用'}</span>
        {vote.available && (
          <span style={{ fontSize: 11, color: 'var(--sub)', fontFamily: 'var(--font-num)' }}>
            强度 {score > 0 ? '+' : ''}{score.toFixed(2)} · 置信 {(vote.confidence || 0).toFixed(2)} · 权重系数 ×{vote.weight_scale || 1}
          </span>
        )}
        <span style={{
          marginLeft: 'auto', fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
          color: vote.agree ? '#2ee6a0' : '#ffcf4d',
          background: vote.agree ? 'rgba(46,230,160,.12)' : 'rgba(255,207,77,.12)',
          border: `1px solid ${vote.agree ? 'rgba(46,230,160,.32)' : 'rgba(255,207,77,.32)'}`,
        }}>{vote.agree ? '模型同向' : '模型分歧'}</span>
      </div>

      {/* 方向强度条（空头← →多头） */}
      {vote.available && (
        <div style={{ marginTop: 6, position: 'relative', height: 6, background: 'var(--line)', borderRadius: 3 }}>
          <div style={{ position: 'absolute', left: '50%', top: -3, bottom: -3, width: 1, background: 'var(--dim)' }} />
          <div style={{
            position: 'absolute', top: 0, bottom: 0,
            left: score >= 0 ? '50%' : `${posPct}%`,
            width: `${Math.abs(score) * 50}%`,
            background: score >= 0 ? 'var(--red)' : 'var(--green)', borderRadius: 3,
          }} />
        </div>
      )}

      {vote.note && (
        <div style={{ marginTop: 4, fontSize: 11, color: 'var(--sub)', lineHeight: 1.45 }}>{vote.note}</div>
      )}

      {/* 各模型融合权重明细 */}
      {vote.available && vote.per_model && vote.per_model.length > 0 && (
        <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 3 }}>
          <div style={{ fontSize: 10.5, color: 'var(--sub)' }}>融合权重明细（按命中率加权）</div>
          {vote.per_model.map((pm, i) => {
            const pds = modelDirStyle(pm.direction)
            return (
              <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11 }}>
                <span style={{ width: 100, color: 'var(--txt)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{pm.name}</span>
                <span style={{ ...pds, fontWeight: 700, width: 52 }}>{pds.label}</span>
                <div style={{ flex: 1, height: 4, background: 'var(--line)', borderRadius: 2, overflow: 'hidden' }}>
                  <div style={{ width: `${Math.round((pm.qw || 0) * 100)}%`, height: '100%', background: 'var(--blue)' }} />
                </div>
                <span style={{ fontFamily: 'var(--font-num)', color: 'var(--sub)', width: 38, textAlign: 'right' }}>qw {(pm.qw || 0).toFixed(2)}</span>
              </div>
            )
          })}
        </div>
      )}

      <div style={{ marginTop: 6, fontSize: 10, color: 'var(--dim)', lineHeight: 1.45 }}>
        裁决：Chronos 单锚主导（竞技场实证 集成净点 10.6 &lt; 单锚 319.4，弱信号叠加无互补）→
        TimesFM / Time-MoE / Moirai 降为观测（qw=0，仅参考面板展示）。融合票作 meta_agent 时序票（权重 0.22）。
      </div>

      {/* ★ 2026-08-19 定稿P0-1：Chronos 单锚化（弱模型观测化，不再反向静默/同向加成） */}
      <div style={{ marginTop: 4, fontSize: 10, color: 'var(--green)', lineHeight: 1.45 }}>
        ★ Chronos 为方向锚：非锚模型本轮全部标记「观测」（qw=0 不参与加权/agree）；
        锚观望则融合票 HOLD（回退单 Chronos）。下方权重明细 qw=0 即「该模型本轮观测化」。
      </div>

      {/* Qwen 校对印章盖在融合法官上（校对对象就是这道融合裁决） */}
      <div style={{ marginTop: 6 }}><ProofreadStamp pr={proofread} /></div>
    </div>
  )
}

// 篮子级 AI 持仓处置卡（2026-08-17 · 用户铁律：开完仓核心任务=维护持仓）
// 数据：ai-flow.basket（后端从最近决策快照提取 DS/HY position_action 融合结果）
function BasketCard({ basket }) {
  if (!basket || !basket.available) return null
  const act = basket.action
  const meta = {
    close_all: { c: '#ff4d5e', txt: '全平锁利/避险', desc: 'AI 建议清空篮子' },
    trim:      { c: '#f59e0b', txt: '分批减仓',      desc: 'AI 建议每笔减半' },
    hold:      { c: '#2ee6a0', txt: '继续持有',      desc: 'AI 建议按兵不动' },
  }[act] || { c: '#94a3b8', txt: act, desc: '' }
  return (
    <div style={{
      marginTop: 8, padding: '8px 10px', borderRadius: 8,
      background: 'linear-gradient(135deg, rgba(255,77,94,.07), rgba(245,158,11,.05))',
      border: '1px solid rgba(255,120,120,.28)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: 'var(--sub)', fontWeight: 700 }}>AI 持仓处置</span>
        <span style={{ fontSize: 15, fontWeight: 900, color: meta.c }}>{meta.txt}</span>
        <span style={{ fontSize: 11, color: 'var(--sub)', fontFamily: 'var(--font-num)' }}>
          置信 {(basket.confidence || 0).toFixed(2)}
        </span>
        <span style={{
          marginLeft: 'auto', fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
          color: basket.confirmed ? '#2ee6a0' : '#ffcf4d',
          background: basket.confirmed ? 'rgba(46,230,160,.12)' : 'rgba(255,207,77,.12)',
          border: `1px solid ${basket.confirmed ? 'rgba(46,230,160,.32)' : 'rgba(255,207,77,.32)'}`,
        }}>
          {basket.confirmed ? '已连续确认 · 将执行' : '确认中（连续 2 轮防抖）'}
        </span>
      </div>
      {basket.reason && (
        <div style={{ marginTop: 4, fontSize: 11, color: 'var(--sub)', lineHeight: 1.5 }}>
          {basket.reason}
          {basket.confirm_note ? <span style={{ color: 'var(--dim)' }}>（{basket.confirm_note}）</span> : null}
        </div>
      )}
      <div style={{ marginTop: 4, fontSize: 10, color: 'var(--dim)', lineHeight: 1.45 }}>
        角色：本地裁决（视觉 + Chronos 锚 + 体制）→ 连续 2 轮确认 → 执行 close_all/trim；篮子浮盈回吐 ≥ max(峰值×50%, $8) 机械兜底全平。
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// 投票席（2026-08-17 · 用户要求：严谨体现多少模型参与裁决）
// 数据：ai-flow.voting（后端从最新决策快照提取 视觉/Chronos锚/体制/融合 等本地票）
// ★ 2026-08-19：云端弃用 + 副驾移出投票 + Chronos 单锚化后，实际参与方为
//   视觉(0.30) / Chronos锚(0.22) / 体制基线(0.20)，副驾席已移除。
// 三态：
//   counted = 本票已计入裁决（weight>0 且 vote∈BUY/SELL）→ 实心色 + 「已计票」
//   watch   = 有权重但观望（HOLD）→ 空心 + 「观望未计」
//   absent  = 未参与/未启用（weight=0）→ 灰 + 「未参与」
// ─────────────────────────────────────────────────────────────
function VotingSeats({ voting }) {
  if (!voting || !voting.available || !voting.seats || voting.seats.length === 0) {
    return null
  }
  const seatTone = {
    counted: { c: '#2ee6a0', bg: 'rgba(46,230,160,.10)', bd: 'rgba(46,230,160,.35)', tag: '已计票' },
    watch:   { c: '#f59e0b', bg: 'rgba(245,158,11,.07)', bd: 'rgba(245,158,11,.30)', tag: '观望未计' },
    absent:  { c: '#5b6e91', bg: 'rgba(91,110,145,.07)', bd: 'rgba(91,110,145,.25)', tag: '未参与' },
  }
  const voteZh = { BUY: '看多 ▲', SELL: '看空 ▼', HOLD: '观望 —' }
  const voteColor = { BUY: 'var(--red)', SELL: 'var(--green)', HOLD: 'var(--sub)' }
  return (
    <div style={{ marginTop: 10, padding: '8px 10px', borderRadius: 8, background: 'var(--panel2)', border: '1px solid var(--line)' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
        <span style={{ fontSize: 12, fontWeight: 800, color: 'var(--txt)' }}>🎫 本轮裁决投票席</span>
        <span style={{
          fontSize: 11, padding: '2px 8px', borderRadius: 20,
          color: voting.counted_seats >= 3 ? '#2ee6a0' : '#f59e0b',
          background: voting.counted_seats >= 3 ? 'rgba(46,230,160,.10)' : 'rgba(245,158,11,.10)',
          border: `1px solid ${voting.counted_seats >= 3 ? 'rgba(46,230,160,.30)' : 'rgba(245,158,11,.30)'}`,
        }}>
          有效票 {voting.counted_seats} / {voting.total_seats} 席
        </span>
        <span style={{ marginLeft: 'auto', fontSize: 11, color: 'var(--sub)' }}>
          裁决：<b style={{ color: voteColor[voting.decision] || 'var(--txt)' }}>{voteZh[voting.decision] || voting.decision}</b>
          <span style={{ fontFamily: 'var(--font-num)', marginLeft: 6 }}>置信 {(voting.confidence * 100).toFixed(0)}%</span>
        </span>
      </div>
      {voting.is_latest === false && voting.ts && (
        <div style={{ fontSize: 9.5, color: 'var(--dim)', marginBottom: 5 }}>
          注：HOLD 观望轮次不生成交易记录，以上为最近一次含方向裁决的票况（{voting.ts}）。当前观望轮的票况见上方「本轮 AI 状态」。
        </div>
      )}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 6 }}>
        {voting.seats.map((s) => {
          const tone = seatTone[s.state] || seatTone.absent
          const extra = s.key === 'fusion' && s.models ? `（${s.models}模型）` : ''
          return (
            <div key={s.key} style={{
              padding: '6px 8px', borderRadius: 6,
              background: tone.bg, border: `1px solid ${tone.bd}`,
              display: 'flex', flexDirection: 'column', gap: 3,
            }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 4 }}>
                <span style={{ fontSize: 11, fontWeight: 700, color: 'var(--txt)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {s.name}{extra}
                </span>
                <span style={{ fontSize: 9, padding: '0 5px', borderRadius: 8, color: tone.c, border: `1px solid ${tone.bd}`, whiteSpace: 'nowrap' }}>
                  {tone.tag}
                </span>
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--dim)' }}>{s.role}</div>
              <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                <span style={{ fontSize: 12, fontWeight: 800, color: voteColor[s.vote] || 'var(--sub)' }}>
                  {voteZh[s.vote] || s.vote}
                </span>
                {s.counted && (
                  <span style={{ fontFamily: 'var(--font-num)', fontSize: 9.5, color: 'var(--sub)' }}>
                    w={s.weight} · {(s.confidence * 100).toFixed(0)}%
                  </span>
                )}
              </div>
            </div>
          )
        })}
      </div>
      <div style={{ marginTop: 5, fontSize: 9.5, color: 'var(--dim)', lineHeight: 1.5 }}>
        口径与裁决器一致：「已计票」= 该模型本轮真实投出方向且权重计入 Meta 加权；「观望未计」= 模型在岗但选择观望（HOLD）；「未参与」= 条件未触发（如副驾仅当时序有方向才调、融合票仅当锚有方向才生效）。
      </div>
    </div>
  )
}

// Qwen3-8B 校验层气泡（2026-08-19 定稿P0：副驾已移出投票，qwen3:8b 专职校验层——
// 校对员(结构闸) / 仓位管理 / L2 反向平仓，不再产生方向票）
function CopilotBubble({ vote, cloudEnabled = true, role = '' }) {
  if (!vote || !vote.available) return null
  const ds = modelDirStyle(vote.vote)
  const conf = vote.confidence || 0
  // ★ 2026-08-19 定稿P0：qwen3:8b 移出投票链（brain_audit 实证 2979 次调用 0 次过锁），
  //   角色固定为校验层——不再有"方向主脑/副驾"投票语义。
  const roleName = role || 'Qwen3-8B 本地校验'
  const roleNote = '角色：校验层 · 校对员(结构闸) + 仓位管理 + L2 反向平仓（conf≥0.60 连续2轮）。已移出方向投票（0.15 权重空转实锤）。'
  return (
    <div style={{
      marginTop: 8, padding: '8px 10px', borderRadius: 8,
      background: 'linear-gradient(135deg, rgba(176,123,255,.08), rgba(95,208,201,.06))',
      border: '1px solid rgba(176,123,255,.30)',
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 12, color: 'var(--sub)', fontWeight: 700 }}>{roleName}</span>
        <span style={{ fontSize: 16, fontWeight: 900, color: ds.color }}>{ds.label}</span>
        <span style={{ fontSize: 11, color: 'var(--sub)', fontFamily: 'var(--font-num)' }}>
          置信 {conf.toFixed(2)}
        </span>
        <span style={{
          marginLeft: 'auto', fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
          color: 'var(--dim)', background: 'rgba(91,110,145,.10)',
          border: '1px solid rgba(91,110,145,.30)',
        }}>校验层 · 不投票</span>
      </div>
      {vote.note && (
        <div style={{ marginTop: 4, fontSize: 11, color: 'var(--sub)', lineHeight: 1.45 }}>{vote.note}</div>
      )}
      <div style={{ marginTop: 4, fontSize: 10, color: 'var(--dim)', lineHeight: 1.45 }}>
        {roleNote}
      </div>
    </div>
  )
}

// 注：AI 自动交易循环状态条 + Key 来源 已移至作战图顶部横条（LiveStatusBar.jsx）
// 本组件只关心擂台/进化/交易三类内容流，状态条在 Dashboard 渲染，避免占大块空间。

export default function AITheater({ vision }) {
  const [debate, setDebate] = useState([])
  const [evo, setEvo] = useState([])
  const [feed, setFeed] = useState([])
  const [weights, setWeights] = useState({ deepseek: 0.5, hunyuan: 0.5, deepseek_signals: 0, hunyuan_signals: 0 })
  const [counters, setCounters] = useState({ ai_iterations: 0, decisions_today: 0, scans_today: 0 })
  const [err, setErr] = useState('')
  const [updatedAt, setUpdatedAt] = useState('')
  // ★ 2026-08-11 新增：本地多模型融合擂台数据源
  const [localModels, setLocalModels] = useState([])
  const [fusionVote, setFusionVote] = useState(null)
  const [proofread, setProofread] = useState(null)
  const [cloudEnabled, setCloudEnabled] = useState(true)
  // 后端下发的运行模式与角色文案（2026-08-19 后 qwen3 = 校验层，不投票）
  const readiness = useModelReadiness()
  // ★ 2026-08-14 新增：副驾第5路票（Qwen3-8B 常态确认型副驾）
  const [copilot, setCopilot] = useState(null)
  // ★ 2026-08-17 新增：篮子级 AI 持仓处置（用户铁律：开完仓核心任务=维护持仓）
  const [basket, setBasket] = useState(null)
  // ★ 2026-08-17 新增：投票席（严谨体现多少模型参与裁决）
  const [voting, setVoting] = useState(null)
  // ★ 2026-08-12 新增：本地模式决策溯源链路
  const [localTrace, setLocalTrace] = useState([])

  // ★ 2026-08-12 布局稳定器：第一模块（本地多模型融合擂台）要完整显示、无滚动条，
  //   后两块（决策溯源/进化时间线）必须严格和它等高。CSS grid 的 stretch 会被后两块
  //   内容撑开，做不到这一点；改用 ResizeObserver 实时测量第一模块自然高度，同步给后两块。
  const mainRef = useRef(null)
  const debateRef = useRef(null)
  const evoRef = useRef(null)
  useEffect(() => {
    if (!mainRef.current || !debateRef.current || !evoRef.current) return
    const sync = () => {
      if (!mainRef.current || !debateRef.current || !evoRef.current) return
      // 窄屏堆叠时不清除高度，让三块各自自然高度
      if (window.innerWidth <= 1200) {
        debateRef.current.style.height = ''
        evoRef.current.style.height = ''
        return
      }
      const h = mainRef.current.getBoundingClientRect().height
      debateRef.current.style.height = `${h}px`
      evoRef.current.style.height = `${h}px`
    }
    const ro = new ResizeObserver(sync)
    ro.observe(mainRef.current)
    window.addEventListener('resize', sync)
    sync()
    return () => {
      ro.disconnect()
      window.removeEventListener('resize', sync)
    }
  }, [])

  useEffect(() => {
    let alive = true
    const load = async () => {
      try {
        const data = await fetchAIFlow()
        if (!alive) return
        setDebate(Array.isArray(data?.debate) ? data.debate : [])
        setEvo(Array.isArray(data?.evolution) ? data.evolution : [])
        setFeed(Array.isArray(data?.feed) ? data.feed : [])
        setWeights(data?.weights || weights)
        setCounters(data?.counters || counters)
        setLocalModels(Array.isArray(data?.local_models) ? data.local_models : [])
        setFusionVote(data?.fusion_vote || null)
        setProofread(data?.proofread || null)
        setCopilot(data?.copilot_vote || null)
        setBasket(data?.basket || null)
        setVoting(data?.voting || null)
        setCloudEnabled(data?.cloud_enabled !== false)
        setLocalTrace(Array.isArray(data?.local_trace) ? data.local_trace : [])
        setUpdatedAt(data?.ts || new Date().toISOString())
        setErr('')
      } catch (e) {
        if (!alive) return
        setErr(e?.message || 'AI 流程数据获取失败')
      }
    }
    let timer = null
    const tick = async () => {
      await load()
      if (alive) timer = setTimeout(tick, 5000)
    }
    tick()
    return () => {
      alive = false
      if (timer) clearTimeout(timer)
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const consensusPct = debate.find((d) => d.who === 'meta')?.consensus_pct || 0
  // 监控类（走马灯）：扫描 / 评估 / 信号 —— 全部是 AI 在盯盘，不属真实交易动作
  const scanItems = feed.filter((f) =>
    ['scan', 'evaluate', 'signal'].includes(f.kind)
  )
  // 交易执行流：只显示真实仓位动作（开仓 / 平仓 / 部分平 / 止损）
  const tradeItems = feed.filter((f) =>
    ['open', 'close', 'close_partial', 'sl'].includes(f.kind)
  )
  // 跑马灯：把最近 12 条 scan 拼成一条带，超出则从最新再补，最多 30 条
  const marqueeList = scanItems.slice(0, 30)
  // 时间格式化为 HH:MM
  const hm = (iso) => {
    const t = fmtTime(iso)
    return t ? t.slice(0, 5) : ''
  }

  return (
    <div className="panel" style={{ marginBottom: 14 }}>
      {/* ── 顶部头条：标题 / 计数 / 实时状态 / 扫描跑马灯 ── */}
      <div className="h" style={{ flexWrap: 'wrap', gap: 8 }}>
        <span>AI 工作剧场 · 前台实时</span>
        <span className="live">实时</span>
        <span className="ai-counters">
          AI 已迭代 <b>{counters.ai_iterations}</b> 次 · 今日决策 <b>{counters.decisions_today}</b> 次 · 扫描 <b>{counters.scans_today}</b> 次
        </span>
        {/* 横向跑马灯：扫描活动快讯（位置 = 原空着的那一块红框框起来处） */}
        <div className="scan-tape" title="AI 持续扫描 XAUUSD 多周期行情">
          <span className="scan-tape-label">扫描快讯</span>
          <div className="scan-tape-viewport">
            {marqueeList.length === 0 ? (
              <span className="scan-tape-empty">暂无扫描记录</span>
            ) : (
              <div className="scan-tape-track">
                {/* 第一份用于显示，第二份用于无缝循环 */}
                {marqueeList.map((s, i) => (
                  <span key={`a-${i}`} className="scan-chip">
                    <span className="scan-chip-tm">{hm(s.ts)}</span>
                    <span className="scan-chip-dot" />
                    <span className="scan-chip-text">{s.text}</span>
                  </span>
                ))}
                {marqueeList.map((s, i) => (
                  <span key={`b-${i}`} className="scan-chip" aria-hidden="true">
                    <span className="scan-chip-tm">{hm(s.ts)}</span>
                    <span className="scan-chip-dot" />
                    <span className="scan-chip-text">{s.text}</span>
                  </span>
                ))}
              </div>
            )}
          </div>
        </div>
        <span style={{ color: 'var(--dim)', fontSize: 11, marginLeft: 'auto', whiteSpace: 'nowrap' }}>
          更新 {fmtTime(updatedAt)}
        </span>
      </div>
      <div className="theater">
        {/* ── 本地多模型融合擂台（2026-08-11 新增，主视觉） ── */}
        <div ref={mainRef} className="panel theater-main" style={{ padding: 12 }}>
          <div className="h" style={{ marginBottom: 8, flexWrap: 'wrap', gap: 6 }}>
            本地多模型融合擂台
            <span style={{
              fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
              color: '#5fd0c9', background: 'rgba(95,208,201,.10)', border: '1px solid rgba(95,208,201,.30)',
            }}>感知·视觉7B + Chronos锚</span>
            <span style={{
              fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
              color: '#b07bff', background: 'rgba(176,123,255,.10)', border: '1px solid rgba(176,123,255,.30)',
            }}>校验·Qwen3-8B</span>
            <span style={{
              fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
              color: 'var(--blue)', background: 'rgba(80,160,255,.10)', border: '1px solid rgba(80,160,255,.30)',
            }}>观测·3时序模型</span>
            <span style={{
              fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
              color: '#ffcf4d', background: 'rgba(255,207,77,.10)', border: '1px solid rgba(255,207,77,.30)',
            }}>★ Chronos 单锚 · 弱模型观测化</span>
            <span style={{
              fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
              color: '#5fd0c9', background: 'rgba(95,208,201,.10)', border: '1px solid rgba(95,208,201,.30)',
            }}>GPU2·Vision({(() => {
              const m = String(vision?.model || "").match(/^[\w.\-]+:(\w+)$/)
              return m ? m[1] : (vision?.model || "?")
            })()})</span>
            <span style={{
              fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
              color: '#2ee6a0', background: 'rgba(46,230,160,.10)', border: '1px solid rgba(46,230,160,.30)',
            }}>融合票权重 0.22</span>
          </div>

          {!cloudEnabled && (
            <div style={{
              fontSize: 11, color: '#ffcf4d', background: 'rgba(255,207,77,.07)',
              border: '1px solid rgba(255,207,77,.22)', borderRadius: 6, padding: '6px 10px', marginBottom: 10,
            }}>
              {/* ★ 2026-08-19 云端永久弃用 + 单锚化：本地四层分工——
                  感知(视觉7B + Chronos锚) / 裁决(本地加权) / 校验(Qwen3-8B) / 执行(规则)。
                  就绪数取后端就绪度，取不到不报数字 —— 报错的数字比不报更伤信任。 */}
              云端方案已永久弃用（零云成本）。当前由
              <b style={{ color: '#ffe08a' }}>
                {' '}全本地四层决策{readiness.localReady != null
                  ? `（${readiness.localReady}/${readiness.localTotal ?? 3} 在岗）`
                  : ''}{' '}
              </b>
              裁决方向：视觉 0.30 · Chronos 锚 0.22 · 体制 0.20（弱时序观测化、qwen3 校验不投票）。
            </div>
          )}

          {localModels.length === 0 && !vision ? (
            <div style={{ color: 'var(--dim)', fontSize: 12, padding: 8 }}>
              正在加载本地模型状态（首次加载权重约需十几秒）…
            </div>
          ) : (
            <div style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
              gap: 10,
            }}>
              {localModels.map((m) => <ModelBubble key={m.key} m={m} />)}
              {vision && <VisionBubble vision={vision} />}
            </div>
          )}

          <VotingSeats voting={voting} />
          <FusionJudge vote={fusionVote} proofread={proofread} />
          <BasketCard basket={basket} />
          <CopilotBubble vote={copilot} cloudEnabled={cloudEnabled} role={readiness.roles?.qwen} />
        </div>

        {/* ── 决策溯源 · 辩论擂台（云端双脑 vs Meta 终裁 / 本地模型决策链路） ── */}
        <div ref={debateRef} className="panel" style={{ padding: 12 }}>
          <div className="h" style={{ marginBottom: 8 }}>
            决策溯源 · 辩论擂台
            {cloudEnabled ? (
              <>
                <span className="badge-ds">DeepSeek 激进派</span>
                <span className="badge-hy">混元 稳健派</span>
                <span className="badge-chronos">Chronos 时序派</span>
                <span style={{
                  fontSize: 10.5, padding: '2px 8px', borderRadius: 20, marginLeft: 4,
                  color: '#b07bff', background: 'rgba(176,123,255,.10)', border: '1px solid rgba(176,123,255,.30)',
                }}>Qwen 校对员</span>
              </>
            ) : (
              <>
                {/* ★ 2026-08-19 定稿P0：qwen3:8b 已移出投票，专职校验层——
                    校对员/仓管/L2 反向平仓，不再是方向主脑/校对员投票票。 */}
                <span style={{
                  fontSize: 10.5, padding: '2px 8px', borderRadius: 20, marginLeft: 4,
                  color: '#b07bff', background: 'rgba(176,123,255,.10)', border: '1px solid rgba(176,123,255,.30)',
                }}>{readiness.roles?.qwen ? `Qwen3 ${readiness.roles.qwen}` : 'Qwen3 本地校验'}</span>
                <span className="badge-vision" style={{
                  fontSize: 10.5, padding: '2px 8px', borderRadius: 20, marginLeft: 4,
                  color: '#5fd0c9', background: 'rgba(95,208,201,.10)', border: '1px solid rgba(95,208,201,.30)',
                }}>视觉结构 0.30</span>
                <span className="badge-chronos">时序融合票 0.22</span>
                <span className="badge-meta">Meta 终裁</span>
              </>
            )}
            {consensusPct > 0 && <span className="cons-badge">共识度 {consensusPct}%</span>}
          </div>

          {/* ★ 2026-08-17：本轮一句话状态（客户秒懂：AI 现在想干嘛、为什么、要不要管） */}
          {(() => {
            const metaItem = debate.find((d) => d.who === 'meta')
            const summary = metaItem?.plain_summary
            if (!summary) return null
            return (
              <div style={{
                marginBottom: 8, padding: '8px 12px', borderRadius: 8,
                background: 'linear-gradient(135deg, rgba(80,160,255,.08), rgba(46,230,160,.06))',
                border: '1px solid rgba(120,200,255,.28)',
                fontSize: 12, lineHeight: 1.6, color: 'var(--txt)',
              }}>
                <span style={{ fontWeight: 800, color: 'var(--blue)', marginRight: 6 }}>🤖 本轮 AI 状态</span>
                {summary}
              </div>
            )
          })()}

          {cloudEnabled ? (
            <div className="debate">
              {debate.length === 0 && (
                <div style={{ color: 'var(--dim)', fontSize: 12, padding: 8 }}>
                  暂无辩论记录 — 点击「触发AI辩论决策」或开启自动交易
                </div>
              )}
              {debate.map((d, i) => (
                <div key={`${d.ts || 'd'}-${i}`} className={`bub ${whoClass(d.who)}`}>
                  <div className="who">
                    {d.model} · {stanceText(d)}
                    <span className="bub-meta">
                      {d.who === 'meta' && d.consensus_pct ? `共识 ${d.consensus_pct}% · ` : ''}
                      R{d.round || 1} · {weightText(d)}
                    </span>
                  </div>
                  {d.who === 'chronos'
                    ? (d.reasoning || '本地时序模型本轮无输出')
                    : cleanReasoning(d.reasoning, d.decision, d.confidence, d.revised_reasoning)}
                  {d.who === 'meta' && d.plain_summary && (
                    <div className="plain-line">💡 人话解读：{d.plain_summary}</div>
                  )}
                  {/* 终裁落地前，本地 Qwen3-8B 会做一次结构校对。
                      印章画在终裁气泡上，因为校对的对象就是这条终裁。 */}
                  {d.who === 'meta' && <ProofreadStamp pr={d.provenance?.proofread} />}
                </div>
              ))}
            </div>
          ) : (
            <div className="debate">
              {localTrace.length === 0 && (
                <div style={{ color: 'var(--dim)', fontSize: 12, padding: 8 }}>
                  本地模型决策链路加载中… 首次启动时序模型约需十几秒。
                </div>
              )}
              {(() => {
                const metaItem = localTrace.find((d) => d.who === 'meta')
                const summary = metaItem?.plain_summary
                if (!summary) return null
                return (
                  <div style={{
                    marginBottom: 8, padding: '8px 12px', borderRadius: 8,
                    background: 'linear-gradient(135deg, rgba(80,160,255,.08), rgba(46,230,160,.06))',
                    border: '1px solid rgba(120,200,255,.28)',
                    fontSize: 12, lineHeight: 1.6, color: 'var(--txt)',
                  }}>
                    <span style={{ fontWeight: 800, color: 'var(--blue)', marginRight: 6 }}>🤖 本轮 AI 状态</span>
                    {summary}
                  </div>
                )
              })()}
              {localTrace.map((d, i) => (
                <div key={`${d.ts || 'ld'}-${i}`} className={`bub ${whoClass(d.who)}`}>
                  <div className="who">
                    {d.model} · {stanceText(d)}
                    <span className="bub-meta">
                      {fmtClock(d.ts)} · {weightText(d)}
                    </span>
                  </div>
                  {d.reasoning || `${d.decision} @ ${d.confidence || 0}%`}
                  {d.who === 'meta' && d.plain_summary && (
                    <div className="plain-line">💡 人话解读：{d.plain_summary}</div>
                  )}
                  {d.who === 'meta' && <ProofreadStamp pr={proofread} />}
                </div>
              ))}
            </div>
          )}
        </div>

        {/* ── 进化时间线 ── */}
        <div ref={evoRef} className="panel" style={{ padding: 12 }}>
          <div className="h" style={{ marginBottom: 8 }}>
            进化时间线
            <span className="evo-counter">AI 已自我迭代 <b>{counters.ai_iterations}</b> 次</span>
          </div>
          <div className="evo">
            {evo.length === 0 && (
              <div style={{ color: 'var(--dim)', fontSize: 12, padding: 8 }}>
                暂无自进化事件 — MetaAgent 会随交易反馈自动调整权重
              </div>
            )}
            {evo.map((e, i) => (
              <div key={`${e.ts || 'e'}-${i}`} className="it">
                <div className="tm">{fmtClock(e.ts)}</div>
                <div>
                  <span className={`evo-tag evo-${e.kind}`}>{e.label}</span>{' '}
                  <b style={{ color: 'var(--txt)' }}>{e.subject}</b>{' '}
                  {e.before && <span style={{ color: 'var(--dim)' }}>{e.before}</span>}
                  {e.delta && <span style={{ color: e.delta.startsWith('+') ? 'var(--green, #4caf50)' : 'var(--red)' }}> → {e.after}（{e.delta}）</span>}
                  {e.after && !e.delta && <span style={{ color: 'var(--blue)' }}> → {e.after}</span>}
                  <div style={{ color: 'var(--dim)', fontSize: 11, marginTop: 2 }}>{e.reason}</div>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
      {err && <div style={{ color: 'var(--red)', fontSize: 11, padding: '4px 12px 8px' }}>{err}</div>}

      {/* ── 交易执行流：只看真实交易事件（开/平/止损/分批/评估/信号），scan 走顶部跑马灯 ── */}
      <div className="panel feed-panel" style={{ padding: 12, marginBottom: 14 }}>
          <div className="h" style={{ marginBottom: 8 }}>
            交易执行流
            <span className="evo-counter">今日 AI 决策 <b>{counters.decisions_today}</b> 次</span>
          </div>
          <div className="feed">
            {tradeItems.length === 0 && (
              <div style={{ color: 'var(--dim)', fontSize: 12, padding: 8, lineHeight: 1.6 }}>
                <div style={{ marginBottom: 4, color: 'var(--txt)' }}>📭 暂无真实交易执行</div>
                <div>AI 正在持续扫描 XAUUSD 多周期行情（见顶部跑马灯），等待高确定性信号入场。</div>
                <div style={{ marginTop: 4 }}>已过滤 <b style={{ color: 'var(--purple)' }}>{scanItems.length}</b> 条扫描类（仅监控，未下单）。</div>
              </div>
            )}
            {tradeItems.map((f, i) => {
              const arrow = f.direction === 'BUY' ? '▲' : f.direction === 'SELL' ? '▼' : ''
              const acctName = f.account_name || ''
              const acctLogin = f.account_login || ''
              return (
                <div key={`${f.ts || 'f'}-${i}`} className={`e feed-${f.kind}`}>
                  <span className="tm">{fmtClock(f.ts)}</span>{' '}
                  <span className={`tagx ${f.tag}`}>{f.tag_text}</span>{' '}
                  {arrow && <span className={f.direction === 'BUY' ? 'up' : 'down'}>{arrow}</span>}{' '}
                  {acctName && (
                    <>
                      <span style={{
                        fontSize: 10, color: 'var(--purple)', background: 'rgba(176,123,255,.12)',
                        padding: '0px 5px', borderRadius: 3, marginRight: 2, fontWeight: 700,
                      }}>{acctName}</span>
                      {acctLogin && (
                        <span style={{
                          fontSize: 9, color: 'var(--dim)', background: 'rgba(255,255,255,.04)',
                          padding: '0px 4px', borderRadius: 3, marginRight: 4,
                        }}>({acctLogin})</span>
                      )}
                    </>
                  )}
                  <span className="feed-text">{f.text}</span>
                  {f.confidence > 0 && <span className="feed-conf">置信{f.confidence}%</span>}
                </div>
              )
            })}
          </div>
        </div>
      </div>
  )
}
