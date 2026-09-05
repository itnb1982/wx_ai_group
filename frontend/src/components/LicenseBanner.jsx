/**
 * 授权提示条（V6 Phase 8.4）
 *
 * ★ 只是一条横幅，绝不遮挡下方内容。
 *   授权失效只停开新仓，客户的持仓还在市场里，他必须能看到浮亏、能点平仓。
 *   遮罩式"付费墙"在交易软件里等同于扣押客户资产的操作权——红线，永不实现。
 *
 * 两个颜色档（见 services/license.js bannerLevel 注释）：
 *   warn 黄条 = 还能交易，但要处理了（试用剩 ≤3 天 / 宽限期）
 *   block 红条 = 已停开新仓（过期 / 未授权 / 机器不匹配 / 停用 / 时间异常）
 */
import { useState } from 'react'
import { useLicense } from '../hooks/useLicense.js'
import { bannerLevel, actionHint } from '../services/license.js'

export default function LicenseBanner({ onGoActivate }) {
  const st = useLicense()
  const [dismissed, setDismissed] = useState(false)

  const level = bannerLevel(st)
  if (level === 'none') return null

  // 红条不可关闭：客户关掉后就忘了，等发现"怎么一天没开单"已经损失了交易机会。
  // 黄条可关闭（本次会话内），因为它不影响交易，天天糊在头上很烦人。
  if (level === 'warn' && dismissed) return null

  const hint = actionHint(st)

  return (
    <div className={`lic-banner lic-${level}`} role="status">
      <span className="lic-banner-dot" aria-hidden />
      <span className="lic-banner-main">
        <b>{st.state_label}</b>
        {hint ? <span className="lic-banner-hint">{hint}</span> : null}
      </span>

      {/* 红条必须明确写清"持仓不受影响"，否则客户第一反应是"我的单被平了？" */}
      {level === 'block' && (
        <span className="lic-banner-safe">已有持仓不受影响，止损止盈照常运行</span>
      )}

      <button className="lic-banner-btn" onClick={onGoActivate}>
        {st.state === 'quota_exceeded' ? '管理账号配额' : '前往激活'}
      </button>

      {level === 'warn' && (
        <button
          className="lic-banner-x"
          onClick={() => setDismissed(true)}
          title="本次不再提示"
          aria-label="关闭提示"
        >
          ×
        </button>
      )}
    </div>
  )
}
