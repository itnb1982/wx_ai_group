// XAU/USD 万象Ai 自动量化交易系统 — 侧栏品牌头 + 导航 + 授权徽章 + 用户卡
import { useEffect, useState } from 'react'
import LicenseBadge from './LicenseBadge.jsx'
import AboutDialog from './AboutDialog.jsx'
import { LogoMark } from './brand/Logo.jsx'
import { BRAND, TAGLINE } from '../brand/identity'
import { useModelReadiness } from '../brand/modelReadiness.js'

// 内联 SVG 图标（替代 emoji：emoji 字体不一致才显丑，且专业感不够）
const Icon = {
  Dashboard: () => (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="7" height="9" rx="1.5" />
      <rect x="14" y="3" width="7" height="5" rx="1.5" />
      <rect x="14" y="12" width="7" height="9" rx="1.5" />
      <rect x="3" y="16" width="7" height="5" rx="1.5" />
    </svg>
  ),
  Wallet: () => (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 7c0-1.1.9-2 2-2h12a2 2 0 0 1 2 2v2H5a2 2 0 0 1-2-2z" />
      <path d="M3 9v10a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V11" />
      <circle cx="17" cy="15" r="1.5" />
    </svg>
  ),
  Shield: () => (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2 4 5v6c0 5 3.5 9.5 8 11 4.5-1.5 8-6 8-11V5l-8-3z" />
      <path d="M9 12l2 2 4-4" />
    </svg>
  ),
  Key: () => (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="8" cy="14" r="4" />
      <path d="M11 11l9-9" />
      <path d="M17 5l3 3" />
      <path d="M14 8l3 3" />
    </svg>
  ),
  Logout: () => (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="M16 17l5-5-5-5" />
      <path d="M21 12H9" />
    </svg>
  ),
  Pulse: () => (
    <svg viewBox="0 0 24 24" width="8" height="8" fill="currentColor">
      <circle cx="12" cy="12" r="6" />
    </svg>
  ),
  // 芯片：本地双核（Qwen3-8B / Chronos-2）跑在客户自己的机器上，
  // 用芯片而非云图标，正是要区别于上面那两个云端大脑。
  Chip: () => (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="7" y="7" width="10" height="10" rx="2" />
      <path d="M10 3v3M14 3v3M10 18v3M14 18v3M3 10h3M3 14h3M18 10h3M18 14h3" />
    </svg>
  ),
  // 信号源参考：广播波，区别于云端双脑（云）/本地双核（芯片），
  // 强调"多模型信号对照"这一观测属性。
  Signal: () => (
    <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M4 12a8 8 0 0 1 16 0" />
      <path d="M7.5 12a4.5 4.5 0 0 1 9 0" />
      <circle cx="12" cy="12" r="1.4" fill="currentColor" stroke="none" />
    </svg>
  ),
}

const ITEMS = [
  { key: 'dashboard', label: '仪表盘', icon: Icon.Dashboard, desc: 'AI 交易大脑实时作战' },
  { key: 'accounts',  label: '账户管理', icon: Icon.Wallet, desc: 'MT5 多账户连接与持仓' },
  { key: 'strategy',  label: '策略风控', icon: Icon.Shield, desc: 'AI 策略与风险管理' },
  { key: 'keys',      label: 'AI Key 管理', icon: Icon.Key, desc: 'DeepSeek / 混元密钥' },
  { key: 'system',    label: '系统管理', icon: Icon.Chip, desc: '本地双核运行台' },
  { key: 'signals',   label: '信号源参考', icon: Icon.Signal, desc: 'fusion_v2 融合票·权重0.22' },
]

// 从 email 取头像首字符
const avatarOf = (email) => {
  if (!email) return '?'
  const head = email.split('@')[0]
  return head.slice(0, 2).toUpperCase()
}

// 角色徽章（按 email 域名简单区分）
const roleOf = (email) => {
  if (!email) return { label: '访客', color: '#888' }
  if (email.includes('admin') || email.includes('wx.local')) return { label: '管理员', color: '#ffce4d' }
  if (email.includes('demo')) return { label: '演示', color: '#5ad' }
  return { label: '用户', color: '#4f8cff' }
}

