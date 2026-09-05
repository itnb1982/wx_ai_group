/**
 * 授权与激活 API 层（V6 Phase 8.4）
 *
 * ╔════════════════════════════════════════════════════════════════════╗
 * ║ ★ 前端授权红线（与后端 9.4.2「只关水龙头，不抽走桶里的水」同源）    ║
 * ║                                                                    ║
 * ║   授权失效时**绝不允许**用遮罩层把仪表盘挡住。                      ║
 * ║   原因：授权是商业契约，不是风控。客户此刻仓位还在市场里，          ║
 * ║   他必须能看到浮亏、能点平仓、能改止损。把界面糊死等于               ║
 * ║   "因为你没续费，所以你的钱我也不让你管了" —— 这会直接产生赔付纠纷。 ║
 * ║                                                                    ║
 * ║   正确做法：顶部提示条 + 侧栏徽章 + 独立激活页，仪表盘全程可用。     ║
 * ╚════════════════════════════════════════════════════════════════════╝
 */
import { getJSON } from './api.js'

/** 授权状态（不需登录 —— 过期时客户可能连登录都困难，求助通道不能剪） */
export function fetchLicenseStatus() {
  return getJSON('/license/status')
}

/** 本机机器码（客户申请/迁移授权时报给服务商） */
export function fetchMachineCode() {
  return getJSON('/license/machine')
}

/** 离线激活：粘贴令牌即可，不依赖联网 */
export function activateLicense(token, customer = '') {
  return getJSON('/license/activate', {
    method: 'POST',
    body: JSON.stringify({ token: token.trim(), customer }),
  })
}

/** 主动心跳：拉取续期/吊销。★ 失败不影响交易，前端也不得据此告警 */
export function licenseHeartbeat() {
  return getJSON('/license/heartbeat', { method: 'POST' })
}

/** 释放账号占用的配额坑（软删，保留对账痕迹） */
export function releaseAccountSlot(mt5Login) {
  return getJSON('/license/release', {
    method: 'POST',
    body: JSON.stringify({ mt5_login: String(mt5Login) }),
  })
}

// ─────────────────────────────────────────────────────────────
// 状态呈现规则（集中在这里，避免各组件各判各的导致配色不一致）
// ─────────────────────────────────────────────────────────────

/** 一切正常、无需打扰客户的状态 */
const SILENT_STATES = new Set(['active', 'disabled'])

/** 还能开仓但需要提醒的状态（黄条） */
const WARN_STATES = new Set(['trial', 'grace'])

/**
 * 提示条等级：
 *   'none' 不显示 | 'warn' 黄条（还能交易） | 'block' 红条（已停开新仓）
 *
 * 刻意把"还能交易"和"已停开新仓"分成两个颜色档：
 * 试用期/宽限期客户看到红色会以为系统已经停了，慌张打电话；
 * 而真停了却只给黄色，客户会以为还能跑，错过续费窗口。
 */
export function bannerLevel(st) {
  if (!st || !st.state) return 'none'
  if (SILENT_STATES.has(st.state)) return 'none'
  // 试用/宽限：仍可开仓 → 黄
  if (WARN_STATES.has(st.state)) {
    // 试用期还剩很多天时不必天天弹，剩 3 天内才提醒
    if (st.state === 'trial' && (st.days_remaining ?? 99) > 3) return 'none'
    return 'warn'
  }
  // 其余（expired / unlicensed / machine_mismatch / suspended / clock_tampered / quota_exceeded）
  return st.allow_open ? 'warn' : 'block'
}

/** 徽章配色：金=正常，黄=需关注，红=已停开新仓 */
export function badgeTone(st) {
  const lv = bannerLevel(st)
  if (lv === 'block') return 'bad'
  if (lv === 'warn') return 'warn'
  return 'ok'
}

/** 状态 → 客户看得懂的一句话行动指引（不是技术描述） */
export function actionHint(st) {
  if (!st) return ''
  switch (st.state) {
    case 'trial':
      return `试用剩余 ${st.days_remaining ?? 0} 天，到期后将停止开新仓（持仓不受影响）`
    case 'grace':
      return '授权已到期，正处于 72 小时宽限期。请尽快联系服务商续期'
    case 'expired':
      return '授权已到期，系统已停止开新仓。已有持仓的止损止盈仍在正常工作'
    case 'unlicensed':
      return '尚未激活。请在「授权激活」页粘贴授权令牌'
    case 'machine_mismatch':
      return '当前授权绑定的是另一台机器。更换设备请联系服务商重新签发'
    case 'suspended':
      return '授权已被停用，请联系服务商'
    case 'clock_tampered':
      return '检测到系统时间被回拨。请将系统时间校准为网络时间后重试'
    case 'quota_exceeded':
      return `账号数已达上限（${st.used_accounts ?? '?'}/${st.max_accounts ?? '?'}）。可释放闲置账号或升级配额`
    default:
      return ''
  }
}
