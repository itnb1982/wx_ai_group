// 视觉第四票 · 知觉模型可视化面板（万象Ai · 2026-08-14）
// =====================================================================
// 把后台 vision_service 的「视觉模型第四票」工作状态直观呈现到三个页面：
//   · mode="compact"  → 仪表盘（紧跟 Meta 质量陪审团，紧凑在岗卡）
//   · mode="status"   → 系统管理（完整诊断：状态/H4·M15读/聚合/生产计数/错误）
//   · mode="signal"   → 信号源参考（作为第5路增强信号，附"仅参考不参与下单"红线）
//
// ★ 铁律（与系统定位一致）
//   1. 红涨绿跌：BUY→红涨↑ / SELL→绿跌↓ / HOLD→灰观望。
//   2. 状态色不苟且：只有"确实在岗且出过票"才给绿；未启用/未启动/读图中
//      一律灰或蓝；缺模型黄、运行异常红。绝不把"待命/未调用"画成绿。
//   3. 视觉票是增强信号（加法融合），不是 GO/NO-GO 闸门；结构上就标清楚
//      "提准非拦截"，避免用户误以为它在砍交易。
//   4. 字段严格对齐后端 VisionVote.as_dict() 与 vision_service.status()：
//      enabled / model / started / runs / ok_runs / last_err / last_primary /
//      vote{ available,direction,confidence,score,h4_dir,m15_dir,
//            h4_conf,m15_conf,agree,weight_scale,note,updated_at,latency_ms,model }
// =====================================================================
import { useState } from 'react'

const num = { fontFamily: 'var(--font-num)' }

// 方向 → 颜色 / 文案（中国习惯：红涨绿跌）
function dirStyle(dir) {
  if (dir === 'BUY') return { color: 'var(--red)', label: '看多 ↑' }
  if (dir === 'SELL') return { color: 'var(--green)', label: '看空 ↓' }
  if (dir === 'HOLD') return { color: 'var(--sub)', label: '观望 —' }
  return { color: 'var(--sub)', label: '—' }
}

// 状态色：只有"确实在岗"才给绿，其余一律灰/蓝/黄/红，不搞模糊地带
const TONE = {
  在岗:   { c: '#2ee6a0', bg: 'rgba(46,230,160,.10)', bd: 'rgba(46,230,160,.35)' },
  未启用: { c: '#5b6e91', bg: 'rgba(91,110,145,.10)', bd: 'rgba(91,110,145,.30)' },
  待唤醒: { c: '#4D9BFF', bg: 'rgba(77,155,255,.10)', bd: 'rgba(77,155,255,.32)' },
  缺模型: { c: '#ffcf4d', bg: 'rgba(255,207,77,.10)', bd: 'rgba(255,207,77,.35)' },
  不可用: { c: '#ff5c6c', bg: 'rgba(255,92,108,.10)', bd: 'rgba(255,92,108,.35)' },
}
const toneOf = (h) => TONE[h] || TONE['不可用']

const fmtAgo = (s) => {
  if (s === null || s === undefined) return '从未'
  if (s < 0) s = 0
  if (s < 60) return `${Math.round(s)} 秒前`
  if (s < 3600) return `${Math.round(s / 60)} 分钟前`
  return `${(s / 3600).toFixed(1)} 小时前`
}
const fmtMs = (v) => (v === null || v === undefined ? '—' : `${Math.round(v)} ms`)
const pct = (v) => `${Math.round((v || 0) * 100)}%`

