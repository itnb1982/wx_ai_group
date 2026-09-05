/**
 * 关于弹窗（V6 第十章合规必备）
 *
 * 三处版权之一（登录页页脚 / 侧栏授权徽章 / 本弹窗）。
 * 必须展示：版本号(APP_VERSION) + 构建时间(BUILD_TIME) + 提交号(GIT_COMMIT)
 * + 授权信息 + 风险提示(RISK_DISCLAIMER) + 第三方模型声明(THIRD_PARTY_NOTICE)。
 * 所有文案一律从 brand/identity.js 单一权威源取，禁止手写。
 */
import { useEffect, useState } from 'react'
import {
  BRAND, TAGLINE, APP_VERSION, BUILD_TIME, GIT_COMMIT,
  copyrightText, RISK_DISCLAIMER, THIRD_PARTY_NOTICE, MODEL_LINEUP,
} from '../brand/identity'
import { useLicense } from '../hooks/useLicense'
import { LogoStacked } from './brand/Logo.jsx'
import { useModelReadiness } from '../brand/modelReadiness'

// 「关于」弹窗里的模型阵容表。
//
// 为什么这张表必须存在于「关于」而不只是系统管理页：
// 「关于」是客户截图发给我们做工单的第一现场。把四个模型的真实在岗状态
// 印在这里，能省掉排障时"你那边 Qwen 装了吗 / 我不知道"的一整轮拉扯。
//
// 更要紧的一点：这里紧挨着风险提示与第三方声明。若副文宣称四模型协同，
// 而阵容表明明白白只亮了三盏灯，客户当场就能对上账——
// 这是把「虚标红线」做成客户可自查的东西，而不是我们的内部自律。
// ★ 2026-08-18 关云架构同步（三处失真一并修）：
//   ① 漏了视觉模型 qwen2.5vl:7b —— 它以 0.30 权重实际参与方向裁决，是三脑之一，
//      客户在这张表里却根本看不到它，等于阵容少报一员；
//   ② lane 写「本地双核」，实际已是本地三脑（语义 × 视觉 × 时序）；
//   ③ 「{readyN}/4 在岗」把两朵云永远算进分母 —— 用户主动关云后永远显示 x/4，
//      看着像半坏的系统。关云是**主动省钱选择**，不是故障，分母必须跟着模式走。
// ★ 2026-08-19 云端永久弃用：阵容改为 identity.js 的 MODEL_LINEUP（四层架构——
//   感知/校验/观测；DeepSeek/混元条目已删除；qwen3 定位校验层不投票）。
const LINEUP = MODEL_LINEUP.map((m) => ({
  key: m.key, name: m.label, lane: m.tier, role: m.role, cloud: false,
}))