export default function Sidebar({ view, onSelect, onLogout, user }) {
  // 版本号从 /api/info 动态拉取，配置升级即同步
  const [appMeta, setAppMeta] = useState({ version: '1.0.0', name: 'XAU/USD 万象Ai 自动量化交易系统' })
  // 模型就绪度（全局单例轮询，多处订阅只发一份请求）
  const readiness = useModelReadiness()
  useEffect(() => {
    let alive = true
    fetch('/api/info')
      .then((r) => r.json())
      .then((d) => {
        if (!alive) return
        if (d) setAppMeta({ version: d.version || '1.0.0', name: d.app || '' })
      })
      .catch(() => {})
    return () => { alive = false }
  }, [])

  const email = user?.email || ''
  const avatar = avatarOf(email)
  const role = roleOf(email)
  const [aboutOpen, setAboutOpen] = useState(false)

  return (
    <aside className="sidebar">
      {/* ───── 顶部品牌头（点击查看「关于」弹窗） ───── */}
      <div className="side-brand" style={{ cursor: 'pointer' }} title="点击查看关于 / 版本 / 授权"
           onClick={() => setAboutOpen(true)}>
        <div className="side-logo-mark" aria-label="logo">
          <LogoMark size={38} />
        </div>
        <div className="side-brand-text">
          <div className="side-brand-name">{BRAND.fullName}</div>
          {/* 副文随实测就绪度切换：第四个模型没在岗就说三模型，不虚标 */}
          <div className="side-brand-sub" title={readiness.readyCount != null ? `当前 ${readiness.readyCount}/${readiness.total} 个模型在岗` : ''}>
            {readiness.compact || TAGLINE.compact}
          </div>
          <div className="side-brand-meta">
            <span className="side-version" title="版本号（配置升级自动同步）">v{appMeta.version}</span>
            <span className="side-build-tag">商业版</span>
          </div>
        </div>
      </div>

      {/* 装饰分隔线 */}
      <div className="side-divider">
        <span className="side-divider-dot" />
      </div>

      {/* ───── 导航区 ───── */}
      <nav className="side-nav">
        {ITEMS.map((it) => {
          const Ico = it.icon
          const active = view === it.key
          return (
            <button
              key={it.key}
              className={`side-item ${active ? 'on' : ''}`}
              onClick={() => onSelect(it.key)}
            >
              <span className="side-ic"><Ico /></span>
              <span className="side-txt">
                <span className="side-label">{it.label}</span>
                <span className="side-desc">{it.desc}</span>
              </span>
              {active && <span className="side-active-bar" aria-hidden />}
            </button>
          )
        })}
      </nav>

      {/* ───── 底部：授权徽章 + 用户卡 + 退出登录 ─────
          授权徽章刻意不做成导航项：它是"状态"不是"功能"，
          客户平时只需要瞟一眼剩余天数，需要时才点进去激活。
          放进导航列表会让四个高频功能被一个低频入口稀释。 */}
      <div className="side-foot">
        <LicenseBadge onClick={() => onSelect('license')} />
        <div className="side-user-card">
          <div className="side-user-avatar" style={{
            background: `conic-gradient(from ${(email.charCodeAt(0) || 65) * 7}deg, #4f8cff, #b07bff, #ffd56b, #4f8cff)`,
          }}>
            <span>{avatar}</span>
          </div>
          <div className="side-user-info">
            <div className="side-user-email" title={email}>{email || '未登录'}</div>
            <div className="side-user-meta">
              <span className="side-role-badge" style={{ color: role.color, borderColor: role.color + '66' }}>
                {role.label}
              </span>
              <span className="side-user-status">
                <Icon.Pulse /> 在线
              </span>
            </div>
          </div>
        </div>
        <button className="logout-btn" onClick={onLogout} title="退出登录">
          <Icon.Logout />
          <span style={{ marginLeft: 6 }}>退出登录</span>
        </button>
      </div>

      <AboutDialog open={aboutOpen} onClose={() => setAboutOpen(false)} />
    </aside>
  )
}