// 推断服务整体状态 headline + tone key + 说明
function statusOf(d) {
  // 首屏尚未拿到数据：中性"读取中"，绝不画成红色"查询失败"误导用户
  if (!d) return { h: '读取中', t: '未启用', detail: '正在连接视觉服务…' }
  if (d.ok === false) return { h: '查询失败', t: '不可用', detail: d.error || '接口未返回' }
  if (d.enabled === false) return { h: '未启用', t: '未启用', detail: '视觉第四票开关关闭' }
  if (!d.started) return { h: '未启动', t: '待唤醒', detail: '生产者线程未启动' }
  const v = d.vote || {}
  if (v.available) return { h: '在岗', t: '在岗', detail: '视觉第四票实时输出中' }
  const err = d.last_err || ''
  if (err) {
    if (/not found|未找到|找不到|missing|no such/i.test(err)) return { h: '缺模型', t: '缺模型', detail: err }
    return { h: '运行异常', t: '不可用', detail: err }
  }
  return { h: '读图中', t: '待唤醒', detail: v.note || '首次渲染 / 行情准备中' }
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

function Badge({ t, text }) {
  const tone = toneOf(t)
  return (
    <span style={{
      fontSize: 11, padding: '2px 9px', borderRadius: 20,
      color: tone.c, background: tone.bg, border: `1px solid ${tone.bd}`,
    }}>
      {text}
    </span>
  )
}

// 方向强度条（红涨绿跌，与 FusionPanel 同构）
function ScoreBar({ score }) {
  const s = score || 0
  const posPct = Math.round((s + 1) * 50)
  return (
    <div style={{ position: 'relative', height: 8, background: 'var(--line)', borderRadius: 4, marginTop: 2 }}>
      <div style={{ position: 'absolute', left: '50%', top: -3, bottom: -3, width: 1, background: 'var(--dim)' }} />
      <div style={{
        position: 'absolute', top: 0, bottom: 0,
        left: s >= 0 ? '50%' : `${posPct}%`,
        width: `${Math.abs(s) * 50}%`,
        background: s >= 0 ? 'var(--red)' : 'var(--green)', borderRadius: 4,
      }} />
    </div>
  )
}

// H4 / M15 / M5 三帧结构读（三路并列；M5 仅在自身结构清晰时发声，模糊自动沉默）
function TimeframeReads({ v }) {
  const h4 = dirStyle(v.h4_dir)
  const m15 = dirStyle(v.m15_dir)
  const m5 = dirStyle(v.m5_dir)
  const m5Silent = !(v.m5_conf > 0)
  return (
    <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap' }}>
      <div style={{ flex: '1 1 130px' }}>
        <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 4 }}>H4 结构 / 趋势（权重 0.6）</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 14, fontWeight: 800, color: h4.color }}>{h4.label}</span>
          <span style={{ ...num, fontSize: 12, color: 'var(--sub)' }}>置信 {pct(v.h4_conf)}</span>
        </div>
      </div>
      <div style={{ flex: '1 1 130px' }}>
        <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 4 }}>M15 即时结构（权重 0.4）</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ fontSize: 14, fontWeight: 800, color: m15.color }}>{m15.label}</span>
          <span style={{ ...num, fontSize: 12, color: 'var(--sub)' }}>置信 {pct(v.m15_conf)}</span>
        </div>
      </div>
      <div style={{ flex: '1 1 130px' }}>
        <div style={{ fontSize: 11, color: 'var(--dim)', marginBottom: 4 }}>M5 入场结构（权重 0.2）</div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {m5Silent ? (
            <span style={{ fontSize: 12.5, fontWeight: 700, color: 'var(--dim)' }}>沉默（结构模糊）</span>
          ) : (
            <>
              <span style={{ fontSize: 14, fontWeight: 800, color: m5.color }}>{m5.label}</span>
              <span style={{ ...num, fontSize: 12, color: 'var(--sub)' }}>置信 {pct(v.m5_conf)}</span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ─────────────────────────────────────────────────────────────
// 主组件
// ─────────────────────────────────────────────────────────────
export default function VisionModelPanel({ data, mode = 'status' }) {
  const st = statusOf(data)
  const tone = toneOf(st.t)
  const v = (data && data.vote) || {}
  const fresh = v.updated_at ? Math.max(0, Date.now() / 1000 - v.updated_at) : null
  const stale = fresh != null && fresh > 900
  const ds = dirStyle(v.direction)
  const model = data?.model || v.model || 'qwen2.5vl:3b'

  // ── 紧凑模式：仪表盘 ──
  if (mode === 'compact') {
    return (
      <div className="panel" style={{ padding: 16 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--txt)' }}>视觉第四票 · 知觉模型</span>
          <Badge t={st.t} text={st.h} />
          <span style={{ ...num, fontSize: 11, color: 'var(--dim)', marginLeft: 'auto' }}>{model}</span>
        </div>

        {v.available ? (
          <>
            <TimeframeReads v={v} />
            <div style={{ marginTop: 12, display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
              <div>
                <div style={{ fontSize: 11, color: 'var(--dim)' }}>聚合方向</div>
                <div style={{ ...num, fontSize: 18, fontWeight: 900, color: ds.color }}>
                  {ds.label}
                  <span style={{ fontSize: 12, color: 'var(--sub)', marginLeft: 6, fontWeight: 600 }}>
                    强度 {v.score > 0 ? '+' : ''}{(v.score || 0).toFixed(2)} · 置信 {pct(v.confidence)}
                  </span>
                </div>
              </div>
              <div style={{ marginLeft: 'auto', textAlign: 'right' }}>
                <div style={{ fontSize: 11, color: 'var(--dim)' }}>出票新鲜度</div>
                <div style={{ ...num, fontSize: 13, color: stale ? '#ffcf4d' : 'var(--sub)' }}>
                  {fresh == null ? '—' : fmtAgo(fresh)}{stale ? ' (僵死)' : ''}
                </div>
              </div>
            </div>
            <ScoreBar score={v.score} />
          </>
        ) : (
          <div style={{ fontSize: 12, color: v.available === false ? 'var(--sub)' : 'var(--dim)', lineHeight: 1.6 }}>
            {st.detail}
            <span style={{ ...num, marginLeft: 6, color: 'var(--dim)' }}>· {data?.runs ?? 0} 次渲染</span>
          </div>
        )}

        <div style={{ marginTop: 10, fontSize: 10.5, color: 'var(--dim)', lineHeight: 1.5 }}>
          第5路增强信号 · 渲染 H4/M15/M5 三周期图表→视觉模型(GPU2)识别结构→融合提方向准确率（提准非拦截）
        </div>
      </div>
    )
  }

  // ── 信号模式：信号源参考（第5路增强） ──
  if (mode === 'signal') {
    return (
      <div style={{
        marginTop: 12, padding: '12px 14px', borderRadius: 8,
        background: 'linear-gradient(135deg, rgba(255,207,77,.05), rgba(120,200,255,.06))',
        border: `1px solid ${tone.bd}`,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 12.5, color: 'var(--sub)', fontWeight: 700 }}>视觉第四票（第5路增强 · 视觉模型）</span>
          <Badge t={st.t} text={st.h} />
          <span style={{ ...num, fontSize: 11, color: 'var(--dim)', marginLeft: 'auto' }}>{model}</span>
        </div>

        {v.available ? (
          <>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginTop: 8 }}>
              <span style={{ ...num, fontSize: 17, fontWeight: 900, color: ds.color }}>{ds.label}</span>
              <span style={{ ...num, fontSize: 11, color: 'var(--sub)' }}>
                强度 {v.score > 0 ? '+' : ''}{(v.score || 0).toFixed(2)} · 置信 {pct(v.confidence)} · 系数 ×{v.weight_scale || 1} · {fmtMs(v.latency_ms)}
              </span>
              <span style={{
                marginLeft: 'auto', fontSize: 10.5, padding: '2px 8px', borderRadius: 20,
                color: v.agree ? '#2ee6a0' : '#ffcf4d',
                background: v.agree ? 'rgba(46,230,160,.12)' : 'rgba(255,207,77,.12)',
                border: `1px solid ${v.agree ? 'rgba(46,230,160,.32)' : 'rgba(255,207,77,.32)'}`,
              }}>{v.agree ? 'H4·M15·M5 同向' : 'H4·M15·M5 分歧'}</span>
            </div>

            <div style={{ marginTop: 10 }}><ScoreBar score={v.score} /></div>
            <div style={{ marginTop: 10 }}><TimeframeReads v={v} /></div>
          </>
        ) : (
          <div style={{ marginTop: 8, fontSize: 12, color: v.available === false ? 'var(--sub)' : 'var(--dim)', lineHeight: 1.6 }}>
            {st.detail}
          </div>
        )}

        {v.note && <div style={{ marginTop: 8, fontSize: 11, color: 'var(--sub)', lineHeight: 1.5 }}>{v.note}</div>}

        <div style={{ marginTop: 10, fontSize: 10.5, color: 'var(--dim)', lineHeight: 1.5 }}>
          算式：聚合方向 = 0.6×H4 + 0.4×M15 + 0.2×M5 加权；三帧分歧时权重系数降至 {v.agree ? '1.0' : '0.7'}。
          仅作增强信号融合进决策链提方向准确率，<b style={{ color: 'var(--sub)' }}>不参与 GO/NO-GO 闸门、不砍交易笔数</b>。
        </div>
        {/* ★ 2026-08-18 自相矛盾修复：上一段已写明"融合进决策链"，这里却写"绝不参与
            交易与下单"——同一张卡里两句话互相打脸，客户不知该信哪句，运维排障也无从判断
            视觉票到底算不算数。事实是：视觉票以 0.30 权重参与方向加权（关云后是本地三脑
            之一），但它既不能单独触发下单，也不做 GO/NO-GO 一票否决。红线要说的是后者，
            改成准确表述，而不是一句已经过期的"绝不参与"。 */}
        <div style={{
          marginTop: 8, fontSize: 10.5, color: 'var(--gold)', lineHeight: 1.5,
          padding: '6px 8px', borderRadius: 6, background: 'rgba(255,207,77,.08)', border: '1px solid rgba(255,207,77,.25)',
        }}>
          ★ 边界：视觉票以权重 <b>0.30</b> 参与方向加权，但<b>不能单独触发下单</b>，
          也<b>不做一票否决</b>；须与 Chronos 锚 / 体制基线合成共识后才成立（提准非拦截）。
        </div>
      </div>
    )
  }

  // ── 默认 status 模式：系统管理（完整诊断） ──
  return (
    <div className="panel" style={{ padding: 18 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 4 }}>
        {/* 型号修正：标题原写 3b，正文与后端实配均为 qwen2.5vl:7b（5.4GB Q4_K_M，
            刚好 fit 8GB 显存）。标题写错型号会让运维照着 3b 去拉模型。
            型号一律以后端下发的 model 字段为准，写死只作兜底。 */}
        <span style={{ fontSize: 15, fontWeight: 600, color: 'var(--txt)' }}>
          视觉结构票 · 知觉模型（{model || 'qwen2.5vl:7b'}）
        </span>
        <Badge t={st.t} text={st.h} />
        <span style={{ ...num, fontSize: 11, color: 'var(--dim)', marginLeft: 'auto' }}>{model}</span>
      </div>
      <div style={{ fontSize: 12, color: 'var(--sub)', marginBottom: 14, lineHeight: 1.6 }}>{st.detail}</div>

      {/* 角色说明 */}
      <div style={{ fontSize: 12, color: 'var(--sub)', lineHeight: 1.7, background: 'var(--panel2)', borderRadius: 8, padding: '10px 12px', borderLeft: '3px solid var(--blue)', marginBottom: 14 }}>
        第5路增强信号：后台实时（90s）渲染 H4/M15/M5 三周期图表 → 视觉模型（qwen2.5vl:7b，运行于 GPU2 第二张 3060Ti，
        与 GPU1 第一张 3060Ti 上的 qwen3:8b 物理隔离）识别市场结构 → 缓存 VisionVote，
        meta_agent 同步读取融合进决策链提方向准确率。<b style={{ color: 'var(--sub)' }}>加法融合、非闸门、不砍交易笔数</b>，
        专治亚盘震荡方向判断失效。独占 GPU2 显存，与 GPU1 上的 qwen3:8b / CPU 上的 Chronos-2 零争抢。
        （GPU 编号按 Windows 任务管理器视角：GPU0=核显接显示器 / GPU1=第一张3060Ti / GPU2=第二张3060Ti）
      </div>

      {/* 当前视觉票输出 */}
      {v.available ? (
        <>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
            <span style={{ fontSize: 12, color: 'var(--dim)' }}>当前聚合方向</span>
            <span style={{ ...num, fontSize: 18, fontWeight: 900, color: ds.color }}>{ds.label}</span>
            <span style={{ ...num, fontSize: 12, color: 'var(--sub)' }}>
              强度 {v.score > 0 ? '+' : ''}{(v.score || 0).toFixed(2)} · 置信 {pct(v.confidence)} · 系数 ×{v.weight_scale || 1}
            </span>
            {v.agree
              ? <span style={{ fontSize: 11, color: '#2ee6a0' }}>H4·M15·M5 同向</span>
              : <span style={{ fontSize: 11, color: '#ffcf4d' }}>H4·M15·M5 分歧</span>}
          </div>
          <ScoreBar score={v.score} />
          <div style={{ marginTop: 14 }}><TimeframeReads v={v} /></div>
        </>
      ) : (
        <div style={{ fontSize: 12, color: 'var(--sub)', marginBottom: 14, lineHeight: 1.6 }}>
          暂无有效视觉票输出：{st.detail}。系统照常按其余信号决策，视觉增强暂未参与。
        </div>
      )}

      {/* 运行诊断指标 */}
      <div style={{ display: 'flex', gap: 14, flexWrap: 'wrap', paddingTop: 14, borderTop: '1px solid var(--line)' }}>
        <Stat label="渲染次数" value={data?.runs ?? 0} unit="次" />
        <Stat label="成功出票" value={data?.ok_runs ?? 0} unit="次" accent={(data?.ok_runs ?? 0) > 0 ? 'var(--green)' : undefined} />
        {v.latency_ms != null && <Stat label="推理耗时" value={fmtMs(v.latency_ms)} />}
        {fresh != null && <Stat label="出票新鲜度" value={fmtAgo(fresh)} accent={stale ? '#ffcf4d' : undefined} />}
      </div>

      {/* 诊断细节 */}
      <div style={{ marginTop: 12, fontSize: 11, color: 'var(--dim)', lineHeight: 1.8 }}>
        <div>当前行情主号：<span style={{ ...num, color: 'var(--sub)' }}>{data?.last_primary || '—'}</span></div>
        {data?.last_err && <div style={{ color: '#ffcf4d' }}>最近错误：{data.last_err}</div>}
        {v.note && <div style={{ color: 'var(--sub)' }}>结构备注：{v.note}</div>}
      </div>

      {/* 缺模型时的部署指引 */}
      {st.t === '缺模型' && (
        <div style={{ marginTop: 14, padding: 12, borderRadius: 10, border: '1px dashed var(--line)', background: 'rgba(255,255,255,.02)' }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--gold)', marginBottom: 6 }}>如何启用视觉第四票</div>
          <ol style={{ margin: 0, paddingLeft: 18, fontSize: 12, color: 'var(--sub)', lineHeight: 1.9 }}>
            <li>安装 Ollama（ollama.com，Windows 一键安装包）</li>
            <li>命令行执行 <code style={{ fontFamily: 'var(--font-num)', color: 'var(--blue)' }}>ollama pull qwen2.5vl:3b</code>（约 3.2GB，多模态视觉模型）</li>
            <li>回到本页，状态变为「在岗」即接入完成</li>
          </ol>
          <div style={{ fontSize: 11, color: 'var(--dim)', marginTop: 8, lineHeight: 1.6 }}>
            不装也能正常交易 —— 视觉模型是增强项不是依赖项，缺席时系统按其余信号运行，只是少了"看图识别结构"这一路增强。
          </div>
        </div>
      )}
    </div>
  )
}
