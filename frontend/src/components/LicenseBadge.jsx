/**
 * 侧栏授权徽章（V6 Phase 8.4）
 *
 * 放在侧栏底部用户卡上方——客户随时能瞟一眼"我的授权还有多久"，
 * 而不是等到某天突然不开单了才去翻设置。
 *
 * 配色刻意与盈亏色脱钩（沿用 Phase 4 风控面板的结论）：
 *   金 = 正常 / 黄 = 需关注 / 红 = 已停开新仓。
 *   如果用绿色表示"正常"，客户会下意识把它读成"在赚钱"。
 */
import { useLicense } from '../hooks/useLicense.js'
import { badgeTone } from '../services/license.js'

/** 剩余天数 → 一句话（永久授权不显示天数，显示"永久"） */
function remainText(st) {
  if (!st) return ''
  if (st.state === 'disabled') return '未启用校验'
  if (!st.valid_until) return '永久授权'
  const d = st.days_remaining
  if (d === null || d === undefined) return ''
  if (d < 0) return '已到期'
  if (d === 0) return '今日到期'
  return `剩 ${d} 天`
}

export default function LicenseBadge({ onClick }) {
  const st = useLicense()
  // 状态还没拉到时不占位闪烁——空着比闪一下"未授权"强得多
  if (!st) return null

  const tone = badgeTone(st)
  const remain = remainText(st)
  const quota =
    st.max_accounts > 0 ? `${st.used_accounts ?? 0}/${st.max_accounts}` : ''

  return (
    <button
      className={`lic-badge lic-badge-${tone}`}
      onClick={onClick}
      title={st.message || st.state_label}
    >
      <span className="lic-badge-ic" aria-hidden>
        <svg viewBox="0 0 24 24" width="14" height="14" fill="none"
             stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="10" width="18" height="11" rx="2" />
          <path d="M8 10V7a4 4 0 0 1 8 0v3" />
        </svg>
      </span>
      <span className="lic-badge-body">
        <span className="lic-badge-top">
          <span className="lic-badge-state">{st.state_label}</span>
          {st.edition_label ? (
            <span className="lic-badge-edition">{st.edition_label}</span>
          ) : null}
        </span>
        <span className="lic-badge-sub">
          {remain}
          {quota ? <span className="lic-badge-quota">账号 {quota}</span> : null}
        </span>
      </span>
    </button>
  )
}