function LineupTable({ rd }) {
  // 云端就绪从 cloud 明细取；取不到就按未就绪显示——宁可少报不可虚标。
  const on = (k) => {
    if (k === 'qwen') return !!rd?.qwen
    if (k === 'chronos') return !!rd?.chronos
    if (k === 'vision') return !!rd?.vision
    return !!rd?.cloud?.[k]?.ready
  }
  // cloudEnabled 三态：true=混跑 / false=纯本地 / null=尚未取到（不猜，按混跑显示全阵容）
  const cloudOff = rd?.cloudEnabled === false
  const rows = cloudOff ? LINEUP.filter((m) => !m.cloud) : LINEUP
  const readyN = rows.filter((m) => on(m.key)).length
  const totalN = rows.length
  return (
    <div style={{ marginTop: 16 }}>
      <div style={{
        display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 6,
        fontSize: 11.5, fontWeight: 800, color: 'var(--sub)',
      }}>
        <span>模型阵容</span>
        <span style={{ fontFamily: 'var(--font-num)', color: readyN >= totalN ? 'var(--green)' : 'var(--gold)' }}>
          {readyN}/{totalN} 在岗
        </span>
        {cloudOff && (
          <span style={{ fontSize: 10.5, fontWeight: 700, color: 'var(--dim)' }}>
            · 云端方案已永久弃用 · 全本地运行（零云成本）
          </span>
        )}
      </div>
      <div style={{ border: '1px solid var(--line)', borderRadius: 10, overflow: 'hidden' }}>
        {rows.map((m, i) => {
          const ok = on(m.key)
          // 角色文案优先用后端下发（随云开关切换），本地写死的只作兜底
          const roleText = rd?.roles?.[m.key] || m.role
          return (
            <div
              key={m.key}
              style={{
                display: 'flex', alignItems: 'center', gap: 8, padding: '7px 10px',
                fontSize: 11.5,
                borderTop: i ? '1px solid var(--line)' : 'none',
                background: ok ? 'transparent' : 'rgba(255,255,255,0.015)',
              }}
            >
              <span style={{
                width: 6, height: 6, borderRadius: '50%', flex: '0 0 6px',
                background: ok ? 'var(--green)' : 'var(--dim)',
              }} />
              <span style={{ color: ok ? 'var(--txt)' : 'var(--dim)', fontWeight: 600, flex: '0 0 118px' }}>
                {m.name}
              </span>
              <span style={{ color: 'var(--dim)', flex: '0 0 62px', fontSize: 10.5 }}>{m.lane}</span>
              <span style={{ color: 'var(--dim)', fontSize: 10.5, flex: 1 }}>{roleText}</span>
              <span style={{
                fontSize: 10, fontWeight: 700,
                color: ok ? 'var(--green)' : 'var(--dim)',
              }}>{ok ? '在岗' : '未就绪'}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function Row({ label, value, mono }) {
  return (
    <div style={{ display: 'flex', gap: 10, padding: '6px 0', borderBottom: '1px solid var(--line)', fontSize: 12.5 }}>
      <span style={{ color: 'var(--sub)', flex: '0 0 84px' }}>{label}</span>
      <span style={{ color: 'var(--txt)', wordBreak: 'break-all', ...(mono ? { fontFamily: 'var(--font-num)' } : {}) }}>
        {value || '—'}
      </span>
    </div>
  )
}

export default function AboutDialog({ open, onClose }) {
  // useLicense 初始快照为 null（首次挂载尚未拉取），不能直接解构 status。
  // 后端 /license/status 返回 { success, data: {...} }，hook 已取 data 作为 _state，
  // 因此直接拿到的是 { state, state_label, ... }，不是嵌套 status 对象。
  const lic = useLicense() || {}
  const rd = useModelReadiness()
  const [buildTime, setBuildTime] = useState('')

  useEffect(() => {
    if (BUILD_TIME) {
      try {
        setBuildTime(new Date(BUILD_TIME).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai' }))
      } catch {
        setBuildTime(BUILD_TIME)
      }
    }
  }, [])

  if (!open) return null
  const licLabel = {
    active: '已授权', trial: '试用中', grace: '宽限期内',
    expired: '已过期', unlicensed: '未授权', disabled: '已停用',
  }[lic.state] || lic.state_label || '未知'

  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 999,
        background: 'rgba(4,8,16,0.66)', backdropFilter: 'blur(4px)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 16,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 480, maxWidth: '100%', maxHeight: '90vh', overflowY: 'auto',
          background: 'var(--panel)', border: '1px solid var(--line)',
          borderRadius: 16, padding: 24, boxShadow: '0 20px 60px rgba(0,0,0,0.5)',
        }}
      >
        <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 8 }}>
          <LogoStacked size={64} />
        </div>

        <div style={{ marginTop: 8 }}>
          <Row label="版本" value={`商业版 v${APP_VERSION}`} mono />
          <Row label="构建时间" value={buildTime} mono />
          <Row label="提交号" value={GIT_COMMIT} mono />
          <Row label="授权状态" value={`${licLabel}${lic.edition_label ? ` · ${lic.edition_label}` : ''}`} />
          <Row label="授权客户" value={lic.customer || '—'} />
          <Row
            label="有效期"
            value={lic.valid_until ? `${String(lic.valid_until).slice(0, 10)}${lic.days_remaining != null ? `（剩 ${lic.days_remaining} 天）` : ''}` : '永久授权'}
          />
        </div>

        <LineupTable rd={rd} />

        <div style={{
          marginTop: 16, padding: 12, borderRadius: 10,
          background: 'rgba(255,92,108,0.08)', border: '1px solid rgba(255,92,108,0.25)',
          fontSize: 11.5, lineHeight: 1.7, color: 'var(--sub)',
        }}>
          <div style={{ color: 'var(--red)', fontWeight: 800, marginBottom: 4 }}>风险提示</div>
          {RISK_DISCLAIMER}
        </div>

        <div style={{ marginTop: 10, fontSize: 11, lineHeight: 1.7, color: 'var(--dim)' }}>
          {THIRD_PARTY_NOTICE}
        </div>

        <div style={{ marginTop: 14, textAlign: 'center', fontSize: 11.5, color: 'var(--dim)' }}>
          {copyrightText()} · 商业版
        </div>

        <button
          onClick={onClose}
          style={{
            marginTop: 16, width: '100%', padding: '10px 0', cursor: 'pointer',
            background: 'var(--gold)', color: '#1a1206', border: 'none',
            borderRadius: 10, fontWeight: 800, fontSize: 13,
          }}
        >关闭</button>
      </div>
    </div>
  )
}
