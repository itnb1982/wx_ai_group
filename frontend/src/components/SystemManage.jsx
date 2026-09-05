import { useEffect, useState, useCallback, useRef } from 'react'
import { localModelStatus, localModelWarm, localModelSelftest, cloudStatus, cloudToggle, visionStatus } from '../services/api'
import { MODEL_LINEUP } from '../brand/identity.js'
import VisionModelPanel from './VisionModelPanel'

/**
 * 系统管理 · 本地双核运行台
 *
 * 这个页面回答客户三个问题：
 *   1. 我买的「四模型」现在到底有几个真的在干活？
 *   2. 本地那个 Qwen3-8B 具体在干什么？它查出过问题吗？
 *   3. 它现在能用吗？——给我一个按钮当场验证，而不是让我猜。
 *
 * 设计立场：宁可显示「未启用 + 怎么装」，也不显示一个含糊的绿灯。
 * 一个从没被调用过的模型被画成绿色，是最坏的一种可视化。
 */

// ─────────────────────────────────────────────────────────────
// 状态色：只有"确实在岗"才给绿，其余一律灰/黄/红，不搞模糊地带
// ─────────────────────────────────────────────────────────────
const TONE = {
  在岗:   { c: '#2ee6a0', bg: 'rgba(46,230,160,.10)', bd: 'rgba(46,230,160,.35)' },
  未启用: { c: '#5b6e91', bg: 'rgba(91,110,145,.10)', bd: 'rgba(91,110,145,.30)' },
  // 懒加载尚未触发 ≠ 故障。缺这一档会 fallback 成红色「不可用」，
  // 让运维在系统完全正常时跑去排查一个不存在的问题。
  待唤醒: { c: '#4D9BFF', bg: 'rgba(77,155,255,.10)', bd: 'rgba(77,155,255,.32)' },
  缺模型: { c: '#ffcf4d', bg: 'rgba(255,207,77,.10)', bd: 'rgba(255,207,77,.35)' },
  已降级: { c: '#ffcf4d', bg: 'rgba(255,207,77,.10)', bd: 'rgba(255,207,77,.35)' },
  已手动关闭: { c: '#5b6e91', bg: 'rgba(91,110,145,.10)', bd: 'rgba(91,110,145,.30)' },
  不可用: { c: '#ff5c6c', bg: 'rgba(255,92,108,.10)', bd: 'rgba(255,92,108,.35)' },
}
const toneOf = (h) => TONE[h] || TONE['不可用']

const fmtMs = (v) => (v === null || v === undefined ? '—' : `${Math.round(v)} ms`)
const fmtAgo = (s) => {
  if (s === null || s === undefined) return '从未'
  if (s < 60) return `${Math.round(s)} 秒前`
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`
  return `${(s / 3600).toFixed(1)} 小时前`
}

function Dot({ ok, warn }) {
  const c = ok ? '#2ee6a0' : warn ? '#ffcf4d' : '#5b6e91'
  return (
    <span
      style={{
        display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
        background: c, boxShadow: ok ? `0 0 8px ${c}` : 'none', flex: '0 0 auto',
      }}
    />
  )
}

function Stat({ label, value, unit, accent }) {
  return (
    <div style={{ flex: '1 1 110px', minWidth: 110 }}>
      <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 4, letterSpacing: '.03em' }}>{label}</div>
      <div style={{ fontFamily: 'var(--font-num)', fontSize: 20, color: accent || 'var(--txt)', lineHeight: 1.1 }}>
        {value}
        {unit && <span style={{ fontSize: 11, color: 'var(--dim)', marginLeft: 3 }}>{unit}</span>}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// 四模型阵容条
// ─────────────────────────────────────────────────────────────
function LineupBar({ data }) {
  const s = data?.summary || {}
  const cloud = data?.cloud || {}
  const qwenOk = !!data?.qwen?.available
  const chronosOk = !!data?.chronos?.available

  const n = s.model_ready_count ?? 0
  const total = s.model_total ?? 4

  // 三态：在岗(绿) / 待命(蓝，正常——冷启动或当前无交易触发调用) / 故障(红)
  // 关键：绝不允许把「待命」画成红色故障。最典型场景是后端重启后计数器清零 +
  // 当前休市无决策，云端双脑根本没被调用——此时系统完全健康，开盘即恢复。
  const STATE_META = {
    active: { label: '在岗', ok: true, color: '#2ee6a0' },
    idle:   { label: '待命', ok: false, warn: true, color: '#4D9BFF' },
    down:   { label: '故障', ok: false, warn: false, color: '#ff5c6c' },
  }
  const stateOf = (key) => {
    if (key === 'qwen') return qwenOk ? 'active' : 'idle'
    if (key === 'chronos') return chronosOk ? 'active' : 'idle'
    const c = (cloud[key] || {})
    if (c.down) return 'down'
    if (c.ready) return 'active'
    // 后端已下发 activation 明确区分 idle / down；兜底按「待命」而非「故障」处理，避免误红
    if (c.activation === 'down') return 'down'
    return 'idle'
  }

  return (
    <div className="panel" style={{ padding: 18, marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8, marginBottom: 14 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--txt)' }}>模型阵容</div>
          {/* 副文由后端下发。前端自己数模型个数迟早会跟状态页对不上。 */}
          <div style={{ fontSize: 12, color: 'var(--sub)', marginTop: 3 }}>{s.tagline || '—'}</div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <span style={{ fontFamily: 'var(--font-num)', fontSize: 26, color: n >= total ? 'var(--green)' : 'var(--gold)' }}>{n}</span>
          <span style={{ fontFamily: 'var(--font-num)', fontSize: 15, color: 'var(--dim)' }}>/{total}</span>
          <div style={{ fontSize: 11, color: 'var(--dim)' }}>在岗模型</div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(180px,1fr))', gap: 10 }}>
        {MODEL_LINEUP.map((m) => {
          const st = STATE_META[stateOf(m.key)] || STATE_META.idle
          const ok = st.ok
          return (
            <div
              key={m.key}
              style={{
                border: `1px solid ${ok ? 'rgba(46,230,160,.30)' : st.warn ? 'rgba(77,155,255,.30)' : 'rgba(255,92,108,.30)'}`,
                background: ok ? 'rgba(46,230,160,.05)' : st.warn ? 'rgba(77,155,255,.05)' : 'rgba(255,92,108,.04)',
                borderRadius: 10, padding: '10px 12px',
              }}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 4 }}>
                <Dot ok={ok} warn={st.warn} />
                <span style={{ fontSize: 13, fontWeight: 600, color: ok ? 'var(--txt)' : 'var(--sub)' }}>{m.label}</span>
              </div>
              <div style={{ fontSize: 11, color: 'var(--dim)', lineHeight: 1.5 }}>{m.role}</div>
              <div style={{ fontSize: 10, color: st.color, marginTop: 4 }}>
                {st.label} · {m.tier}
              </div>
            </div>
          )
        })}
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// Qwen3-8B 工作台
// ─────────────────────────────────────────────────────────────
function QwenPanel({ q, onWarm, onSelftest, warming, testing, testResult, cloudOff = false }) {
  const t = toneOf(q?.headline)
  const roles = q?.roles || {}
  const pr = roles.proofreader || {}
  const cp = roles.copilot || {}
  // ★ 2026-08-19 定稿P0：qwen3:8b 已移出方向投票（brain_audit 实证 2979 次调用 0 次过锁），
  //   专职校验层——校对员(结构闸) + 仓位管理 + L2 反向平仓。不再是"方向主脑/降级副驾"。
  const role2Title = '角色二 · 校验层（校对/仓管/L2平仓）'
  const role2Color = 'var(--purple)'
  const role2Desc = (
    <>
      <b style={{ color: 'var(--sub)' }}>已移出方向投票（0.15 权重空转实锤），专职本地校验。</b>
      ① 校对员：每笔非 HOLD 决策落地前做结构校验；② 仓位管理：持仓判断（异步 15s 节流）；
      ③ L2 反向平仓：方向反转置信 ≥0.60 连续 2 轮触发。这里的计数<b style={{ color: 'var(--sub)' }}>持续增长才是正常</b>。
    </>
  )

  return (
    <div className="panel" style={{ padding: 18 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--txt)' }}>Qwen3-8B</span>
        <span
          style={{
            fontSize: 11, padding: '2px 9px', borderRadius: 20,
            color: t.c, background: t.bg, border: `1px solid ${t.bd}`,
          }}
        >
          {q?.headline || '读取中'}
        </span>
        <span style={{ fontSize: 11, color: 'var(--dim)', fontFamily: 'var(--font-num)' }}>{q?.model || 'qwen3:8b'}</span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--sub)', marginBottom: 14, lineHeight: 1.6 }}>{q?.hint || ''}</div>

      {/* ── 双角色 ── */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: 10, marginBottom: 14 }}>
        <div style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 12, background: 'rgba(77,155,255,.04)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 6 }}>
            <Dot ok={!!pr.active} />
            <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--blue)' }}>角色一 · 决策校对员</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--dim)', lineHeight: 1.6, marginBottom: 10 }}>
            常态在岗。每笔非 HOLD 决策落地前做一次结构校验：止损方向是否挂反、
            理由与方向是否自相矛盾、价格是否为幻觉数字。
            <b style={{ color: 'var(--sub)' }}> 只报告，不改方向、不投票。</b>
          </div>
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
            <Stat label="累计校对" value={pr.runs ?? 0} unit="次" />
            <Stat label="查出问题" value={pr.issues ?? 0} unit="次" accent={(pr.issues ?? 0) > 0 ? 'var(--gold)' : undefined} />
          </div>
        </div>

        <div style={{ border: '1px solid var(--line)', borderRadius: 10, padding: 12, background: 'rgba(176,123,255,.04)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 6 }}>
            {/* 关云后它是常态主脑：有票即应亮绿灯，而不是恒灰的"待降级"状态 */}
            <Dot ok={cloudOff ? (cp.runs ?? 0) > 0 : false} warn={!cloudOff && (cp.runs ?? 0) > 0} />
            <span style={{ fontSize: 13, fontWeight: 600, color: role2Color }}>{role2Title}</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--dim)', lineHeight: 1.6, marginBottom: 10 }}>
            {role2Desc}
          </div>
          <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
            <Stat label={cloudOff ? '出票' : '被征询'} value={cp.runs ?? 0} unit="次" />
            <Stat label="放行开仓" value={cp.allowed ?? 0} unit="次" accent={(cp.allowed ?? 0) > 0 ? 'var(--purple)' : undefined} />
          </div>
        </div>
      </div>

      {/* ── 运行指标 ── */}
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', padding: '12px 0', borderTop: '1px solid var(--line)', borderBottom: '1px solid var(--line)', marginBottom: 14 }}>
        <Stat label="时延 P50" value={fmtMs(q?.latency_p50_ms)} />
        <Stat label="时延 P95" value={fmtMs(q?.latency_p95_ms)} />
        <Stat label="最后活动" value={fmtAgo(q?.last_activity_ago_s)} />
      </div>
      {q?.last_activity && (
        <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 14 }}>
          最近一次：<span style={{ color: 'var(--sub)' }}>{q.last_activity}</span>
        </div>
      )}

      {/* ── 操作 ── */}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
        <button className="btn" onClick={onWarm} disabled={warming || testing}>
          {warming ? '预热中…' : '预热模型'}
        </button>
        <button className="btn btn-primary" onClick={onSelftest} disabled={warming || testing}>
          {testing ? '自检中…' : '端到端自检'}
        </button>
      </div>
      <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 8, lineHeight: 1.6 }}>
        预热 = 提前把 5GB 权重装进显存，避免降级那一刻才现加载。
        自检 = 喂一个<b style={{ color: 'var(--sub)' }}>故意写错</b>的决策（BUY 但止损挂在入场价上方），
        校对员能揪出来才算真的在工作 —— 喂正确样本得到"没问题"是分不清它到底查没查的。
      </div>

      {testResult && (
        <div
          style={{
            marginTop: 12, padding: 12, borderRadius: 10,
            border: `1px solid ${testResult.passed ? 'rgba(46,230,160,.35)' : 'rgba(255,92,108,.35)'}`,
            background: testResult.passed ? 'rgba(46,230,160,.06)' : 'rgba(255,92,108,.06)',
          }}
        >
          <div style={{ fontSize: 13, fontWeight: 600, color: testResult.passed ? 'var(--green)' : 'var(--red)', marginBottom: 6 }}>
            {testResult.passed ? '✓ 自检通过' : '✕ 自检未通过'}
          </div>
          <div style={{ fontSize: 12, color: 'var(--sub)', lineHeight: 1.6 }}>{testResult.message}</div>
          {Array.isArray(testResult.issues) && testResult.issues.length > 0 && (
            <ul style={{ margin: '8px 0 0', paddingLeft: 18, fontSize: 12, color: 'var(--sub)', lineHeight: 1.7 }}>
              {testResult.issues.map((it, i) => <li key={i}>{it}</li>)}
            </ul>
          )}
          {testResult.latency_ms != null && (
            <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 8, fontFamily: 'var(--font-num)' }}>
              耗时 {fmtMs(testResult.latency_ms)}
            </div>
          )}
        </div>
      )}

      {/* ── 未启用时的部署指引 ── */}
      {!q?.available && q?.enabled !== false && (
        <div style={{ marginTop: 14, padding: 12, borderRadius: 10, border: '1px dashed var(--line)', background: 'rgba(255,255,255,.02)' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--gold)', marginBottom: 8 }}>如何启用本地校对员</div>
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: 'var(--sub)', lineHeight: 1.9 }}>
            <li>安装 Ollama（ollama.com，Windows 一键安装包）</li>
            <li>命令行执行 <code style={{ fontFamily: 'var(--font-num)', color: 'var(--blue)' }}>ollama pull qwen3:8b</code>（约 5GB）</li>
            <li>回到本页点「预热模型」，状态变为「在岗」即接入完成</li>
          </ol>
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 8, lineHeight: 1.6 }}>
            不装也能正常交易 —— 本地模型是增强项不是依赖项，缺席时系统按三模型运行，
            只是少了一层落单前的结构校验。显存建议 ≥ 6GB 可用。
          </div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// Chronos-2 面板
// ─────────────────────────────────────────────────────────────
function ChronosPanel({ c }) {
  const t = toneOf(c?.headline)
  return (
    <div className="panel" style={{ padding: 18 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 4, flexWrap: 'wrap' }}>
        <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--txt)' }}>Chronos-2 120M</span>
        <span style={{ fontSize: 11, padding: '2px 9px', borderRadius: 20, color: t.c, background: t.bg, border: `1px solid ${t.bd}` }}>
          {c?.headline || '读取中'}
        </span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--sub)', marginBottom: 14, lineHeight: 1.6 }}>{c?.hint || ''}</div>

      {/* 字段必须与后端 chronos_service.status 严格对齐。
          此处曾用 c?.predict_count / c?.calls / c?.covariates —— 后端从不返回这三个键，
          于是「预测次数」恒显示 —，而「协变量」在取不到时回落成硬编码的 3 路，
          等于模型没用协变量却对外宣称用了 3 路，正是品牌虚标红线要禁的事。 */}
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', paddingTop: 12, borderTop: '1px solid var(--line)' }}>
        <Stat label="设备" value={c?.device || '—'} />
        <Stat label="成功预测" value={c?.calls_ok ?? '—'} unit="次" />
        <Stat label="协变量" value={c?.last_covariates?.length ?? 0} unit="路" />
        {c?.last_ok_ago_s != null && <Stat label="最近一次" value={Math.round(c.last_ok_ago_s)} unit="秒前" />}
        {c?.last_latency_ms ? <Stat label="推理耗时" value={c.last_latency_ms} unit="ms" /> : null}
      </div>

      {!c?.available && (
        <div style={{ marginTop: 14, padding: 12, borderRadius: 10, border: '1px dashed var(--line)', background: 'rgba(255,255,255,.02)' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--gold)', marginBottom: 6 }}>当前处于降级状态</div>
          <div style={{ fontSize: 12, color: 'var(--sub)', lineHeight: 1.7 }}>
            原因：<span style={{ color: 'var(--dim)' }}>{c?.load_error || c?.reason || '未知'}</span>
          </div>
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 8, lineHeight: 1.6 }}>
            最常见的原因是重型依赖未安装。系统已用子进程探针把 torch 的原生崩溃隔离成"自动降级"，
            <b style={{ color: 'var(--sub)' }}>不会拖垮交易</b>（决策回退 SMC 订单流 + 体制感知）。
            如需恢复，在 <b style={{ color: 'var(--sub)' }}>PowerShell 或 cmd</b> 中执行
            <code style={{ fontFamily: 'var(--font-num)', color: 'var(--blue)' }}> pip install -r backend/requirements-ml.txt</code>。
            <br />
            ⚠ 切勿在 Git Bash / MSYS 终端下启动后端：该环境会让
            <code style={{ fontFamily: 'var(--font-num)' }}> import torch </code>
            必定触发 0xC0000005 原生崩溃（与 ABI 无关，纯属终端环境冲突），
            表现就是本面板恒显示降级。
          </div>
        </div>
      )}
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────
// 本机推理运行时（GPU 显存 + 双 Ollama 实例）
//
// ★ 2026-08-18 新增。关云之后，本机这两张 3060 Ti 就是系统唯一的生命线：
//   · 实例掉线 → 方向票不足 2 张 → 强制 HOLD → 直接停止交易
//   · 显存被占满 → OOM 把模型踢出 VRAM → 下一轮同样缺票
//   在此之前这两件事在面板上完全看不见，只能靠人蹲命令行敲 nvidia-smi。
//   把它画出来，等于把系统最脆弱的那根弦从黑箱里搬到台面上。
//
//   配色语义：绿=正常 / 黄=偏紧 / 红=危险。此处是资源水位而非行情涨跌，
//   不适用「红涨绿跌」的金融配色约定。
// ─────────────────────────────────────────────────────────────
const PRESSURE_C = {
  normal: { c: '#2ee6a0', bg: 'rgba(46,230,160,.10)', bd: 'rgba(46,230,160,.30)', label: '正常' },
  tight: { c: '#ffcf4d', bg: 'rgba(255,207,77,.10)', bd: 'rgba(255,207,77,.32)', label: '偏紧' },
  critical: { c: '#ff5c6c', bg: 'rgba(255,92,108,.10)', bd: 'rgba(255,92,108,.32)', label: '危险' },
}

function MemBar({ pct, tone }) {
  const w = Math.max(0, Math.min(100, Number(pct) || 0))
  return (
    <div style={{ height: 7, borderRadius: 4, background: 'rgba(255,255,255,.06)', overflow: 'hidden', marginTop: 6 }}>
      <div style={{ width: `${w}%`, height: '100%', background: tone.c, borderRadius: 4, transition: 'width .4s ease' }} />
    </div>
  )
}

function RuntimeHealthPanel({ rt, alert, alertLevel, ctx }) {
  // 探测失败时不猜、不画假绿灯 —— 保留「未知」比编一个正常状态诚实。
  const gpu = rt?.gpu || {}
  const ol = rt?.ollama || {}
  const gpus = Array.isArray(gpu.gpus) ? gpu.gpus : []
  const insts = Array.isArray(ol.instances) ? ol.instances : []
  const leaks = Array.isArray(rt?.leaks) ? rt.leaks : []
  // 告警级别由后端下发。前端绝不靠文案前缀（如 '⚠'）反推颜色——
  // 那种隐式约定一旦有人改文案就静默失效，红灯该亮时不亮。
  const alertTone = alertLevel === 'crit' ? PRESSURE_C.critical
    : alertLevel === 'warn' ? PRESSURE_C.tight
      : PRESSURE_C.normal

  return (
    <div className="panel" style={{ padding: 18, marginBottom: 16 }}>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8, marginBottom: 12 }}>
        <div>
          <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--txt)' }}>🖥 本机推理运行时</div>
          <div style={{ fontSize: 12, color: 'var(--sub)', marginTop: 3 }}>
            双 Ollama 实例各独占一张 3060 Ti · 显存水位与模型常驻情况直接决定能否出票
          </div>
        </div>
        <div style={{ textAlign: 'right' }}>
          <span style={{ fontFamily: 'var(--font-num)', fontSize: 24, color: ol.all_up ? 'var(--green)' : 'var(--red)' }}>
            {ol.up_count ?? '—'}
          </span>
          <span style={{ fontFamily: 'var(--font-num)', fontSize: 14, color: 'var(--dim)' }}>/{ol.total ?? 2}</span>
          <div style={{ fontSize: 11, color: 'var(--dim)' }}>实例存活 · {ol.resident_count ?? 0} 个模型常驻</div>
        </div>
      </div>

      {alert && (
        <div style={{
          padding: '8px 10px', marginBottom: 12, borderRadius: 8, fontSize: 11.5, lineHeight: 1.6,
          color: alertTone.c, background: alertTone.bg, border: `1px solid ${alertTone.bd}`,
        }}>
          {alert}
        </div>
      )}

      {/* ── GPU 显存水位 ── */}
      {gpu.available ? (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: 10 }}>
          {gpus.map((g) => {
            const tone = PRESSURE_C[g.pressure] || PRESSURE_C.normal
            return (
              <div key={g.cuda_index} style={{
                border: `1px solid ${tone.bd}`, background: tone.bg, borderRadius: 10, padding: '10px 12px',
              }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                  {/* 编号一律用 Windows 任务管理器视角标注：用户是对着任务管理器看的，
                      标 CUDA 序号会让人对不上号（核显把 Windows 序号顶掉了一位）。 */}
                  <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--txt)' }}>
                    {g.windows_label} <span style={{ fontSize: 10, color: 'var(--dim)', fontWeight: 400 }}>(CUDA{g.cuda_index})</span>
                  </span>
                  <span style={{ fontSize: 10, color: tone.c }}>{tone.label}</span>
                </div>
                <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 3 }}>{g.role}</div>
                <MemBar pct={g.mem_used_pct} tone={tone} />
                <div style={{ display: 'flex', justifyContent: 'space-between', marginTop: 5, fontSize: 10.5, color: 'var(--dim)', fontFamily: 'var(--font-num)' }}>
                  <span style={{ color: tone.c }}>{g.mem_used_mb} / {g.mem_total_mb} MB · {g.mem_used_pct}%</span>
                  <span>空闲 {g.mem_free_mb}MB · {g.util_pct}% · {g.temp_c}℃</span>
                </div>
                {/* 显存对账：显卡上的推理进程数 vs 实例声明的常驻模型数。
                    对不上就是僵尸 runner 在白吃显存——不报错、不掉线，
                    只是余量被悄悄啃掉，等到要升级模型时才突然 OOM。 */}
                {(() => {
                  const lk = leaks.find((x) => x.cuda_index === g.cuda_index)
                  if (!lk) return null
                  return (
                    <div style={{
                      marginTop: 6, fontSize: 10, lineHeight: 1.55, color: PRESSURE_C.tight.c,
                      padding: '5px 7px', borderRadius: 6,
                      background: PRESSURE_C.tight.bg, border: `1px solid ${PRESSURE_C.tight.bd}`,
                    }}>
                      对账不平：{lk.runner_count} 个推理进程 / {lk.model_count} 个常驻模型 ·
                      约 <b>{lk.unaccounted_mb}MB</b> 无人认领（疑似僵尸 runner，重启该实例可回收）
                    </div>
                  )
                })()}
              </div>
            )
          })}
        </div>
      ) : (
        <div style={{ fontSize: 11.5, color: 'var(--dim)', padding: '8px 0' }}>
          显存水位不可读：{gpu.reason || '未知原因'}
        </div>
      )}

      {/* ── 双实例常驻明细 ── */}
      <div style={{ marginTop: 12, display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(280px,1fr))', gap: 10 }}>
        {insts.map((i) => {
          const ok = !!i.http_ok
          const resident = i.headline === '常驻'
          return (
            <div key={i.key} style={{
              border: '1px solid var(--line)', borderRadius: 10, padding: '10px 12px',
              background: 'rgba(255,255,255,.02)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginBottom: 4 }}>
                <Dot ok={ok && resident} warn={ok && !resident} />
                <span style={{ fontSize: 12.5, fontWeight: 600, color: ok ? 'var(--txt)' : 'var(--red)' }}>
                  :{i.port}
                </span>
                <span style={{ fontSize: 10.5, color: 'var(--dim)' }}>{i.role}</span>
                <span style={{ marginLeft: 'auto', fontSize: 10, color: resident ? 'var(--green)' : ok ? '#ffcf4d' : 'var(--red)' }}>
                  {i.headline}
                </span>
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--dim)', lineHeight: 1.6 }}>{i.hint}</div>
              {(i.resident || []).map((m) => (
                <div key={m.model} style={{ fontSize: 10, color: 'var(--sub)', fontFamily: 'var(--font-num)', marginTop: 3 }}>
                  {m.model} · {m.quant || '?'} · {m.params || '?'} · VRAM {m.vram_mb}MB · ctx {m.ctx ?? '?'}
                </div>
              ))}
              <div style={{ fontSize: 10, color: 'var(--dim)', marginTop: 4, fontFamily: 'var(--font-num)' }}>
                探针 {i.status_code ?? '—'} · {fmtMs(i.latency_ms)}
              </div>
            </div>
          )
        })}
      </div>

      {/* ── 上下文档位：显存余量换来的「视野宽度」 ──
          这不是装饰指标。num_ctx 决定一次能喂给方向主脑多少市场信息
          （多周期 K 线 + 订单流 + 新闻 + 持仓上下文）。档位被压回基线，
          意味着模型正在「看不全」的状态下投票。 */}
      {ctx && (
        <div style={{
          marginTop: 12, padding: '10px 12px', borderRadius: 10,
          border: `1px solid ${ctx.expanded_active ? PRESSURE_C.normal.bd : PRESSURE_C.tight.bd}`,
          background: ctx.expanded_active ? PRESSURE_C.normal.bg : PRESSURE_C.tight.bg,
        }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 11.5, color: 'var(--sub)' }}>方向主脑上下文</span>
            <span style={{
              fontFamily: 'var(--font-num)', fontSize: 16, fontWeight: 600,
              color: ctx.expanded_active ? PRESSURE_C.normal.c : PRESSURE_C.tight.c,
            }}>
              {ctx.num_ctx ?? '—'}
            </span>
            <span style={{ fontSize: 10.5, color: 'var(--dim)' }}>
              tokens · 基线 {ctx.baseline} / 扩展 {ctx.expanded}
            </span>
            <span style={{
              marginLeft: 'auto', fontSize: 10, padding: '2px 8px', borderRadius: 999,
              color: ctx.expanded_active ? PRESSURE_C.normal.c : PRESSURE_C.tight.c,
              border: `1px solid ${ctx.expanded_active ? PRESSURE_C.normal.bd : PRESSURE_C.tight.bd}`,
            }}>
              {ctx.expanded_active ? '扩展档 · 视野完整' : '基线档 · 视野受限'}
            </span>
          </div>
          <div style={{ fontSize: 10.5, color: 'var(--dim)', marginTop: 5, lineHeight: 1.6 }}>
            {ctx.reason || `需空闲显存 ≥ ${ctx.require_free_mb}MB 才自动升档`}
          </div>
        </div>
      )}
    </div>
  )
}

export default function SystemManage() {
  const [data, setData] = useState(null)
  const [err, setErr] = useState('')
  const [loading, setLoading] = useState(true)
  const [warming, setWarming] = useState(false)
  const [testing, setTesting] = useState(false)
  const [testResult, setTestResult] = useState(null)
  const [toast, setToast] = useState('')
  const [cloud, setCloud] = useState(null)
  const [cloudBusy, setCloudBusy] = useState(false)
  const [vision, setVision] = useState(null)
  const alive = useRef(true)

  const load = useCallback(async () => {
    try {
      const [d, c, vs] = await Promise.all([
        localModelStatus().catch(() => null),
        cloudStatus().catch(() => null),
        visionStatus().catch(() => null),
      ])
      if (!alive.current) return
      setData(d)
      setCloud(c)
      setVision(vs)
      setErr('')
    } catch (e) {
      if (e.name !== 'AuthError' && alive.current) setErr(e.message || '读取失败')
    } finally {
      if (alive.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    alive.current = true
    let timer = null
    const tick = async () => {
      await load()
      if (alive.current) timer = setTimeout(tick, 8000)
    }
    tick()
    return () => { alive.current = false; if (timer) clearTimeout(timer) }
  }, [load])

  const onWarm = async () => {
    setWarming(true); setToast('')
    try {
      const r = await localModelWarm()
      setToast(r.ok ? `预热成功，耗时 ${Math.round(r.latency_ms || 0)} ms` : `预热失败：${r.detail || r.message}`)
      load()
    } catch (e) {
      setToast(`预热异常：${e.message}`)
    } finally {
      setWarming(false)
    }
  }

  const onSelftest = async () => {
    setTesting(true); setTestResult(null); setToast('')
    try {
      const r = await localModelSelftest()
      setTestResult(r)
      load()
    } catch (e) {
      setTestResult({ passed: false, message: `自检异常：${e.message}` })
    } finally {
      setTesting(false)
    }
  }

  const onCloudToggle = async () => {
    if (!cloud) return
    setCloudBusy(true)
    try {
      const c = await cloudToggle(!cloud.master_enabled)
      setCloud(c)
    } catch (e) {
      setToast(`云模型开关切换失败：${e.message}`)
    } finally {
      setCloudBusy(false)
    }
  }

  // 运行模式判定统一在此取一次：优先信云开关端点（更实时），
  // 取不到再退回 status 下发的 cloud_enabled。两处口径必须同源，
  // 否则会出现"标题说三脑、卡片说副驾"的自打脸。
  const cloudOff = cloud ? !cloud.effective_enabled : (data?.summary?.cloud_enabled === false)

  return (
    <div style={{ padding: '22px 26px', maxWidth: 1400, margin: '0 auto' }}>
      <div style={{ marginBottom: 18 }}>
        <h2 style={{ margin: 0, fontSize: 20, fontWeight: 600, color: 'var(--txt)' }}>系统管理</h2>
        <div style={{ fontSize: 12, color: 'var(--sub)', marginTop: 5 }}>
          {/* 「双核」是关云前的旧口径（qwen+chronos）。视觉票已是决策链正式一员，
              关云后本地是三脑，标题写死双核等于当着客户少报一个模型。 */}
          {cloudOff ? '本地三脑运行台' : '本地模型运行台'} —— 查看在岗状态、实际工作量与本机显存余量，并可当场验证其可用性
        </div>
      </div>

      {err && (
        <div style={{ padding: 12, marginBottom: 14, borderRadius: 10, border: '1px solid rgba(255,92,108,.35)', background: 'rgba(255,92,108,.07)', color: 'var(--red)', fontSize: 13 }}>
          {err}
        </div>
      )}
      {toast && (
        <div style={{ padding: 10, marginBottom: 14, borderRadius: 10, border: '1px solid var(--line)', background: 'rgba(255,255,255,.03)', color: 'var(--sub)', fontSize: 12 }}>
          {toast}
        </div>
      )}

      {loading && !data ? (
        <div style={{ padding: 40, textAlign: 'center', color: 'var(--dim)', fontSize: 13 }}>读取本地模型状态…</div>
      ) : (
        <>
          {/* ── 云模型总开关（客户自选） ── */}
          {cloud && (
            <div className="panel" style={{
              padding: 18, marginBottom: 16,
              borderColor: cloud.effective_enabled ? 'rgba(79,140,255,.4)' : 'rgba(46,230,160,.4)',
            }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
                <div style={{ flex: '1 1 auto', minWidth: 260 }}>
                  <div style={{ fontSize: 15, fontWeight: 600, color: 'var(--txt)' }}>☁️ 云模型总开关（已冻结）</div>
                  <div style={{ fontSize: 13, color: '#2ee6a0', fontWeight: 700, marginTop: 4 }}>
                    纯本地运行 · 云端方案已永久弃用
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 4, lineHeight: 1.6 }}>
                    2026-08-19 用户决策：云端 DS/HY 永久弃用，决策链 100% 本地模型（视觉 + Chronos 锚 + qwen3:8b 校验）。此开关已冻结，不可再开启云端。
                  </div>
                </div>
                <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 6 }}>
                  <button
                    className={'btn'}
                    disabled
                    style={{ minWidth: 150, opacity: .6, cursor: 'not-allowed' }}
                    title="云端方案已永久弃用，此开关已冻结"
                  >
                    🔒 已冻结 · 纯本地
                  </button>
                  <span style={{ fontSize: 10, color: 'var(--dim)' }}>
                    决策链：视觉 + Chronos 锚 + 本地加权 · 校验：qwen3:8b
                  </span>
                </div>
              </div>
              <div style={{ display: 'flex', gap: 10, marginTop: 12, flexWrap: 'wrap' }}>
                {Object.entries(cloud.providers || {}).map(([p, info]) => (
                  <span key={p} style={{
                    fontSize: 11, padding: '3px 10px', borderRadius: 20,
                    color: info.available ? 'var(--blue)' : 'var(--dim)',
                    background: info.available ? 'rgba(79,140,255,.1)' : 'rgba(255,255,255,.04)',
                    border: '1px solid ' + (info.available ? 'rgba(79,140,255,.3)' : 'var(--line)'),
                  }}>
                    {p === 'deepseek' ? 'DeepSeek' : '混元'} · {info.available ? info.source : '无可用 Key'}
                  </span>
                ))}
              </div>
              {cloud.master_enabled && !cloud.effective_enabled && (
                <div style={{
                  marginTop: 10, fontSize: 11, color: '#ffcf4d', padding: '8px 10px', borderRadius: 8,
                  background: 'rgba(255,207,77,.08)', border: '1px solid rgba(255,207,77,.3)', lineHeight: 1.6,
                }}>
                  ℹ 云端方案已永久弃用（2026-08-19 决策），系统以<b>全本地模式</b>运行，决策链 100% 本地模型，零云成本。
                </div>
              )}
            </div>
          )}

          <LineupBar data={data} />
          {/* 运行时健康紧跟阵容条：阵容说"谁在岗"，运行时说"还撑得住吗"。
              后者是关云后的生命线，位置必须在各模型详情卡之前。 */}
          <RuntimeHealthPanel
            rt={data?.runtime}
            alert={data?.summary?.runtime_alert}
            alertLevel={data?.summary?.runtime_alert_level}
            ctx={data?.qwen?.num_ctx_detail}
          />
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(420px,1fr))', gap: 16 }}>
            <QwenPanel
              q={data?.qwen}
              onWarm={onWarm}
              onSelftest={onSelftest}
              warming={warming}
              testing={testing}
              testResult={testResult}
              cloudOff={cloudOff}
            />
            <ChronosPanel c={data?.chronos} />
            <VisionModelPanel data={vision} mode="status" />
          </div>

          {/* ── 平仓通道架构（2026-08-19 定稿：全本地 8 通道） ── */}
          <div className="panel" style={{ padding: 18, marginTop: 16 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 }}>
              <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--txt)' }}>🛡 平仓通道架构（全本地 8 通道）</span>
              <span style={{ fontSize: 10.5, padding: '2px 8px', borderRadius: 20, color: '#2ee6a0', background: 'rgba(46,230,160,.10)', border: '1px solid rgba(46,230,160,.30)' }}>
                云端弃用 · 全部本地/规则
              </span>
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(300px,1fr))', gap: 10 }}>
              {[
                { ch: 'L1 硬 SL/TP', mgr: '规则 (MT5 原生)', trig: '开仓即设 · 实时', st: '✅', note: '不可跳过地板' },
                { ch: 'L2 AI 反向平仓', mgr: 'Qwen3-8B (本地)', trig: '反向置信≥0.60 连续 2 轮', st: '✅', note: '已本地化(P0-2)' },
                { ch: 'L3 篮子护盾', mgr: '规则', trig: '篮子浮盈≥$100 + 120s', st: '✅', note: '锁利兜底' },
                { ch: 'smart_exit 规则', mgr: '规则引擎', trig: 'TP阶梯 / 追踪 / 回吐', st: '✅', note: 'LLM 异常时兜底' },
                { ch: '机械反向止损', mgr: '规则', trig: '反向破位确认', st: '✅', note: '硬护栏' },
                { ch: '视觉离场看护', mgr: 'Qwen2.5-VL 7B', trig: '结构转弱 conf≥0.60', st: '✅', note: '30s 刷新 / 300s 过期' },
                { ch: '仓位管理', mgr: 'Qwen3-8B (本地)', trig: '持仓判断 · 异步 15s 节流', st: '✅', note: '不阻塞主循环' },
                { ch: '人工紧急处置 L-M', mgr: '运维人工', trig: 'MANUAL_HALT 期间', st: '✅', note: '系统永不自动触发' },
              ].map((x) => (
                <div key={x.ch} style={{ border: '1px solid var(--line)', borderRadius: 8, padding: '9px 12px', background: 'rgba(46,230,160,.03)' }}>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 3 }}>
                    <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--txt)' }}>{x.ch}</span>
                    <span style={{ marginLeft: 'auto', fontSize: 13 }}>{x.st}</span>
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--sub)' }}>管理：{x.mgr}</div>
                  <div style={{ fontSize: 10.5, color: 'var(--dim)', lineHeight: 1.5, marginTop: 2 }}>{x.trig} {x.note ? `· ${x.note}` : ''}</div>
                </div>
              ))}
            </div>
          </div>
        </>
      )}
    </div>
  )
}
